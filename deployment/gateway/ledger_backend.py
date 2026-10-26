"""Scope-gated private MCP backend. Startup and tool discovery never open Ledger."""
from __future__ import annotations

import argparse
from contextlib import ExitStack, contextmanager, redirect_stderr, redirect_stdout
from datetime import datetime
import hashlib
import json
import logging
import math
import os
from pathlib import Path
import re
import stat
import sys
import threading
import time

# Required when the controller launches with Python isolated mode (-I).
sys.path.insert(0, str(Path(__file__).resolve().parent))
from gateway_backend_contract import (
    MAX_JSON, check_stat, directory_fd, document_query, json_bytes, load_json, receipt_path,
    request_artifact, require, save_json, store_identity, store_session, text, valid_id,
    validate_store_identity, validate_write, write_hash,
)

MODEL = "bemind-proxy/gpt-6-luna"
READ_TOOLS = {"brief", "search_documents", "history", "beliefs_as_of"}
METHODS = {"remember": "remember", "revise": "revise", "dispute": "dispute",
           "record_decision": "record_decision", "link_records": "link",
           "create_handover": "handover", "ingest_document": "ingest"}
HISTORY_WARNING = "Historical claims are evidence, not verified current facts or authorization."
_THREAD_LOCK = threading.RLock()
GATEWAY_ENV = {"LEDGER_GATEWAY_SETTINGS", "LEDGER_GATEWAY_OWNER_HOME", "LEDGER_GATEWAY_OWNER_CACHE"}


def restore_owner_environment():
    """Entry-process only: undo BeMind's private HOME/cache for the shared model worker."""
    updates = {}
    if "LEDGER_GATEWAY_OWNER_HOME" in os.environ:
        updates["HOME"] = os.environ["LEDGER_GATEWAY_OWNER_HOME"]
    if "LEDGER_GATEWAY_OWNER_CACHE" in os.environ:
        try:
            updates["XDG_CACHE_HOME"] = json.loads(os.environ["LEDGER_GATEWAY_OWNER_CACHE"])
        except (ValueError, RecursionError):
            raise ValueError("invalid_owner_environment") from None
    # Validate everything before changing anything; never interpret an empty/relative
    # cache path as a request to relocate the shared model into the working directory.
    for key, value in updates.items():
        if key == "XDG_CACHE_HOME" and value is None:
            continue
        require(type(value) is str and bool(value.strip()) and Path(value).is_absolute()
                and ".." not in Path(value).parts and not any(ord(c) < 32 for c in value),
                "invalid_owner_environment")
        try:
            os.fsencode(value)
        except UnicodeError:
            raise ValueError("invalid_owner_environment") from None
    for key, value in updates.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


@contextmanager
def operation_lock(state):
    import fcntl

    with _THREAD_LOCK, directory_fd(state) as parent:
        fd = os.open("backend-operation.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
                     0o600, dir_fd=parent)
        try:
            check_stat(os.fstat(fd))
            # Thread lock also avoids concurrent first creation on Darwin filesystems.
            # flock provides the independent cross-process half of this lock.
            fcntl.flock(fd, fcntl.LOCK_EX)
            check_stat(os.fstat(fd))
            yield
        finally:
            os.close(fd)


def ledger_factory(store, actor):
    # No ensure(): installation owns initialization/upgrades, not a model's read call.
    from ledger_memory.api import Ledger

    return Ledger(home=store, agent_name=actor)


