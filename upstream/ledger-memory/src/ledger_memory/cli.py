"""`ledger` — the human-facing command line for ledger-memory.

Writes made here are attributed to the OWNER (the human), not to the agent. Output is JSON by default
so it can be piped; `--plain` prints a readable summary for the common commands.
"""
from __future__ import annotations

import contextlib

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from ledger_memory.api import KNOWLEDGE_TYPES, SOURCES, Ledger, LedgerError
from ledger_memory import config, locking, semantic_rebuild
from ledger_memory.reader import ReaderError
from ledger_memory.embedding_recipe import RECIPE_CONFIG_KEY, EmbeddingRecipe

SETTABLE = {"context.tool_reads_max", "context.briefing_budget_tokens"}


def _out(obj: Any, plain: bool = False) -> None:
    if plain and isinstance(obj, dict) and "results" in obj:
        for r in obj["results"]:
            label = r.get("statement") or r.get("title") or r.get("text") or ""
            print(f"[{r.get('type', 'doc')}:{r.get('id', r.get('document_id'))}] {label}  ({', '.join(r.get('lanes', [])) or r.get('uri', '')})")
        for w in obj.get("warnings", []):
            print(f"warning: {w}", file=sys.stderr)
        return
    print(json.dumps(obj, indent=2, default=str, ensure_ascii=False))


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="ledger", description="Append-only, auditable memory for AI agents.")
    p.add_argument("--home", help="memory location (default: $LEDGER_HOME or ~/.ledger-memory)")
    p.add_argument("--plain", action="store_true", help="readable output instead of JSON")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="create the memory store (safe to re-run)")
    sub.add_parser("status", help="counts, location, index health")

    s = sub.add_parser("remember", help="store a fact (attributed to you, the owner)")
    s.add_argument("statement"); s.add_argument("--source", choices=sorted(SOURCES), default="user_said")
    s.add_argument("--kind", default="note"); s.add_argument("--confidence", type=float)
    s.add_argument("--document-id", type=int)

    s = sub.add_parser("recall", help="search memory"); s.add_argument("query"); s.add_argument("--limit", type=int, default=8)

    s = sub.add_parser("revise", help="replace a fact with a corrected version (reason required)")
    s.add_argument("fact_id", type=int); s.add_argument("statement"); s.add_argument("--reason", required=True)
    s.add_argument("--source", choices=["document", "inferred", "observed", "user_said"],
                   help="where the corrected statement comes from (default: kept from the old version)")
    s.add_argument("--document-id", type=int, dest="document_id", help="the ingested document backing it (with --source document)")

    s = sub.add_parser("dispute", help="flag a fact as contested"); s.add_argument("fact_id", type=int)
    s.add_argument("--reason", required=True); s.add_argument("--conflicts-with", type=int, help="the other fact it contradicts")

    s = sub.add_parser("brief", help="the budgeted briefing for one question")
    s.add_argument("question"); s.add_argument("--budget", type=int, default=1500)
    s = sub.add_parser("recall-more", help="one bounded extra look-up (a fresh process starts a fresh allowance)")
    s.add_argument("query")
    s = sub.add_parser("config", help="show or set a setting: context.tool_reads_max (1-5, default 3), "
                                       "context.briefing_budget_tokens")
    s.add_argument("key"); s.add_argument("value", nargs="?")

    s = sub.add_parser("history", help="every version of a fact"); s.add_argument("fact_id", type=int)
    s = sub.add_parser("as-of", help="beliefs at a past moment (ISO-8601 UTC)"); s.add_argument("when")
    s = sub.add_parser("beliefs", help="current facts"); s.add_argument("--kind"); s.add_argument("--limit", type=int, default=50)

    s = sub.add_parser("decide", help="record a decision"); s.add_argument("title")
    s.add_argument("--summary", required=True); s.add_argument("--rationale", required=True)
    s.add_argument("--status", default="accepted"); s.add_argument("--fact-id", type=int)

    s = sub.add_parser("ingest", help="store a text document"); s.add_argument("path")
    s.add_argument("--uri"); s.add_argument("--media-type", default="text/plain")
    s = sub.add_parser("search-docs", help="search inside documents"); s.add_argument("query")
    s.add_argument("--limit", type=int, default=5)

    sub.add_parser("reindex", help="bring stale search indexes up to date")
    sub.add_parser("check", help="integrity check")
    sub.add_parser("backup", help="checksummed, compressed backup into <home>/backups")
    s = sub.add_parser("restore", help="restore a backup to another file, or over the live memory with --force (refused while any session uses it; refuses on checksum mismatch)")
    s.add_argument("archive"); s.add_argument("--out", required=True); s.add_argument("--force", action="store_true")

    s = sub.add_parser("semantic", help="manage opt-in local meaning-based search")
    s.add_argument("state", choices=["enable", "on", "off", "disable", "status", "import"])
    s.add_argument("directory", nargs="?")
    s.add_argument("--model", choices=["bge-m3"], default=None, help="pinned local embedding model")

    s = sub.add_parser("acceptance", help="write an offline synthetic acceptance report")
    s.add_argument("--out", required=True)
    sub.add_parser("serve", help="run the MCP server on stdio (what the agent launches)")
    s = sub.add_parser("selftest", help="exercise every feature on a throwaway store and report")
    s.add_argument("--offline", action="store_true",
                   help="force keyword-only synthetic checks without contacting a model service")
    return p


