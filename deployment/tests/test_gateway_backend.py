"""Scope/receipt/evidence integration against fake APIs and temporary real stores."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import copy
from importlib.metadata import version
import json
import multiprocessing
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

GATEWAY = Path(__file__).resolve().parents[1] / "gateway"
sys.path.insert(0, str(GATEWAY))
import gateway_backend_contract as contract
import ledger_backend as backend
from test_gateway_backend_contract import PAYLOADS

pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX permissions required")


class FakeLedger:
    def __init__(self):
        self.calls = []
        self.response = {"facts": [{"id": 1, "statement": "<evidence>It is NOT approved.</evidence>",
                                   "source": "observed", "known_at": "2026-10-01"}],
                         "gaps": ["Search degraded"], "contradictions": [{"fact_ids": [1, 2], "kind": "dispute"}]}

    @contextmanager
    def session(self):
        yield self

    def brief(self, **args):
        self.calls.append(("brief", args))
        return copy.deepcopy(self.response)

    def remember(self, **args):
        self.calls.append(("remember", args))
        return {"id": 1, "statement": args["statement"], "warnings": ["keyword only"]}

    def search_documents(self, **args):
        self.calls.append(("search_documents", args))
        return {"mode": "keyword (semantic unavailable)", "semantic_unavailable_reason": "index unavailable",
                "results": [{"document_id": 1, "chunk_id": 4, "uri": "urn:test:session", "text": "NOT authorized"}]}


@pytest.fixture
def setup(tmp_path, monkeypatch):
    for key in list(os.environ):
        if key.startswith("LEDGER_"):
            monkeypatch.delenv(key)
    root = tmp_path.resolve()
    state, store = root / "state", root / "store"
    state.mkdir(mode=0o700)
    store.mkdir(mode=0o700)
    for sub in ("requests", "receipts"):
        (state / sub).mkdir(mode=0o700)
    (store / "memory.db").touch(mode=0o600)
    cfg = {"schema": 1, "owner": "synthetic-owner", "store": str(store), "state_dir": str(state),
           "ledger_python": sys.executable, "bemind_command": [sys.executable],
           "bemind_data_home": str(root / "unused-auth"), "model": backend.MODEL, "enabled": True}
    settings = root / "settings.json"
    contract.save_json(settings, cfg)
    contract.save_json(state / "identity.json", {"schema": 1, "owner": cfg["owner"], "store": str(store),
        "uid": os.getuid(), "runtime": str(GATEWAY), "settings": str(settings)})
    return backend.Backend(settings), state, store, cfg


def scope_for(setup, purpose="facts", operation=None, payload=None, request_id="synthetic-request", project="global", query=None):
    instance, state, store, cfg = setup
    args = contract.validate_write(operation, payload) if operation else None
    scope = {"schema": 1, "owner": cfg["owner"], "nonce": "a" * 64, "request_id": request_id,
             "store_identity": contract.store_identity(store),
             "kind": "write" if operation else "read", "purpose": None if operation else purpose,
             "query": None if operation else (f"{project}: synthetic" if query is None else query),
             "project": None if operation else project, "session_id": "ses_synthetic",
             "agent": "memory-luna-" + ("write" if operation else purpose), "expires_at": time.time() + 120,
             "max_calls": 1 if operation or purpose == "facts" else 2, "budget_tokens": 800, "document_limit": 2,
             "operation": operation, "arguments": args, "payload_hash": contract.write_hash(operation, args) if operation else None}
    contract.save_json(state / "active-request.json", scope)
    contract.save_json(state / "verified-model.json", {"schema": 1, "model": "gpt-6-luna",
                       **{key: scope[key] for key in ("owner", "nonce", "request_id", "session_id")}})
    if operation:
        contract.save_json(contract.receipt_path(state, request_id), {"schema": 1, "request_id": request_id,
                           "operation": operation, "payload_hash": scope["payload_hash"], "state": "queued",
                           "store_identity": scope["store_identity"]})
    return scope


@pytest.fixture
def fake(monkeypatch):
    ledger = FakeLedger()
    monkeypatch.setattr(backend, "ledger_factory", lambda *args: ledger)
    return ledger


@pytest.mark.parametrize("tool", ["brief", "search_documents"])
@pytest.mark.parametrize("attack", ["no_scope", "no_marker", "model", "owner", "nonce", "session_id", "request_id",
                                    "expired", "zero", "identity", "settings_store", "max_calls"])
def test_scope_denials_before_api(setup, fake, attack, tool):
    instance, state, store, cfg = setup
    scope_for(setup, purpose="facts" if tool == "brief" else "history")
    if attack in ("no_scope", "no_marker"):
        (state / ("active-request.json" if attack == "no_scope" else "verified-model.json")).unlink()
    else:
        path = state / ("active-request.json" if attack in ("expired", "zero", "max_calls") else
                        "identity.json" if attack == "identity" else "verified-model.json")
        value = contract.load_json(path)
        if attack in ("expired", "zero"):
            value["expires_at"] = 0 if attack == "zero" else time.time() - 1
        elif attack == "max_calls":
            value["max_calls"] = 99
        elif attack == "identity":
            value["owner"] = "other-owner"
        elif attack == "settings_store":
            path = instance.settings
            value = {**cfg, "store": str(store.parent / "other-store")}
        else:
            value[attack] = "wrong-value"
        contract.save_json(path, value)
    with pytest.raises((ValueError, OSError)):
        instance.call(tool, {} if tool == "brief" else {"query": "synthetic"})
    assert fake.calls == []
    assert not list((state / "requests").iterdir())


def test_no_store_import_or_startup_writes(setup):
    instance, state, store, cfg = setup
    before = {p.relative_to(store): p.stat().st_mtime_ns for p in store.rglob("*")}
    script = "import sys; sys.path.insert(0,sys.argv[1]); import ledger_backend; ledger_backend.make_server(sys.argv[2]); assert 'ledger_memory.api' not in sys.modules"
    result = subprocess.run([sys.executable, "-I", "-B", "-c", script, str(GATEWAY), str(instance.settings)],
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert before == {p.relative_to(store): p.stat().st_mtime_ns for p in store.rglob("*")}
    assert not (state / "backend-operation.lock").exists()


@pytest.mark.parametrize("overrides,home,cache", [
    ({"LEDGER_GATEWAY_OWNER_HOME": "/synthetic-owner", "LEDGER_GATEWAY_OWNER_CACHE": "null"},
     "/synthetic-owner", None),
    ({"LEDGER_GATEWAY_OWNER_HOME": "/synthetic-owner", "LEDGER_GATEWAY_OWNER_CACHE": '"/synthetic-owner-cache"'},
     "/synthetic-owner", "/synthetic-owner-cache"),
    ({"LEDGER_GATEWAY_OWNER_HOME": "/synthetic owner", "LEDGER_GATEWAY_OWNER_CACHE": '"/synthetic owner cache"'},
     "/synthetic owner", "/synthetic owner cache"),
    ({"LEDGER_GATEWAY_OWNER_HOME": "/synthetic-owner"}, "/synthetic-owner", "/synthetic-private-cache"),
    ({"LEDGER_GATEWAY_OWNER_CACHE": "null"}, "/synthetic-private-home", None),
    ({}, "/synthetic-private-home", "/synthetic-private-cache"),
])
def test_owner_cache_restored_only_in_backend_entry_process(setup, overrides, home, cache):
    instance, state, store, cfg = setup
    parent_env = dict(os.environ)
    env = {**parent_env, "HOME": "/synthetic-private-home", "XDG_CACHE_HOME": "/synthetic-private-cache", **overrides}
    before = {p: p.stat().st_mtime_ns for p in store.rglob("*")}
    script = '''
import json, os, sys
sys.path.insert(0, sys.argv[1])
settings = sys.argv[2]
store = sys.argv[3]
def no_store_open(event, args):
    if event == "open" and isinstance(args[0], str):
        assert args[0] != store and not args[0].startswith(store + os.sep), "startup opened store"
sys.addaudithook(no_store_open)
before = dict(os.environ)
import ledger_backend
assert dict(os.environ) == before  # Importing into a caller must not alter its environment.
assert "ledger_memory.api" not in sys.modules
def inspect_startup(path):
    assert path == settings
    ledger_backend.Backend(path)
    assert "ledger_memory.api" not in sys.modules
    from ledger_memory import embedding_worker
    roots = {}
    original = sys.platform
    try:
        for platform in ("darwin", "linux"):
            sys.platform = platform
            roots[platform] = str(embedding_worker.worker_profile().root)
    finally:
        sys.platform = original
    assert "ledger_memory.api" not in sys.modules
    print(json.dumps({"home": os.environ.get("HOME"), "cache": os.environ.get("XDG_CACHE_HOME"), "roots": roots}))
    raise SystemExit(0)
ledger_backend.make_server = inspect_startup
sys.argv = ["ledger_backend.py", "--settings", settings]
ledger_backend.main()
'''
    result = subprocess.run([sys.executable, "-I", "-B", "-c", script, str(GATEWAY), str(instance.settings), str(store)],
                            env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    value = json.loads(result.stdout)
    assert value == {"home": home, "cache": cache, "roots": {
        "darwin": str(Path(home) / "Library" / "Caches" / "ledger-memory"),
        "linux": str((Path(cache) if cache is not None else Path(home) / ".cache") / "ledger-memory")}}
    assert dict(os.environ) == parent_env
    assert before == {p: p.stat().st_mtime_ns for p in store.rglob("*")}
    assert not (state / "backend-operation.lock").exists()


@pytest.mark.parametrize("overrides", [
    {"LEDGER_GATEWAY_OWNER_HOME": "relative-owner"}, {"LEDGER_GATEWAY_OWNER_HOME": ""},
    {"LEDGER_GATEWAY_OWNER_HOME": "/synthetic-owner/../elsewhere"},
    {"LEDGER_GATEWAY_OWNER_HOME": "/synthetic-owner\nprivate"},
    {"LEDGER_GATEWAY_OWNER_CACHE": "not-json-private-value"}, {"LEDGER_GATEWAY_OWNER_CACHE": "true"},
    {"LEDGER_GATEWAY_OWNER_CACHE": "42"}, {"LEDGER_GATEWAY_OWNER_CACHE": "[]"},
    {"LEDGER_GATEWAY_OWNER_CACHE": '""'}, {"LEDGER_GATEWAY_OWNER_CACHE": '"relative-cache"'},
    {"LEDGER_GATEWAY_OWNER_CACHE": '"/synthetic-cache/../elsewhere"'},
    {"LEDGER_GATEWAY_OWNER_CACHE": '"/synthetic-cache\\u0000private"'},
    {"LEDGER_GATEWAY_OWNER_CACHE": '"/synthetic-cache\\ud800private"'},
])
def test_invalid_owner_environment_fails_without_logging_values(setup, overrides):
    instance, state, store, cfg = setup
    env = {**os.environ, "HOME": "/synthetic-private-home", "XDG_CACHE_HOME": "/synthetic-private-cache",
           "LEDGER_GATEWAY_OWNER_HOME": "/synthetic-owner", "LEDGER_GATEWAY_OWNER_CACHE": "null", **overrides}
    before = {p: p.stat().st_mtime_ns for p in store.rglob("*")}
    result = subprocess.run([sys.executable, "-I", "-B", str(GATEWAY / "ledger_backend.py"),
                             "--settings", str(instance.settings)], env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 1 and result.stdout == ""
    assert result.stderr.strip() == '{"error":"invalid_owner_environment"}'
    assert before == {p: p.stat().st_mtime_ns for p in store.rglob("*")}
    assert not (state / "backend-operation.lock").exists()


def test_owner_environment_validation_is_atomic(setup):
    env = {**os.environ, "HOME": "/synthetic-private-home", "XDG_CACHE_HOME": "/synthetic-private-cache",
           "LEDGER_GATEWAY_OWNER_HOME": "/synthetic-owner", "LEDGER_GATEWAY_OWNER_CACHE": '"relative-cache"'}
    script = '''
import os, sys
sys.path.insert(0, sys.argv[1])
import ledger_backend
before = dict(os.environ)
try:
    ledger_backend.restore_owner_environment()
except ValueError as error:
    assert str(error) == "invalid_owner_environment"
else:
    raise AssertionError("invalid cache accepted")
assert dict(os.environ) == before
assert "ledger_memory.api" not in sys.modules
'''
    result = subprocess.run([sys.executable, "-I", "-B", "-c", script, str(GATEWAY)], env=env,
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr


def test_brief_capture_canonical_and_warnings(setup, fake):
    instance, state, _, _ = setup
    scope = scope_for(setup)
    result = instance.call("brief", {})
    assert fake.calls == [("brief", {"question": scope["query"], "budget_tokens": 800})]
    artifact = contract.load_json(contract.request_artifact(state, scope["request_id"]))
    assert result["pool"] == artifact["pool"]
    assert result["pool"]["fact:1"]["statement"] == fake.response["facts"][0]["statement"]
    assert "Search degraded" in result["warnings"]
    assert {"contradictions": fake.response["contradictions"]} in result["warnings"]
    assert artifact["calls"] == 1 and artifact["outcomes"] == [{"tool": "brief", "ok": True}]
    with pytest.raises(ValueError, match="call_limit"):
        instance.call("brief", {})


@pytest.mark.parametrize("tool,args", [
    ("brief", {"question": "global: synthetic"}), ("brief", {"budget_tokens": 800}),
    ("brief", {"question": "global: synthetic", "budget_tokens": 800}),
    ("brief", {"question": "different"}), ("brief", {"question": "global: synthetic", "budget_tokens": True}),
    ("brief", {"question": "global: synthetic", "budget_tokens": 801}),
    ("brief", {"question": "global: synthetic", "extra": "ignored?"}),
    ("brief", {"question": "global: synthetic", "budget_tokens": "800"}),
    ("search_documents", {"query": "global: synthetic", "limit": 0}),
    ("search_documents", {}), ("search_documents", {"limit": 2}),
    ("search_documents", {"query": "global: synthetic", "limit": 2}),
    ("search_documents", {"query": "other", "limit": 2}),
    ("history", {"fact_id": True}), ("beliefs_as_of", {"when": "yesterday"}),
    ("beliefs_as_of", {"when": "2026-10-01", "limit": 2}),
])
def test_invalid_tool_args(setup, fake, tool, args):
    scope_for(setup, purpose="both")
    with pytest.raises(ValueError):
        setup[0].call(tool, args)
    assert fake.calls == []


@pytest.mark.parametrize("tool", ["brief", "search_documents"])
@pytest.mark.parametrize("args", [None, [], "", False, 0, {"ignored": None}])
def test_scoped_readers_reject_invalid_argument_shapes(setup, fake, tool, args):
    scope_for(setup, purpose="both")
    with pytest.raises(ValueError, match="invalid_arguments"):
        setup[0].call(tool, args)
    assert not fake.calls


@pytest.mark.parametrize("field,value", [("budget_tokens", 799), ("budget_tokens", True),
                                         ("document_limit", 1), ("document_limit", 3)])
def test_scoped_readers_still_require_fixed_scope_caps(setup, fake, field, value):
    scope = scope_for(setup, purpose="both")
    contract.save_json(setup[1] / "active-request.json", {**scope, field: value})
    for tool in ("brief", "search_documents"):
        with pytest.raises(ValueError, match="invalid_scope"):
            setup[0].call(tool, {} if tool == "brief" else {"query": "synthetic"})
    assert not fake.calls


def test_read_cannot_write_and_facts_cannot_history(setup, fake):
    scope = scope_for(setup)
    with pytest.raises(ValueError):
        setup[0].call("apply_approved_write", {"request_id": scope["request_id"]})
    with pytest.raises(ValueError):
        setup[0].call("search_documents", {"query": "synthetic"})
    assert not fake.calls


def test_both_one_brief_one_history(setup, fake):
    scope = scope_for(setup, purpose="both")
    instance = setup[0]
    instance.call("search_documents", {"query": "synthetic"})
    assert fake.calls == [("search_documents", {"query": "synthetic", "limit": 2})]
    with pytest.raises(ValueError, match="history_limit"):
        instance.call("search_documents", {"query": "synthetic"})
    instance.call("brief", {})
    assert len(fake.calls) == 2


def test_history_two_calls_no_brief(setup, fake):
    scope = scope_for(setup, purpose="history")
    instance = setup[0]
    with pytest.raises(ValueError):
        instance.call("brief", {})
    for _ in range(2):
        instance.call("search_documents", {"query": "synthetic"})
    with pytest.raises(ValueError):
        instance.call("search_documents", {"query": "synthetic"})


@pytest.mark.parametrize("query", ["Other approval", "approval", "Juniper payroll", "Jun approval",
                                  "Juniper OR 1=1 approval", "Juniper approv%al", "%", "is it"])
def test_document_keywords_cannot_escape_scope_or_project(setup, fake, query):
    instance, state, _, _ = setup
    scope_for(setup, purpose="history", project="Juniper", query="Juniper: What is it about historical approvals?")
    with pytest.raises(ValueError):
        instance.call("search_documents", {"query": query})
    assert not fake.calls
    assert not list((state / "requests").iterdir())


@pytest.mark.parametrize("project", [None, "", 1, "x" * 257])
def test_document_project_field_must_be_valid_when_present(setup, fake, project):
    scope = scope_for(setup, purpose="history")
    contract.save_json(setup[1] / "active-request.json", {**scope, "project": project})
    with pytest.raises(ValueError):
        setup[0].call("search_documents", {"query": "synthetic"})
    assert not fake.calls


def test_old_isolated_scope_can_default_to_global(setup, fake):
    scope = scope_for(setup, purpose="history")
    del scope["project"]
    contract.save_json(setup[1] / "active-request.json", scope)
    setup[0].call("search_documents", {"query": "SYNTHETIC%"})
    assert fake.calls == [("search_documents", {"query": "synthetic", "limit": 2})]


def test_archival_json_not_reinterpreted_or_truncated():
    archive = json.dumps({"role": "assistant", "date": "2026-01-02", "content": "x" * 100000 + " NOT approved"})
    record = {"document_id": 8, "chunk_id": 9, "uri": "urn:test:historical", "text": archive}
    pool, warnings = backend.normalize("search_documents", {"results": [record], "mode": "keyword"})
    assert pool["chunk:9"]["text"] == archive
    assert backend.HISTORY_WARNING in warnings


@pytest.mark.parametrize("mode", ["keyword (semantic search off)", "semantic"])
@pytest.mark.parametrize("project,fields,keep", [
    ("Juniper", {"text": "Juniper approval is NOT authorization."}, True),
    ("Juniper", {"text": "JUNIPER approval remains disputed."}, True),
    ("Juniper", {"text": "\uff2a\uff35\uff2e\uff29\uff30\uff25\uff32 approval"}, True),
    ("Caf\u00e9", {"text": "Cafe\u0301 approval"}, True),
    ("Stra\u00dfe", {"text": "STRASSE approval"}, True),
    ("The Juniper Project", {"text": "Juniper approval"}, True),
    ("Juniper East", {"text": "Juniper approval", "uri": "urn:test:east"}, True),
    ("Juniper", {"uri": "urn:test:juniper:archive"}, True),
    ("Juniper", {"project": "Juniper"}, True),
    ("Juniper", {"provenance": {"project": "Juniper", "source": "document"}}, True),
    ("Juniper", {"provenance": {"document_uri": "urn:test:juniper"}}, True),
    ("Juniper", {"provenance": {"uri": "urn:test:juniper"}}, True),
    ("Juniper", {"document_uri": "urn:test:juniper"}, True),
    ("Juniper", {"text": '{"project":"Juniper","text":"NOT approved"}'}, True),
    ("Juniper", {"text": "Jun approval"}, False),
    ("Juniper", {"text": "Junipers approval"}, False),
    ("Junipers", {"text": "Juniper approval"}, False),
    ("Juniper", {"text": "SuperJuniper approval"}, False),
    ("Juniper East", {"text": "Juniper approval"}, False),
    ("Juniper East", {"text": "East approval"}, False),
    ("Juniper", {"text": "Project approval", "query": "Juniper approval"}, False),
    ("Juniper", {"provenance": {"query": "Juniper", "author": "Juniper"}}, False),
    ("Juniper", {"provenance": {"project": ["Juniper"]}}, False),
    ("Juniper", {"warnings": ["Juniper: rejected-record-warning"]}, False),
    ("HR", {"text": "HR approval"}, True),
    ("HR", {"text": "\uff28\uff32 approval"}, True),
    ("HR", {"text": "sharepolicy approval"}, False),
    ("HR", {"text": "thread approval"}, False),
    ("HR", {"text": "HRS approval"}, False),
    ("QA", {"text": "QA approval"}, True),
    ("QA", {"text": "Qatar approval"}, False),
    ("HR QA", {"text": "HR QA approval"}, True),
    ("HR QA", {"text": "HR approval"}, False),
    ("X", {"text": "X approval"}, True),
    ("X", {"text": "Extra approval"}, False),
    ("global", {"text": "Other project approval"}, True),
    ("\uff27\uff2c\uff2f\uff22\uff21\uff2c", {"text": "Other project approval"}, True),
])
def test_document_filter_before_capture_preserves_whole_evidence(setup, fake, monkeypatch, mode, project, fields, keep):
    instance, state, _, _ = setup
    record = {"document_id": 7, "chunk_id": 8, "text": "Synthetic approval", **fields}
    response = {"query": project + " approval", "mode": mode, "results": [record],
                "semantic_unavailable_reason": "index unavailable"}
    before = copy.deepcopy(response)
    def search(**args):
        fake.calls.append(("search_documents", args))
        return response
    monkeypatch.setattr(fake, "search_documents", search)
    scope = scope_for(setup, purpose="history", project=project, query=project + " approval")
    result = instance.call("search_documents", {"query": project + " approval"})
    captured = contract.load_json(contract.request_artifact(state, scope["request_id"]))
    assert response == before
    assert result["pool"] == captured["pool"]
    assert result["warnings"] == captured["warnings"]
    assert len(fake.calls) == 1 and fake.calls[0][1]["limit"] == 2
    assert captured["calls"] == 1 and captured["outcomes"] == [{"tool": "search_documents", "ok": True}]
    assert backend.HISTORY_WARNING in result["warnings"]
    assert {"semantic_unavailable_reason": "index unavailable"} in result["warnings"]
    if keep:
        assert result["pool"]["chunk:8"] == {**record, "ref": "chunk:8", "type": "chunk", "id": 8}
        assert not any("missing project words" in str(w) for w in result["warnings"])
    else:
        assert result["pool"] == {} and result["refs"] == []
        assert any("incomplete:" in str(w) and "already-limited" in str(w) for w in result["warnings"])
        assert "rejected-record-warning" not in json.dumps([result, captured])
        assert record["text"] not in json.dumps([result, captured])


@pytest.mark.parametrize("fields,missing", [
    ({"known_at": "2026-01-01", "source": "observed", "still_current": True}, []),
    ({"known_at": "2026-01-01", "source": "inferred", "still_current": False, "superseded_by": 2}, []),
    ({"known_at": None, "source": "unclassified", "still_current": True, "superseded_by": None}, []),
    ({}, ["known_at", "source", "still_current"]),
    ({"source": "observed", "still_current": True}, ["known_at"]),
    ({"known_at": "2026-01-01", "still_current": True}, ["source"]),
    ({"known_at": None, "source": "unclassified"}, ["still_current"]),
    ({"known_at": "2026-01-01", "source": "observed", "still_current": False}, ["superseded_by"]),
])
def test_asof_field_aware_warnings_and_values_preserved(setup, fake, monkeypatch, fields, missing):
    instance, state, _, _ = setup
    record = {"id": 1, "statement": "Historical claim, NOT authorization", "kind": "note", **fields}
    response = {"as_of": "2026-01-01", "beliefs": [record]}
    before = copy.deepcopy(response)
    def as_of(**args):
        fake.calls.append(("as_of", args))
        return response
    monkeypatch.setattr(fake, "as_of", as_of, raising=False)
    scope = scope_for(setup, purpose="history")
    result = instance.call("beliefs_as_of", {"when": "2026-01-01"})
    captured = contract.load_json(contract.request_artifact(state, scope["request_id"]))
    assert response == before
    assert result["pool"] == captured["pool"]
    item = result["pool"]["fact:1"]
    assert item == {**record, "ref": "fact:1", "type": "fact", "as_of": "2026-01-01"}
    if "still_current" in fields:
        assert item["still_current"] is fields["still_current"]
        assert any("absence of disputes" in str(w) for w in result["warnings"])
    assert "status" not in item
    assert backend.HISTORY_WARNING in result["warnings"]
    assert not any("rc3 as_of" in str(w) for w in result["warnings"])
    assert [w for w in result["warnings"] if isinstance(w, dict) and "as_of_missing_fields" in w] == (
        [{"as_of_missing_fields": {"ref": "fact:1", "fields": missing}}] if missing else [])
    assert fake.calls == [("as_of", {"when": "2026-01-01"})]


def test_asof_mixed_and_empty_results_do_not_invent_capabilities():
    pool, warnings = backend.normalize("beliefs_as_of", {"as_of": "2026-01-01", "beliefs": []})
    assert pool == {} and warnings == [backend.HISTORY_WARNING]
    pool, warnings = backend.normalize("beliefs_as_of", {"as_of": "2026-01-01", "beliefs": [
        {"id": 1, "statement": "Old", "known_at": "2025-01-01", "source": "observed", "still_current": True},
        {"id": 2, "statement": "Older"},
    ]})
    assert [w for w in warnings if isinstance(w, dict)] == [
        {"as_of_missing_fields": {"ref": "fact:2", "fields": ["known_at", "source", "still_current"]}}]


def test_history_asof_shapes_and_collisions(setup, fake, monkeypatch):
    instance, state, _, _ = setup
    scope = scope_for(setup, purpose="history")
    monkeypatch.setattr(fake, "history", lambda **kw: {"versions": [{"id": 1, "statement": "Old", "source": "user_said",
        "known_at": "2026-01-01", "status": "active", "changes": [{"reason": "Correction", "at": "2026-01-02"}]}]}, raising=False)
    monkeypatch.setattr(fake, "as_of", lambda **kw: {"as_of": "2026-01-01", "beliefs": [{"id": 1, "statement": "Old", "kind": "note"}]}, raising=False)
    first = instance.call("history", {"fact_id": 1})
    with pytest.raises(ValueError, match="backend_read_failed"):
        instance.call("beliefs_as_of", {"when": "2026-01-01"})
    captured = contract.load_json(contract.request_artifact(state, scope["request_id"]))
    assert captured["pool"] == first["pool"]
    assert any("provenance conflicted" in str(w) for w in captured["warnings"])


def test_committed_receipt_idempotent_and_no_payload(setup, fake):
    instance, state, _, _ = setup
    scope = scope_for(setup, operation="remember", payload=PAYLOADS["remember"])
    args = {"request_id": scope["request_id"]}
    first = instance.call("apply_approved_write", args)
    second = instance.call("apply_approved_write", args)
    assert first == second and len(fake.calls) == 1
    raw = contract.receipt_path(state, scope["request_id"]).read_text()
    assert PAYLOADS["remember"]["statement"] not in raw and "arguments" not in raw
    assert first["receipt"]["state"] == "committed"
    assert first["receipt"]["store_identity"] == scope["store_identity"]
    assert fake.calls[0][1]["source"] == PAYLOADS["remember"]["source"]


@pytest.mark.parametrize("kind", ["read", "write"])
@pytest.mark.parametrize("identity", [None, {}, {"device": 0}, {"device": 0, "inode": True},
                                      {"device": False, "inode": 1}, {"device": 0, "inode": 0},
                                      {"device": "0", "inode": 1}, {"device": 0, "inode": 1, "extra": 1}])
def test_scope_store_identity_required_and_strict(setup, fake, kind, identity):
    instance, state, _, _ = setup
    scope = scope_for(setup, **({"operation": "remember", "payload": PAYLOADS["remember"]} if kind == "write" else {}))
    if identity is None:
        del scope["store_identity"]
    else:
        scope["store_identity"] = identity
    contract.save_json(state / "active-request.json", scope)
    with pytest.raises(ValueError, match="invalid_store_identity"):
        instance.call("brief" if kind == "read" else "apply_approved_write",
                      {} if kind == "read" else {"request_id": scope["request_id"]})
    assert not fake.calls
    assert not list((state / "requests").iterdir())


@pytest.mark.parametrize("status", ["queued", "executing", "indeterminate", "committed"])
@pytest.mark.parametrize("identity", [None, {"device": 0, "inode": 1}, {"device": True, "inode": 1}])
def test_receipt_identity_missing_or_mismatched_never_acknowledged(setup, fake, status, identity):
    instance, state, _, _ = setup
    scope = scope_for(setup, operation="remember", payload=PAYLOADS["remember"])
    path = contract.receipt_path(state, scope["request_id"])
    receipt = contract.load_json(path)
    receipt["state"] = status
    if identity is None:
        del receipt["store_identity"]
    else:
        receipt["store_identity"] = identity
    contract.save_json(path, receipt)
    with pytest.raises(ValueError, match="invalid_store_identity|receipt_store_mismatch"):
        instance.call("apply_approved_write", {"request_id": scope["request_id"]})
    assert contract.load_json(path) == receipt
    assert not fake.calls
    assert not list((state / "requests").iterdir())


@pytest.mark.parametrize("status", ["queued", "executing", "indeterminate", "committed"])
@pytest.mark.parametrize("refresh_scope", [False, True])
def test_replaced_database_cannot_reuse_old_receipt(setup, fake, status, refresh_scope):
    instance, state, store, _ = setup
    scope = scope_for(setup, operation="remember", payload=PAYLOADS["remember"])
    path = contract.receipt_path(state, scope["request_id"])
    receipt = {**contract.load_json(path), "state": status}
    contract.save_json(path, receipt)
    replacement = store / "replacement.db"
    replacement.write_bytes(b"synthetic replacement database")
    replacement.chmod(0o600)
    os.replace(replacement, store / "memory.db")
    assert contract.store_identity(store) != scope["store_identity"]
    if refresh_scope:
        scope["store_identity"] = contract.store_identity(store)
        contract.save_json(state / "active-request.json", scope)
    with pytest.raises(ValueError, match="receipt_store_mismatch" if refresh_scope else "store_identity_mismatch"):
        instance.call("apply_approved_write", {"request_id": scope["request_id"]})
    assert contract.load_json(path) == receipt
    assert not fake.calls


def test_replaced_database_rejected_before_read_or_cached_pool(setup, fake):
    instance, state, store, _ = setup
    scope = scope_for(setup, purpose="both")
    instance.call("brief", {})
    path = contract.request_artifact(state, scope["request_id"])
    capture = contract.load_json(path)
    replacement = store / "replacement.db"
    replacement.touch(mode=0o600)
    os.replace(replacement, store / "memory.db")
    with pytest.raises(ValueError, match="store_identity_mismatch"):
        instance.call("search_documents", {"query": "synthetic"})
    assert contract.load_json(path) == capture
    assert len(fake.calls) == 1


@pytest.mark.parametrize("status", ["queued", "committed"])
def test_scope_expiring_during_lease_wait_cannot_write_or_acknowledge(setup, fake, monkeypatch, status):
    instance, state, _, _ = setup
    scope = scope_for(setup, operation="remember", payload=PAYLOADS["remember"])
    path = contract.receipt_path(state, scope["request_id"])
    receipt = {**contract.load_json(path), "state": status}
    contract.save_json(path, receipt)
    original = backend.store_session
    @contextmanager
    def delayed_lease(store):
        with original(store):
            contract.save_json(state / "active-request.json", {**scope, "expires_at": 0})
            yield
    monkeypatch.setattr(backend, "store_session", delayed_lease)
    with pytest.raises(ValueError, match="expired_scope"):
        instance.call("apply_approved_write", {"request_id": scope["request_id"]})
    assert not fake.calls
    assert contract.load_json(path) == receipt
    assert not list((state / "requests").iterdir())


@pytest.mark.parametrize("crash", [False, True])
def test_receipt_transitions_keep_identity_and_hold_shared_lease(setup, fake, monkeypatch, crash):
    from ledger_memory import locking
    instance, state, store, _ = setup
    scope = scope_for(setup, operation="remember", payload=PAYLOADS["remember"])
    original = backend.save_json
    statuses = []
    def observe(path, value):
        if value.get("state") in {"executing", "committed", "indeterminate"}:
            assert value["store_identity"] == scope["store_identity"]
            with pytest.raises(TimeoutError):
                with locking.exclusive(locking.session_lock_path(store / "memory.db"), timeout=0):
                    pytest.fail("receipt transition did not hold rc3 session lease")
            statuses.append(value["state"])
        return original(path, value)
    monkeypatch.setattr(backend, "save_json", observe)
    if crash:
        def failure(**args):
            raise RuntimeError("synthetic failure")
        monkeypatch.setattr(fake, "remember", failure)
    result = instance.call("apply_approved_write", {"request_id": scope["request_id"]})
    assert statuses == ["executing", "indeterminate" if crash else "committed"]
    assert result["receipt"]["store_identity"] == scope["store_identity"]


@pytest.mark.parametrize("attack", ["id", "extra", "scope_payload", "receipt_hash", "executing", "indeterminate", "read"])
def test_write_conflicts_and_uncertainty(setup, fake, attack):
    instance, state, _, _ = setup
    scope = scope_for(setup, operation="remember", payload=PAYLOADS["remember"])
    args = {"request_id": scope["request_id"]}
    path = contract.receipt_path(state, scope["request_id"])
    receipt = contract.load_json(path)
    if attack == "id":
        args["request_id"] = "different-id"
    elif attack == "extra":
        args["operation"] = "dispute"
    elif attack == "scope_payload":
        scope["arguments"]["statement"] = "Model edit"
        contract.save_json(state / "active-request.json", scope)
    elif attack == "receipt_hash":
        receipt["payload_hash"] = "wrong"
        contract.save_json(path, receipt)
    elif attack in ("executing", "indeterminate"):
        receipt["state"] = attack
        contract.save_json(path, receipt)
        result = instance.call("apply_approved_write", args)
        assert result["retry_allowed"] is False and result["receipt"] == receipt
        assert not fake.calls
        return
    if attack == "read":
        tool, args = "brief", {}
    else:
        tool = "apply_approved_write"
    with pytest.raises(ValueError):
        instance.call(tool, args)
    assert not fake.calls


def test_receipt_save_after_mutation_failure_retains_executing(setup, fake, monkeypatch):
    instance, state, _, _ = setup
    scope = scope_for(setup, operation="remember", payload=PAYLOADS["remember"])
    original = backend.save_json
    def failing(path, value):
        if value.get("state") == "committed":
            raise OSError("synthetic disk failure")
        return original(path, value)
    monkeypatch.setattr(backend, "save_json", failing)
    with pytest.raises(ValueError, match="receipt_commit_unconfirmed"):
        instance.call("apply_approved_write", {"request_id": scope["request_id"]})
    assert len(fake.calls) == 1
    assert contract.load_json(contract.receipt_path(state, scope["request_id"]))["state"] == "executing"
    assert instance.call("apply_approved_write", {"request_id": scope["request_id"]})["retry_allowed"] is False
    assert len(fake.calls) == 1


def test_receipt_directory_sync_failure_does_not_claim_committed(setup, fake, monkeypatch):
    instance, state, _, _ = setup
    scope = scope_for(setup, operation="remember", payload=PAYLOADS["remember"])
    original = backend.save_json
    def after_rename(path, value):
        original(path, value)
        if value.get("state") == "committed":
            raise OSError("synthetic directory sync failure")
    monkeypatch.setattr(backend, "save_json", after_rename)
    with pytest.raises(ValueError, match="receipt_commit_unconfirmed"):
        instance.call("apply_approved_write", {"request_id": scope["request_id"]})
    assert contract.load_json(contract.receipt_path(state, scope["request_id"]))["state"] == "executing"
    assert len(fake.calls) == 1


def test_expiry_inside_session_blocks_mutation(setup, fake, monkeypatch):
    instance, state, _, _ = setup
    scope = scope_for(setup, operation="remember", payload=PAYLOADS["remember"])
    @contextmanager
    def late_session():
        contract.save_json(state / "active-request.json", {**scope, "expires_at": 0})
        yield fake
    monkeypatch.setattr(fake, "session", late_session)
    result = instance.call("apply_approved_write", {"request_id": scope["request_id"]})
    assert result["receipt"]["state"] == "indeterminate"
    assert not fake.calls


@pytest.mark.parametrize("attack", ["symlink", "hardlink", "owner", "environment"])
def test_store_safety_before_api(setup, fake, monkeypatch, attack):
    instance, state, store, _ = setup
    scope = scope_for(setup)
    if attack == "symlink":
        (store / "memory.db").unlink()
        (store / "memory.db").symlink_to(store / "outside")
    elif attack == "hardlink":
        os.link(store / "memory.db", store / "alias")
    elif attack == "owner":
        monkeypatch.setattr(contract.os, "getuid", lambda: -1)
    else:
        monkeypatch.setenv("LEDGER_VECTORS_PATH", str(store.parent / "outside.db"))
    with pytest.raises((ValueError, OSError)):
        instance.call("brief", {})
    assert not fake.calls


def test_large_selector_evidence_is_whole_and_warning_captured(setup, fake):
    instance, state, _, _ = setup
    scope = scope_for(setup)
    fake.response["facts"][0]["statement"] = "Synthetic archive " * 10000 + " NOT APPROVED"
    result = instance.call("brief", {})
    artifact = contract.load_json(contract.request_artifact(state, scope["request_id"]))
    assert artifact["pool"]["fact:1"]["statement"].endswith(" NOT APPROVED")
    assert result["refs"] == [] and not result["pool"]
    assert result["warnings"] == artifact["warnings"]
    assert any("whole evidence records omitted" in str(w) for w in result["warnings"])


def test_compact_dispute_receipt_does_not_copy_reason():
    result = backend.compact_result("dispute", {"fact": {"id": 7, "statement": "Payload",
        "disputes": [{"reason": "Full caller rationale"}]}, "warnings": ["index unavailable"]})
    assert result == {"id": 7, "type": "fact", "warnings": ["index unavailable"]}


def test_multithread_write_serialization(setup, fake, monkeypatch):
    scope = scope_for(setup, operation="remember", payload=PAYLOADS["remember"])
    original = fake.remember
    def slow(**args):
        time.sleep(0.04)
        return original(**args)
    monkeypatch.setattr(fake, "remember", slow)
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(lambda _: setup[0].call("apply_approved_write", {"request_id": scope["request_id"]}), range(6)))
    assert all(result == results[0] for result in results) and len(fake.calls) == 1


def _lock_child(state, queue):
    with backend.operation_lock(Path(state)):
        queue.put("entered")


def test_multiprocess_operation_lock(setup):
    ctx = multiprocessing.get_context("spawn")
    queue = ctx.Queue()
    with backend.operation_lock(setup[1]):
        child = ctx.Process(target=_lock_child, args=(str(setup[1]), queue))
        child.start()
        with pytest.raises(Exception):
            queue.get(timeout=0.3)
    assert queue.get(timeout=10) == "entered"
    child.join(timeout=10)
    assert child.exitcode == 0


@pytest.fixture
def real(setup):
    from ledger_memory.api import Ledger
    store = setup[2]
    (store / "memory.db").unlink()
    old = os.umask(0o077)
    try:
        ledger = Ledger(store, agent_name="synthetic-fixture").ensure()
        with ledger.session():
            first = ledger.remember("global: synthetic is NOT authorized", source="observed")
            second = ledger.remember("global: synthetic test evidence", source="user_said")
        yield ledger, first["id"], second["id"]
    finally:
        os.umask(old)


@pytest.mark.parametrize("operation", PAYLOADS)
def test_real_ledger_all_mutations(setup, real, operation):
    ledger, first, second = real
    payload = copy.deepcopy(PAYLOADS[operation])
    if "fact_id" in payload:
        payload["fact_id"] = first
    if "conflicts_with" in payload:
        payload["conflicts_with"] = second
    if operation == "link_records":
        payload.update(source_id=first, target_id=second)
    if operation == "create_handover":
        payload["fact_ids"] = [first, second]
    scope = scope_for(setup, operation=operation, payload=payload)
    result = setup[0].call("apply_approved_write", {"request_id": scope["request_id"]})
    assert result["receipt"]["state"] == "committed", result
    assert result["receipt"]["result"]["id"] > 0
    if operation == "revise":
        assert ledger.get_fact(result["receipt"]["result"]["id"])["source"] == "inferred"


@pytest.mark.parametrize("tool", ["brief", "search_documents", "history", "beliefs_as_of"])
def test_real_ledger_read_shapes(setup, real, tool):
    ledger, first, second = real
    ledger.ingest(text='global: synthetic archive {"role":"user","date":"2026-10-01","text":"NOT approved"}', uri="urn:test:archive")
    scope = scope_for(setup, purpose="facts" if tool == "brief" else "history")
    args = {"brief": {}, "search_documents": {"query": "synthetic"},
            "history": {"fact_id": first}, "beliefs_as_of": {"when": "2099-01-01"}}[tool]
    result = setup[0].call(tool, args)
    assert result["pool"]
    assert "NOT" in json.dumps(result["pool"])
    if tool == "search_documents":
        assert all(ref.startswith("chunk:") for ref in result["pool"])
    if tool == "beliefs_as_of":
        assert all(item["as_of"].startswith("2099") for item in result["pool"].values())
        missing = [w for w in result["warnings"] if isinstance(w, dict) and "as_of_missing_fields" in w]
        if version("ledger-memory") == "0.3.0rc3":
            assert len(missing) == len(result["pool"])
        else:
            assert not missing
            assert all(item["still_current"] is True and item["known_at"] and item["source"]
                       for item in result["pool"].values())
        assert backend.HISTORY_WARNING in result["warnings"]


@pytest.mark.parametrize("query", ["Juniper approval", "%Juniper% _approval_", "JUNIPER approval approval"])
def test_juniper_short_keywords_find_existing_doc_under_keyword_fallback(setup, real, query):
    ledger = real[0]
    target = ledger.ingest(text="Juniper approval remains NOT granted.", uri="urn:test:juniper")
    ledger.ingest(text="Other project approval granted.", uri="urn:test:unrelated-project")
    ledger.ingest(text="Juniper staffing plan.", uri="urn:test:unrelated-topic")
    scope = scope_for(setup, purpose="history", project="Juniper",
                      query="Juniper: What do the historical discussions say about approvals and who authorized them?")
    result = setup[0].call("search_documents", {"query": query})
    assert len(result["pool"]) == 1
    hit = next(iter(result["pool"].values()))
    assert hit["document_id"] == target["document_id"]
    assert hit["text"] == "Juniper approval remains NOT granted."
    assert backend.HISTORY_WARNING in result["warnings"]
    assert any("keyword" in str(warning) for warning in result["warnings"])


@pytest.mark.parametrize("target_text", ["Juniper approval remains NOT granted.", "Juniper staffing plan."])
def test_real_keyword_or_cross_project_hits_never_captured(setup, real, monkeypatch, target_text):
    from ledger_memory.api import Ledger
    ledger = real[0]
    other = ledger.ingest(text="Other project approval granted. unrelated-evidence-one", uri="urn:test:other")
    third = ledger.ingest(text="Third project approval granted. unrelated-evidence-two", uri="urn:test:third")
    target = ledger.ingest(text=target_text, uri="urn:test:juniper")
    original = Ledger.search_documents
    raw = []
    def observe(self, **args):
        assert args == {"query": "juniper approval", "limit": 2}
        response = original(self, **args)
        raw.append(copy.deepcopy(response))
        return response
    monkeypatch.setattr(Ledger, "search_documents", observe)
    instance, state, _, _ = setup
    scope = scope_for(setup, purpose="history", project="Juniper", query="Juniper historical approvals")
    result = instance.call("search_documents", {"query": "Juniper approval"})
    captured = contract.load_json(contract.request_artifact(state, scope["request_id"]))
    assert len(raw) == 1 and "keyword" in raw[0]["mode"]
    raw_ids = {item["document_id"] for item in raw[0]["results"]}
    if version("ledger-memory") == "0.3.0rc3":
        assert not raw_ids & {other["document_id"], third["document_id"]}
    else:
        assert other["document_id"] in raw_ids
        assert len(raw_ids) == 2
        assert any("already-limited" in str(w) for w in result["warnings"])
    if "approval" in target_text:
        assert {item["document_id"] for item in result["pool"].values()} == {target["document_id"]}
        assert next(iter(result["pool"].values()))["text"] == target_text
    else:
        assert result["pool"] == {} and result["refs"] == []
    assert captured["pool"] == result["pool"]
    assert captured["calls"] == 1
    assert backend.HISTORY_WARNING in result["warnings"]
    assert "unrelated-evidence" not in json.dumps([result, captured])
    assert "urn:test:other" not in json.dumps([result, captured])
    assert "urn:test:third" not in json.dumps([result, captured])


@pytest.mark.parametrize("project,query,other_text", [
    ("HR", "HR policy", "sharepolicy guidance"),
    ("HR", "HR policy", "thread policy guidance"),
    ("QA", "QA policy", "Qatar policy guidance"),
    ("HR", "HR", "thread guidance"),
    ("QA", "QA", "Qatar guidance"),
    ("X", "X", "Extra guidance"),
])
def test_real_short_project_keywords_and_substring_filter(setup, real, monkeypatch, project, query, other_text):
    from ledger_memory.api import Ledger
    ledger = real[0]
    other = ledger.ingest(text=other_text, uri="urn:test:unrelated")
    target_text = project + " policy remains NOT approved."
    target = ledger.ingest(text=target_text, uri="urn:test:target")
    original = Ledger.search_documents
    raw = []
    def observe(self, **args):
        assert args == {"query": query.lower(), "limit": 2}
        response = original(self, **args)
        raw.append(copy.deepcopy(response))
        return response
    monkeypatch.setattr(Ledger, "search_documents", observe)
    instance, state, _, _ = setup
    scope = scope_for(setup, purpose="history", project=project, query=project + " policy")
    result = instance.call("search_documents", {"query": query})
    captured = contract.load_json(contract.request_artifact(state, scope["request_id"]))
    assert len(raw) == 1 and "keyword" in raw[0]["mode"]
    raw_ids = [item["document_id"] for item in raw[0]["results"]]
    if version("ledger-memory") == "0.3.0rc3":
        assert raw_ids == [other["document_id"], target["document_id"]]
    elif version("ledger-memory") == "0.3.0rc4":
        assert raw_ids == [target["document_id"]] + ([other["document_id"]] if "policy" in query else [])
    else:
        # rc5's FTS candidates use whole-word/prefix matches, not infix matches.
        unrelated_terms = set(other_text.split())
        overlaps = any(term.startswith(word.lower()) for word in query.lower().split()
                       for term in unrelated_terms)
        assert raw_ids == [target["document_id"]] + ([other["document_id"]] if overlaps else [])
    assert {item["document_id"] for item in result["pool"].values()} == {target["document_id"]}
    assert next(iter(result["pool"].values()))["text"] == target_text
    assert captured["pool"] == result["pool"] and captured["warnings"] == result["warnings"]
    assert captured["calls"] == 1
    assert backend.HISTORY_WARNING in result["warnings"]
    assert any("already-limited" in str(w) for w in result["warnings"]) == (other["document_id"] in raw_ids)
    assert other_text not in json.dumps([result, captured])
    assert "urn:test:unrelated" not in json.dumps([result, captured])


def test_crash_after_database_commit_is_indeterminate_once(setup, real, monkeypatch):
    from ledger_memory.api import Ledger
    original = Ledger.remember
    before = real[0].status()["facts_current"]
    def crash(self, **args):
        original(self, **args)
        raise RuntimeError("synthetic crash after database commit: do not leak payload")
    monkeypatch.setattr(Ledger, "remember", crash)
    scope = scope_for(setup, operation="remember", payload=PAYLOADS["remember"])
    for _ in range(2):
        result = setup[0].call("apply_approved_write", {"request_id": scope["request_id"]})
        assert result["receipt"]["state"] == "indeterminate" and result["retry_allowed"] is False
        assert result["receipt"]["store_identity"] == scope["store_identity"]
    assert real[0].status()["facts_current"] == before + 1


def test_real_restore_refuses_whole_request_lease_and_old_receipt_after_restore(setup, real):
    from ledger_memory.api import LedgerError
    instance, state, store, _ = setup
    ledger = real[0]
    snapshot = ledger.backup("synthetic-before-write")["archive_path"]
    before_count = ledger.status()["facts_current"]
    with contract.store_session(store):
        scope = scope_for(setup, operation="remember", payload=PAYLOADS["remember"])
        committed = instance.call("apply_approved_write", {"request_id": scope["request_id"]})["receipt"]
        assert committed["state"] == "committed"
        # This is between backend calls, but the controller's request lease remains.
        with pytest.raises(LedgerError, match="in use by another session"):
            ledger.restore(snapshot, str(store / "memory.db"), force=True)
        assert contract.store_identity(store) == scope["store_identity"]
    ledger.restore(snapshot, str(store / "memory.db"), force=True)
    assert contract.store_identity(store) != committed["store_identity"]
    assert ledger.status()["facts_current"] == before_count
    # Even a fresh valid scope for the replacement database cannot acknowledge or
    # rerun the old commit. Its durable receipt remains bound to the old database.
    scope["store_identity"] = contract.store_identity(store)
    contract.save_json(state / "active-request.json", scope)
    with pytest.raises(ValueError, match="receipt_store_mismatch"):
        instance.call("apply_approved_write", {"request_id": scope["request_id"]})
    assert contract.load_json(contract.receipt_path(state, scope["request_id"])) == committed
    assert ledger.status()["facts_current"] == before_count


def test_model_free_mcp_list_tools_and_rejection(setup):
    import anyio
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    instance, state, store, _ = setup
    before = (store / "memory.db").stat().st_mtime_ns
    async def exercise():
        params = StdioServerParameters(command=sys.executable,
            args=["-I", "-B", str(GATEWAY / "ledger_backend.py"), "--settings", str(instance.settings)])
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as client:
                await client.initialize()
                result = await client.list_tools()
                assert {tool.name for tool in result.tools} == backend.READ_TOOLS | {"apply_approved_write"}
                for tool in result.tools:
                    if tool.name == "brief":
                        assert tool.input_schema == {"type": "object", "properties": {}, "required": [],
                                                     "additionalProperties": False}
                        denied = await client.call_tool(tool.name, {})
                        assert denied.is_error
                    if tool.name == "search_documents":
                        assert tool.input_schema["required"] == ["query"]
                        assert set(tool.input_schema["properties"]) == {"query"}
                        assert tool.input_schema["properties"]["query"]["maxLength"] == 600
                        assert tool.input_schema["additionalProperties"] is False
                        denied = await client.call_tool(tool.name, {"query": "synthetic"})
                        assert denied.is_error
    anyio.run(exercise)
    assert (store / "memory.db").stat().st_mtime_ns == before


def test_actual_controller_hydrates_backend_and_reuses_receipt(setup, real):
    import memory_gateway as gateway
    instance, state, store, cfg = setup
    class SyntheticWorker:
        calls = 0
        def session(self):
            return "ses_synthetic_controller"
        def infer(self, scope):
            self.calls += 1
            contract.save_json(state / "verified-model.json", {"schema": 1, "model": "gpt-6-luna",
                **{key: scope[key] for key in ("owner", "nonce", "request_id", "session_id")}})
            if scope["kind"] == "write":
                instance.call("apply_approved_write", {"request_id": scope["request_id"]})
                raise TimeoutError("Synthetic lost reply after committed receipt")
            result = instance.call("brief", {})
            return json.dumps({"refs": result["refs"], "missing": False})
        def cleanup(self, session):
            assert contract.load_json(state / "active-request.json")["expires_at"] == 0
            return True
        def stop(self):
            return True
    worker = SyntheticWorker()
    controller = gateway.Controller(gateway.Layout(instance.settings), worker)
    result = controller.execute("read", {"question": "synthetic"})
    assert result["evidence"] and result["cache"] == "disabled"
    request = {"operation": "remember", "payload": PAYLOADS["remember"], "request_id": "controller-write"}
    committed = controller.execute("write", request)
    assert committed["receipt"]["state"] == "committed"
    second = controller.execute("write", request)
    assert second["receipt"] == committed["receipt"] and worker.calls == 2
    assert not list((state / "requests").iterdir())


@pytest.mark.parametrize("owner_cache", ["null", '"/synthetic-owner-cache"'])
@pytest.mark.parametrize("tool", ["brief", "search_documents"])
@pytest.mark.parametrize("supplied", ["empty", "none", "absent"])
def test_model_free_mcp_scoped_read_arguments(setup, real, owner_cache, tool, supplied):
    import anyio
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    instance, state, store, _ = setup
    real[0].ingest(text="global: synthetic archival evidence is NOT authorization", uri="urn:test:archive")
    scope = scope_for(setup, purpose="facts" if tool == "brief" else "history")
    async def exercise():
        params = StdioServerParameters(command=sys.executable,
            args=["-I", "-B", str(GATEWAY / "ledger_backend.py"), "--settings", str(instance.settings)],
            env={"HOME": "/synthetic-private-home", "XDG_CACHE_HOME": "/synthetic-private-cache",
                 "LEDGER_GATEWAY_OWNER_HOME": "/synthetic-owner", "LEDGER_GATEWAY_OWNER_CACHE": owner_cache})
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as client:
                await client.initialize()
                query, cap, value = ("question", "budget_tokens", 800) if tool == "brief" else ("query", "limit", 2)
                bad_queries = [{cap: value}, {query: scope["query"], cap: value},
                               {query: "rephrased query"}, {"extra": None}]
                if tool == "brief":
                    bad_queries.append({query: scope["query"]})
                else:
                    bad_queries.extend([{}, {"query": None}, {"query": 123}, {"query": "synthetic", "path": "/not-allowed"}])
                for bad in bad_queries:
                    refused = await client.call_tool(tool, bad)
                    assert refused.is_error
                if supplied == "absent":
                    good = await client.call_tool(tool)
                else:
                    good = await client.call_tool(tool, None if supplied == "none" else {})
                if tool == "search_documents":
                    assert good.is_error
                    good = await client.call_tool(tool, {"query": "SYNTHETIC%"})
                assert not good.is_error
                assert json.loads(good.content[0].text)["pool"]
    anyio.run(exercise)
    assert contract.load_json(contract.request_artifact(state, scope["request_id"]))["calls"] == 1