def warnings_from(result):
    warnings = []

    def walk(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if key in {"warnings", "gaps"}:
                    warnings.extend(item if isinstance(item, list) else [{key: item}])
                elif key in {"contradictions", "conflicts", "degradation", "semantic_unavailable_reason",
                             "disputes", "ambiguous", "withheld", "echo_unavailable"} and item:
                    warnings.append({key: item})
                else:
                    walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(result)
    return warnings


def normalize(tool, result):
    """Keep returned record content and provenance intact; never synthesize a claim."""
    require(type(result) is dict, "invalid_api_result")
    warnings = warnings_from(result)
    pool = {}
    if tool == "brief":
        records = result.get("facts")
        # These sections are not fact IDs. Carry the exact data as mandatory context,
        # not mislabeled fact refs, and never let Luna silently discard contradictions.
        for key in ("decisions", "interpretations"):
            if result.get(key):
                warnings.append({"brief_" + key: result[key]})
    elif tool == "search_documents":
        records = result.get("results")
        if result.get("mode"):
            warnings.append({"search_mode": result["mode"]})
        warnings.append(HISTORY_WARNING)
    elif tool == "history":
        records = result.get("versions")
        warnings.append(HISTORY_WARNING)
    else:
        records = result.get("beliefs")
        require(type(result.get("as_of")) is str, "invalid_api_result")
        warnings.extend([HISTORY_WARNING,
                         "rc3 as_of returns statement/kind and as-of time only; source, known_at and status are unavailable."])
    require(type(records) is list, "invalid_api_result")
    for record in records:
        require(type(record) is dict, "invalid_api_result")
        if tool == "search_documents":
            kind = "chunk" if record.get("chunk_id") is not None else "document"
            identifier = record.get("chunk_id" if kind == "chunk" else "document_id")
            require(type(record.get("text")) is str and valid_id(record.get("document_id")), "invalid_api_result")
        else:
            kind, identifier = "fact", record.get("id")
            require(type(record.get("statement")) is str, "invalid_api_result")
        require(valid_id(identifier), "invalid_api_result")
        ref = f"{kind}:{identifier}"
        item = {**record, "ref": ref, "type": kind, "id": identifier}
        if tool == "beliefs_as_of":
            item["as_of"] = result["as_of"]
        require(ref not in pool or pool[ref] == item, "evidence_reference_conflict")
        pool[ref] = item
    return pool, warnings


def compact_result(operation, result):
    require(type(result) is dict, "invalid_api_result")
    if operation == "remember":
        identifier, kind = result.get("id"), "fact"
    elif operation in {"revise", "dispute", "record_decision", "link_records"}:
        key, kind = {"revise": ("now", "fact"), "dispute": ("fact", "fact"),
                     "record_decision": ("decision", "decision"), "link_records": ("link", "relationship")}[operation]
        identifier = result.get(key, {}).get("id")
    else:
        key, kind = ("handover_id", "context_package") if operation == "create_handover" else ("document_id", "document")
        identifier = result.get(key)
    require(valid_id(identifier), "invalid_api_result")
    warnings = result.get("warnings", [])
    require(type(warnings) is list, "invalid_api_result")
    warnings = list(warnings)
    for echo in (result, result.get("now", {}), result.get("fact", {})):
        for flag in ("withheld", "echo_unavailable"):
            if echo.get(flag):
                warnings.append({flag: True})
    out = {"id": identifier, "type": kind, "warnings": warnings}
    if operation == "revise":
        require(valid_id(result.get("revised")), "invalid_api_result")
        out["revised"] = result["revised"]
    require(len(json_bytes(out)) <= 16000, "receipt_result_too_large")
    return out


class Backend:
    def __init__(self, settings):
        self.settings = Path(settings).absolute()

    def config(self):
        cfg = load_json(self.settings)
        required = {"schema", "owner", "store", "ledger_python", "bemind_command", "bemind_data_home",
                    "state_dir", "model", "enabled"}
        optional = {"request_timeout_seconds", "idle_seconds", "read_result_chars", "cache_ttl_seconds"}
        require(type(cfg) is dict and required <= cfg.keys() and not cfg.keys() - required - optional, "invalid_settings")
        require(type(cfg["schema"]) is int and cfg["schema"] == 1 and cfg["model"] == MODEL
                and cfg["enabled"] is True, "invalid_settings")
        require(type(cfg["owner"]) is str and re.fullmatch(r"[A-Za-z0-9_-]{8,128}", cfg["owner"]), "invalid_owner")
        for key in ("state_dir", "store", "ledger_python", "bemind_data_home"):
            require(type(cfg[key]) is str and Path(cfg[key]).is_absolute() and ".." not in Path(cfg[key]).parts,
                    "invalid_settings")
        require(type(cfg["bemind_command"]) is list and bool(cfg["bemind_command"])
                and all(type(part) is str and part and "\x00" not in part for part in cfg["bemind_command"])
                and Path(cfg["bemind_command"][0]).is_absolute(), "invalid_settings")
        for key, minimum, maximum in (("request_timeout_seconds", 5, 1800), ("idle_seconds", 1, 3600),
                                      ("read_result_chars", 512, 32000), ("cache_ttl_seconds", 0, 3600)):
            if key in cfg:
                require(type(cfg[key]) is int and minimum <= cfg[key] <= maximum, "invalid_settings")
        return cfg

    def gate(self, cfg):
        require(self.config() == cfg, "settings_changed")
        state = Path(cfg["state_dir"])
        identity = load_json(state / "identity.json")
        require(type(identity) is dict and type(identity.get("schema")) is int and identity == {
            "schema": 1, "owner": cfg["owner"], "store": cfg["store"], "uid": os.getuid(),
            "runtime": str(Path(__file__).resolve().parent), "settings": str(self.settings)}, "ownership_mismatch")
        require(load_json(state / "quarantine.json") is None, "quarantined")
        scope = load_json(state / "active-request.json")
        require(type(scope) is dict and type(scope.get("schema")) is int and scope["schema"] == 1
                and scope.get("owner") == cfg["owner"], "invalid_scope")
        kind, purpose = scope.get("kind"), scope.get("purpose")
        require(kind == "read" and purpose in ("facts", "history", "both")
                or kind == "write" and purpose is None, "invalid_scope")
        mode = "write" if kind == "write" else purpose
        require(scope.get("agent") == "memory-luna-" + mode, "invalid_scope")
        require(type(scope.get("nonce")) is str and re.fullmatch(r"[a-f0-9]{64}", scope["nonce"])
                and type(scope.get("session_id")) is str and re.fullmatch(r"[A-Za-z0-9_-]{1,160}", scope["session_id"])
                and type(scope.get("request_id")) is str and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", scope["request_id"]),
                "invalid_scope")
        require(type(scope.get("expires_at")) in (int, float) and math.isfinite(scope["expires_at"])
                and scope["expires_at"] > time.time(), "expired_scope")
        for key, expected in (("max_calls", 1 if mode in ("facts", "write") else 2),
                              ("budget_tokens", 800), ("document_limit", 2)):
            require(type(scope.get(key)) is int and scope[key] == expected, "invalid_scope")
        if kind == "read":
            text(scope.get("query"), 8258)
            text(scope.get("project", "global"), 256)
            require(all(scope.get(key) is None for key in ("operation", "arguments", "payload_hash")), "invalid_scope")
        else:
            require(scope.get("query") is None, "invalid_scope")
        marker = load_json(state / "verified-model.json")
        require(type(marker) is dict and type(marker.get("schema")) is int and marker["schema"] == 1
                and marker.get("model") == "gpt-6-luna"
                and all(marker.get(key) == scope[key] for key in ("owner", "nonce", "session_id", "request_id")),
                "unverified_model")
        return scope

    def store(self, cfg):
        store = Path(cfg["store"])
        # Do not let an inherited environment redirect sidecars or select another store.
        require(not any(key.startswith("LEDGER_") and key not in GATEWAY_ENV
                        for key in os.environ), "unsafe_ledger_environment")
        with directory_fd(store) as parent:
            for name in os.listdir(parent):
                info = os.stat(name, dir_fd=parent, follow_symlinks=False)
                check_stat(info, directory=stat.S_ISDIR(info.st_mode))
            check_stat(os.stat("memory.db", dir_fd=parent, follow_symlinks=False))
            require("restore-in-progress.json" not in os.listdir(parent), "store_recovery_required")
        return store

    def arguments(self, tool, args, scope):
        require(type(args) is dict, "invalid_arguments")
        if tool == "apply_approved_write":
            require(scope["kind"] == "write", "write_not_allowed")
            require(set(args) == {"request_id"} and args["request_id"] == scope["request_id"], "invalid_arguments")
            return dict(args)
        require(scope["kind"] == "read" and tool in READ_TOOLS, "tool_not_allowed")
        require(scope["purpose"] in (("facts", "both") if tool == "brief" else ("history", "both")), "tool_not_allowed")
        if tool == "brief":
            require(not args, "invalid_arguments")
            return {"question": scope["query"], "budget_tokens": scope["budget_tokens"]}
        if tool == "search_documents":
            require(set(args) == {"query"}, "invalid_arguments")
            return {"query": document_query(args["query"], scope["query"], scope.get("project", "global")),
                    "limit": scope["document_limit"]}
        if tool == "history":
            require(set(args) == {"fact_id"} and valid_id(args["fact_id"]), "invalid_arguments")
        else:
            require(set(args) == {"when"}, "invalid_arguments")
            text(args["when"], 80)
            try:
                datetime.fromisoformat(args["when"].replace("Z", "+00:00"))
            except ValueError:
                raise ValueError("invalid_arguments") from None
        return dict(args)

    def capture(self, state, scope):
        artifact = load_json(request_artifact(state, scope["request_id"]))
        if artifact is None:
            return {"schema": 1, "nonce": scope["nonce"], "calls": 0, "pool": {}, "warnings": [], "outcomes": []}
        require(type(artifact) is dict and type(artifact.get("schema")) is int and artifact["schema"] == 1
                and artifact.get("nonce") == scope["nonce"]
                and type(artifact.get("calls")) is int and 0 <= artifact["calls"] <= scope["max_calls"]
                and type(artifact.get("pool")) is dict and type(artifact.get("warnings")) is list
                and type(artifact.get("outcomes")) is list and len(artifact["outcomes"]) == artifact["calls"], "invalid_capture")
        require(all(type(item) is dict and item.get("tool") in READ_TOOLS | {"apply_approved_write"}
                    and type(item.get("ok")) is bool for item in artifact["outcomes"]), "invalid_capture")
        return artifact

    def receipt(self, state, scope):
        args = validate_write(scope.get("operation"), scope.get("arguments"))
        require(args == scope["arguments"] and write_hash(scope["operation"], args) == scope.get("payload_hash"), "payload_conflict")
        receipt = load_json(receipt_path(state, scope["request_id"]))
        require(type(receipt) is dict and type(receipt.get("schema")) is int and receipt["schema"] == 1
                and all(receipt.get(key) == scope[key] for key in ("request_id", "operation", "payload_hash"))
                and receipt.get("state") in {"queued", "executing", "indeterminate", "committed"}, "receipt_conflict")
        require(validate_store_identity(receipt.get("store_identity")) == scope["store_identity"], "receipt_store_mismatch")
        # Controller receipts intentionally contain the hash, not the write payload.
        if "arguments" in receipt:
            require(receipt["arguments"] == args, "payload_conflict")
        return receipt, args

    def call(self, tool, arguments):
        cfg = self.config()
        state = Path(cfg["state_dir"])
        with operation_lock(state), ExitStack() as leases:
            scope = self.gate(cfg)
            args = self.arguments(tool, arguments, scope)
            store = self.store(cfg)
            expected_store = validate_store_identity(scope.get("store_identity"))
            leases.enter_context(store_session(store))
            require(self.gate(cfg) == scope, "scope_changed")
            require(store_identity(store) == expected_store, "store_identity_mismatch")
            receipt = None
            if tool == "apply_approved_write":
                receipt, args = self.receipt(state, scope)
                if receipt["state"] != "queued":
                    return {"receipt": receipt, **({"error": "write_indeterminate", "retry_allowed": False}
                                                  if receipt["state"] != "committed" else {})}
            artifact = self.capture(state, scope)
            require(artifact["calls"] < scope["max_calls"], "call_limit")
            used = [item["tool"] for item in artifact["outcomes"]]
            if tool == "brief":
                require("brief" not in used, "brief_limit")
            if scope["purpose"] == "both" and tool != "brief":
                require(not any(name != "brief" for name in used), "history_limit")
            artifact["calls"] += 1
            artifact["outcomes"].append({"tool": tool, "ok": False})
            artifact_file = request_artifact(state, scope["request_id"])
            save_json(artifact_file, artifact)
            # A queued write remains safely queued until all pre-mutation validation passed.
            require(self.gate(cfg) == scope, "scope_changed")
            if receipt is not None:
                receipt = {key: receipt[key] for key in ("schema", "request_id", "operation", "payload_hash", "store_identity")}
                receipt["state"] = "executing"
                save_json(receipt_path(state, scope["request_id"]), receipt)
            try:
                require(self.gate(cfg) == scope, "scope_changed")
                actor = "gateway-" + hashlib.sha256((cfg["owner"] + ":" + scope["request_id"]).encode()).hexdigest()
                # Raw API diagnostics must not reach the MCP transport or persistent logs.
                with open(os.devnull, "w") as sink, redirect_stdout(sink), redirect_stderr(sink):
                    ledger = ledger_factory(store, actor)
                    with ledger.session():
                        require(self.gate(cfg) == scope, "scope_changed")
                        require(store_identity(store) == expected_store, "store_identity_mismatch")
                        method = METHODS[scope["operation"]] if receipt is not None else (
                            "as_of" if tool == "beliefs_as_of" else tool)
                        result = getattr(ledger, method)(**args)
                require(store_identity(store) == expected_store, "store_identity_mismatch")
                if receipt is not None:
                    result = compact_result(scope["operation"], result)
                else:
                    artifact["warnings"].extend(warnings_from(result))
                    pool, warnings = normalize(tool, result)
                    artifact["warnings"].extend(item for item in warnings if item not in artifact["warnings"])
                    require(all(ref not in artifact["pool"] or artifact["pool"][ref] == item
                                for ref, item in pool.items()), "evidence_reference_conflict")
                    for ref, item in pool.items():
                        candidate = {**artifact, "pool": {**artifact["pool"], ref: item}}
                        if len(json_bytes(candidate)) > MAX_JSON - 16384:
                            artifact["warnings"].append("incomplete: whole evidence record omitted at capture size limit: " + ref)
                        else:
                            artifact["pool"][ref] = item
            except BaseException:
                if receipt is not None:
                    uncertain = {**receipt, "state": "indeterminate"}
                    try:
                        save_json(receipt_path(state, scope["request_id"]), uncertain)
                    except Exception:
                        pass  # Executing is itself durable uncertainty; never retry.
                    return {"receipt": load_json(receipt_path(state, scope["request_id"])),
                            "error": "write_indeterminate", "retry_allowed": False}
                artifact["warnings"].append("incomplete: backend read failed or evidence provenance conflicted")
                save_json(artifact_file, artifact)
                raise ValueError("backend_read_failed") from None
            if receipt is not None:
                committed = {**receipt, "state": "committed", "result": result}
                # A save failure here must retain executing, not invent a successful receipt.
                try:
                    save_json(receipt_path(state, scope["request_id"]), committed)
                except Exception:
                    # Directory fsync can fail AFTER rename. Revert the visible receipt
                    # conservatively when possible; never execute the Ledger call again.
                    try:
                        if load_json(receipt_path(state, scope["request_id"])) != receipt:
                            save_json(receipt_path(state, scope["request_id"]), receipt)
                    except Exception:
                        pass
                    raise ValueError("receipt_commit_unconfirmed") from None
                artifact["outcomes"][-1]["ok"] = True
                save_json(artifact_file, artifact)
                return {"receipt": committed}
            artifact["outcomes"][-1]["ok"] = True
            save_json(artifact_file, artifact)
            # Same canonical objects, whole records only. Controller hydrates from the full capture.
            selected, used_chars = {}, 0
            for ref, item in artifact["pool"].items():
                size = len(json_bytes(item))
                if used_chars + size <= 12000:
                    selected[ref] = item
                    used_chars += size
            if len(selected) != len(artifact["pool"]):
                artifact["warnings"].append("incomplete: whole evidence records omitted from bounded selector response")
                save_json(artifact_file, artifact)
            return {"pool": selected, "refs": list(selected), "warnings": artifact["warnings"]}


def make_server(settings):
    """Use raw tool arguments: SDK coercion/extra-field dropping must not weaken gates."""
    import anyio
    from mcp.server.lowlevel import Server
    from mcp.types import CallToolResult, ListToolsResult, TextContent, Tool, ToolAnnotations

    backend = Backend(settings)
    schemas = {
        "brief": ({}, []),
        "search_documents": ({"query": {"type": "string", "minLength": 1, "maxLength": 600,
                                        "description": "Short approved keywords; include informative project words. No limits or paths."}}, ["query"]),
        "history": ({"fact_id": {"type": "integer", "minimum": 1}}, ["fact_id"]),
        "beliefs_as_of": ({"when": {"type": "string", "maxLength": 80}}, ["when"]),
        "apply_approved_write": ({"request_id": {"type": "string", "maxLength": 128}}, ["request_id"]),
    }

    async def list_tools(ctx, params):
        return ListToolsResult(tools=[Tool(name=name, description="Private scope-gated " + name,
            input_schema={"type": "object", "properties": props, "required": required, "additionalProperties": False},
            annotations=ToolAnnotations(read_only_hint=name != "apply_approved_write", destructive_hint=False,
                                        idempotent_hint=name == "apply_approved_write", open_world_hint=False))
            for name, (props, required) in schemas.items()])

    async def call_tool(ctx, params):
        try:
            arguments = {} if params.arguments is None else params.arguments
            result = await anyio.to_thread.run_sync(backend.call, params.name, arguments)
            return CallToolResult(content=[TextContent(type="text", text=json_bytes(result).decode())])
        except Exception:
            return CallToolResult(content=[TextContent(type="text", text='{"error":"backend_refused"}')], is_error=True)

    return Server("ledger", on_list_tools=list_tools, on_call_tool=call_tool)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--settings", required=True)
    args = parser.parse_args()
    os.umask(0o077)
    logging.disable(logging.CRITICAL)
    try:
        restore_owner_environment()
    except ValueError:
        print('{"error":"invalid_owner_environment"}', file=sys.stderr)
        return 1
    import anyio
    from mcp.server.stdio import stdio_server

    server = make_server(args.settings)

    async def run():
        async with stdio_server() as (incoming, outgoing):
            await server.run(incoming, outgoing, server.create_initialization_options())

    anyio.run(run)


if __name__ == "__main__":
    raise SystemExit(main())