def _semantic_status(L: Ledger) -> dict[str, Any]:
    from ledger_memory.semantic_readiness import SemanticReadiness, readiness

    if L.db_path.exists():
        report = readiness(L.db_path, L.vectors_path)
    elif os.environ.get("LEDGER_EMBEDDING_BACKEND", "none") in ("", "none"):
        report = SemanticReadiness(False, False, False, None, 0, 0, state="disabled")
    else:
        report = SemanticReadiness(False, False, False, "memory store unavailable")
    reason = report.reason
    # Missing inference libraries show up as the generic "worker unavailable"; only that reason is made specific.
    if (report.state == "degraded" and report.reason in (None, "embedding worker readiness unavailable")
            and not _semantic_dependencies_present()):
        reason = "semantic dependencies unavailable; reinstall with the semantic runtime (see README)"
    return {
        "semantic": "off" if report.state == "disabled" else "on",
        "state": report.state,
        "reason": reason,
        "pending_rows": report.pending_rows,
        "failed_rows": report.failed_rows,
        "model_fingerprint": report.model_fingerprint,
        "worker_running": report.worker_running,
    }


def _semantic_dependencies_present() -> bool:
    """Whether the optional inference libraries are importable, without importing them."""
    from importlib.util import find_spec
    return all(find_spec(name) is not None for name in ("numpy", "onnxruntime", "tokenizers", "truststore"))


def _private_profile():
    """Resolve the per-user service folder and create it owner-private (0700) before any
    model cache is created inside it: a cache mkdir would otherwise create it with default
    permissions, and the worker refuses a folder that is not private."""
    from ledger_memory.embedding_worker import _private_directory, worker_profile
    profile = worker_profile()
    profile.root.mkdir(parents=True, exist_ok=True, mode=0o700)
    _private_directory(profile.root)  # refuse an existing non-private folder before any download
    return profile


def _enable_semantic(L: Ledger) -> dict[str, Any]:
    from ledger_memory import locking
    from ledger_memory.embedding_recipe import activate_onnx_backend
    from ledger_memory.embedding_worker import WorkerProtocolError, ensure_worker
    from ledger_memory.model_artifacts import (
        ModelArtifactError, _manifest, download_model_bundle, model_bundle_path, model_cache_path, verify_model,
    )
    from ledger_memory.semantic_binding import foreign_binding, read_receipt
    from ledger_memory.semantic_pending import request_drain, reset_embedding_failures

    try:
        with locking.exclusive(L.home / "semantic-switch.lock", timeout=120):
            if foreign_binding(L.db_path, L.vectors_path) is not None:
                raise LedgerError("semantic enable refused: sidecar belongs to another database")
            before_backend = config.get_config(L.db_path, "embedding.backend")
            before_recipe = config.get_config(L.db_path, RECIPE_CONFIG_KEY)
            profile = _private_profile()
            manifest, fingerprint = _manifest(None)
            try:
                verified = verify_model(model_bundle_path(profile.root))
            except ModelArtifactError:
                verified = download_model_bundle(model_cache_path(profile.root))
            if verified.model_fingerprint != fingerprint:
                raise LedgerError("semantic artifact fingerprint unavailable")
            ensure_worker(profile=profile)
            recipe = EmbeddingRecipe(
                artifact="BAAI/bge-m3", revision_hash=fingerprint, query_prefix="", document_prefix="",
                pooling="dense-cls-token-weighted-segments", normalization="l2",
                dimensions=manifest["dimensions"], max_tokens_policy="8192-including-specials-full-source-segments",
                runtime="onnxruntime-cpu")
            activate_onnx_backend(L.db_path, recipe, expected_backend=before_backend,
                                  expected_recipe=before_recipe, actor_id=L.owner_id)
            receipt = read_receipt(L.vectors_path)
            model = f"bge-m3:onnx:{fingerprint}"
            if receipt is None or receipt.embedding_model != model or receipt.dimensions != manifest["dimensions"]:
                replacement = semantic_rebuild.replace_active_model_rebuild(
                    L.db_path, sidecar_path=L.vectors_path, require_owned=True)
                if not replacement.receipt_verified:
                    raise LedgerError("semantic generation verification unavailable")
            reset_embedding_failures(L.vectors_path, model)  # an explicit enable retries earlier failures
            request_drain(L.db_path, L.vectors_path)
            return _semantic_status(L)
    except LedgerError:
        raise
    except (ModelArtifactError, WorkerProtocolError) as exc:
        # Both carry fixed, content-free messages (no paths, text or credentials); show the actionable one.
        raise LedgerError(f"semantic enable incomplete: {exc}; keyword retrieval remains available") from None
    except Exception:
        raise LedgerError("semantic enable incomplete; keyword retrieval remains available; run ledger semantic status") from None


# Every command that opens the memory holds the shared session lock while it runs, so an in-place restore can see it.
SESSION_WAIT_SECONDS = 30


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.cmd == "semantic":
        if (args.state == "import") != (args.directory is not None):
            parser.error("only semantic import requires a directory")
        if args.model is not None and args.state not in ("on", "enable"):
            parser.error("--model is only valid with semantic enable/on")
    if args.cmd == "serve":
        from ledger_memory.mcp_server import main as serve
        serve(); return 0
    if args.cmd == "selftest":
        from ledger_memory.selftest import run
        return run(offline=args.offline)
    try:
        if args.cmd == "acceptance":
            from ledger_memory.acceptance import run
            result = run(args.out)
            _out(result, args.plain)
            return 1 if result["status"] == "failed" else 0
        if args.cmd == "semantic" and args.state == "status":
            # Read-only and guaranteed to create nothing, so it does not register a session (that would create the
            # lock file); a restore may replace the file while it reads, which can only make the answer stale.
            _out(_semantic_status(Ledger(args.home)), args.plain)
            return 0
        if (args.cmd == "semantic" and args.state in ("disable", "off")
                and os.environ.get("LEDGER_EMBEDDING_BACKEND", "none") not in ("", "none")):
            raise LedgerError("cannot disable an environment-selected backend; unset LEDGER_EMBEDDING_BACKEND first")
        if (args.cmd == "semantic" and args.state in ("enable", "on")
                and os.environ.get("LEDGER_EMBEDDING_BACKEND") not in (None, "", "bge-m3")):
            raise LedgerError("cannot enable local semantics under this environment override; unset LEDGER_EMBEDDING_BACKEND first")
        if args.cmd == "semantic" and args.state == "import":
            from ledger_memory.embedding_worker import worker_profile
            from ledger_memory.model_artifacts import import_model_bundle, model_cache_path, ModelArtifactError

            try:
                verified = import_model_bundle(Path(args.directory), model_cache_path(_private_profile().root))
            except (ModelArtifactError, OSError, ValueError, RuntimeError):
                raise LedgerError("model bundle import unavailable; verify the pinned artifacts and cache access") from None
            _out({"imported_files": list(verified.files), "model_fingerprint": verified.model_fingerprint,
                  "enabled": False}, args.plain)
            return 0
        L = Ledger(args.home)
        with contextlib.ExitStack() as session:
            if args.cmd != "restore":   # a restore takes the session lock exclusively itself; this process must not hold it
                try:
                    session.enter_context(locking.shared(locking.session_lock_path(L.db_path),
                                                         timeout=SESSION_WAIT_SECONDS))
                except TimeoutError:
                    raise LedgerError("this memory is being restored; retry in a moment") from None
            L = L.ensure()
            result = _dispatch(L, args)
    except (LedgerError, ReaderError) as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    _out(result, args.plain)
    return 0


def _dispatch(L: Ledger, a: argparse.Namespace) -> Any:
    c = a.cmd
    if c == "init":
        out = {"initialized": str(L.db_path), "index_types": KNOWLEDGE_TYPES}
        if L.last_upgrade:
            out["upgraded"] = L.last_upgrade
        return out
    if c == "status":
        return L.status()
    if c == "remember":
        return L.remember(a.statement, source=a.source, kind=a.kind, confidence=a.confidence,
                          document_id=a.document_id, as_owner=True)
    if c == "recall":
        return L.recall(a.query, limit=a.limit)
    if c == "revise":
        return L.revise(a.fact_id, a.statement, reason=a.reason, as_owner=True, source=a.source,
                        document_id=a.document_id)
    if c == "dispute":
        return L.dispute(a.fact_id, reason=a.reason, as_owner=True, conflicts_with=a.conflicts_with)
    if c == "brief":
        return L.brief(a.question, budget_tokens=a.budget)
    if c == "recall-more":
        return L.recall_more(a.query)
    if c == "config":
        if a.key not in SETTABLE:
            raise LedgerError(f"settable keys: {sorted(SETTABLE)}")
        if a.value is None:
            return {a.key: config.get_config(L.db_path, a.key)}
        if not a.value.strip().isdigit():
            raise LedgerError(f"{a.key} must be a whole number")
        config.set_config(L.db_path, key=a.key, value=a.value.strip(), actor_id=L.owner_id)
        return {a.key: a.value.strip()}
    if c == "history":
        return L.history(a.fact_id)
    if c == "as-of":
        return L.as_of(a.when)
    if c == "beliefs":
        return L.current_beliefs(kind=a.kind, limit=a.limit)
    if c == "decide":
        return L.record_decision(a.title, summary=a.summary, rationale=a.rationale, status=a.status,
                                 fact_id=a.fact_id, as_owner=True)
    if c == "ingest":
        return L.ingest(path=a.path, uri=a.uri, media_type=a.media_type, as_owner=True)
    if c == "search-docs":
        return L.search_documents(a.query, limit=a.limit)
    if c == "reindex":
        return L.refresh(repair=True)
    if c == "check":
        return L.check()
    if c == "backup":
        return L.backup()
    if c == "restore":
        return L.restore(a.archive, a.out, force=a.force)
    if c == "semantic":
        if a.state == "status":
            return _semantic_status(L)
        if a.state in ("off", "disable"):
            from ledger_memory.locking import exclusive
            try:
                with exclusive(L.home / "semantic-switch.lock", timeout=120):
                    config.set_config(L.db_path, key="embedding.backend", value="none", actor_id=L.owner_id)
            except TimeoutError:
                raise LedgerError("semantic policy change busy; retry after provisioning finishes") from None
            return _semantic_status(L)
        return _enable_semantic(L)
    raise LedgerError(f"unknown command {c}")


if __name__ == "__main__":
    sys.exit(main())
