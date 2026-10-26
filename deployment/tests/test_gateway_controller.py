"""Synthetic only: no BeMind inference, real credentials, or real memory store."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import hashlib
import importlib.util
import json
import multiprocessing
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

GATEWAY = Path(__file__).resolve().parents[1] / "gateway"
spec = importlib.util.spec_from_file_location("memory_gateway", GATEWAY / "memory_gateway.py")
gateway = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = gateway
spec.loader.exec_module(gateway)
pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX only; Windows ACL support fails closed")


class Helpers:
    @staticmethod
    @contextmanager
    def store_session(store):
        import fcntl

        # Synthetic shared/exclusive maintenance protocol; never a real store.
        fd = os.open(Path(store).parent / "synthetic-store-session.lock", os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
            yield
        finally:
            os.close(fd)

    @staticmethod
    def store_identity(store):
        return {"device": 1, "inode": 2}

    @staticmethod
    def validate_write(operation, payload):
        if operation != "remember" or not isinstance(payload, dict) or set(payload) != {"statement"} or not isinstance(payload["statement"], str):
            raise ValueError("invalid")
        return {"statement": payload["statement"], "source": "user_said"}

    @staticmethod
    def write_hash(operation, arguments):
        return hashlib.sha256(json.dumps([operation, arguments], sort_keys=True).encode()).hexdigest()

    @staticmethod
    def request_artifact(state, request_id):
        return state / "requests" / (hashlib.sha256(request_id.encode()).hexdigest() + ".json")

    @staticmethod
    def receipt_path(state, request_id):
        return state / "receipts" / (hashlib.sha256(request_id.encode()).hexdigest() + ".json")

    @staticmethod
    def knowledge_fingerprint(store):
        raise AssertionError("Caching is explicitly disabled")


@pytest.fixture
def layout(tmp_path):
    root = tmp_path.resolve()
    root.chmod(0o700)
    config = {"schema": 1, "owner": "synthetic-installation-123", "store": str(root / "synthetic-store"),
              "ledger_python": sys.executable, "bemind_command": [sys.executable],
              "bemind_data_home": str(root / "unused-auth-data"), "state_dir": str(root / "private-state"),
              "model": gateway.MODEL, "enabled": True, "idle_seconds": 2, "request_timeout_seconds": 5}
    path = root / "settings.json"
    gateway.save(path, config)
    return gateway.Layout(path)


class FakeWorker:
    def __init__(self, layout, lifetime=None):
        self.layout = layout
        self.calls = 0
        self.sessions = 0
        self.cleaned = []
        self.idle = True
        self.raise_after_commit = False
        self.outcome = "committed"
        self.raw = '{"refs":["fact:2"],"missing":false}'
        self.delay = 0

    def session(self):
        self.sessions += 1
        return "ses_synthetic_" + str(self.sessions)

    def infer(self, scope):
        self.calls += 1
        gateway.save(self.layout.state / "test-active.json", {"active": True})
        if self.delay:
            time.sleep(self.delay)
        gateway.save(self.layout.state / "verified-model.json", {
            **{key: scope[key] for key in ("owner", "nonce", "session_id", "request_id")}, "model": "gpt-6-luna"})
        if scope["kind"] == "write":
            receipt = {"schema": 1, "request_id": scope["request_id"], "operation": scope["operation"],
                       "payload_hash": scope["payload_hash"], "state": self.outcome, "result": {"id": 7},
                       "store_identity": dict(scope["store_identity"])}
            gateway.save(Helpers.receipt_path(self.layout.state, scope["request_id"]), receipt)
            if self.raise_after_commit:
                raise TimeoutError("SENSITIVE provider detail must never escape")
            return "untrusted acknowledgement"
        gateway.save(Helpers.request_artifact(self.layout.state, scope["request_id"]), {
            "schema": 1, "nonce": scope["nonce"], "calls": 1,
            "pool": {"fact:2": {"ref": "fact:2", "id": 2, "type": "fact", "statement": "Canonical original value.",
                                "source": "user_said", "status": "disputed", "known_at": "2026-10-01"}},
            "warnings": ["Unverified user claim", "Search degraded"], "outcomes": [{"tool": "brief", "ok": True}]})
        return self.raw

    def cleanup(self, session):
        assert gateway.load(self.layout.state / "active-request.json")["expires_at"] == 0
        assert not (self.layout.state / "verified-model.json").exists()
        self.cleaned.append(session)
        return self.idle

    def stop(self):
        return True


@pytest.fixture
def controller(layout):
    return gateway.Controller(layout, FakeWorker(layout), Helpers)


def write_args(statement="Exact caller data", request_id="stable-write-id"):
    return {"operation": "remember", "payload": {"statement": statement}, "request_id": request_id}


def test_canonical_hydration_warnings_no_model_prose(controller):
    result = controller.execute("read", {"question": "exact question"})
    assert result["evidence"][0]["statement"] == "Canonical original value."
    assert result["evidence"][0]["status"] == "disputed"
    assert result["warnings"] == ["Unverified user claim", "Search degraded"]
    assert result["cache"] == "disabled"
    assert controller.worker.cleaned == ["ses_synthetic_1"]
    assert not list((controller.layout.state / "requests").iterdir())
    assert not (controller.layout.state / "active-request.json").exists()
    assert not (controller.layout.state / "verified-model.json").exists()


@pytest.mark.parametrize("raw", ["free prose", "```json\n{}\n```", "{}", '[]',
    '{"refs":["fact:999"],"missing":false}', '{"refs":["fact:2","fact:2"],"missing":false}',
    '{"refs":[2],"missing":false}', '{"refs":[],"missing":"false"}',
    '{"refs":[],"missing":false,"evidence":"invented"}'])
def test_invalid_model_output_cannot_be_evidence(controller, raw):
    controller.worker.raw = raw
    with pytest.raises(gateway.GatewayError):
        controller.execute("read", {"question": "x"})
    assert not (controller.layout.state / "active-request.json").exists()
    assert not list((controller.layout.state / "requests").iterdir())


def test_bounded_whole_records_preserve_warnings():
    scope = {"nonce": "n", "max_calls": 1, "purpose": "history"}
    record = {"ref": "document:7", "text": "Uncut sentence. " * 100}
    artifact = {"schema": 1, "nonce": "n", "calls": 1, "pool": {"document:7": record}, "warnings": ["mandatory"],
                "outcomes": [{"tool": "search_documents", "ok": True}]}
    result = gateway.hydrate('{"refs":["document:7"],"missing":false}', artifact, scope, 100)
    assert result["evidence"] == [] and result["missing"]
    assert "mandatory" in result["warnings"]
    assert any("whole records omitted" in warning for warning in result["warnings"])
    assert any("Historical claims" in warning for warning in result["warnings"])


@pytest.mark.parametrize("outcomes", [None, [], {}, [None], [{"tool": "brief"}], [{"ok": 1}], [{"ok": "true"}],
                                     [{"tool": "brief", "ok": True}, {"tool": "brief", "ok": True}]])
def test_hydrate_requires_complete_typed_outcomes(outcomes):
    artifact = {"schema": 1, "nonce": "n", "calls": 1, "pool": {}, "warnings": [], "outcomes": outcomes}
    with pytest.raises(gateway.GatewayError, match="^invalid_evidence$"):
        gateway.hydrate('{"refs":[],"missing":true}', artifact, {"nonce": "n", "max_calls": 1, "purpose": "facts"}, 6000)


@pytest.mark.parametrize("missing", [True, False])
def test_failed_backend_read_is_explicit_error_not_valid_missing(controller, monkeypatch, missing):
    original = controller.worker.infer

    def infer(scope):
        original(scope)
        target = Helpers.request_artifact(controller.layout.state, scope["request_id"])
        artifact = gateway.load(target)
        artifact["pool"] = {}
        artifact["outcomes"][0]["ok"] = False
        artifact["warnings"].append("incomplete: backend read failed")
        gateway.save(target, artifact)
        return json.dumps({"refs": [], "missing": missing})

    monkeypatch.setattr(controller.worker, "infer", infer)
    with pytest.raises(gateway.GatewayError, match="^backend_read_failed$"):
        controller.execute("read", {"question": "synthetic failed read"})
    assert gateway.load(controller.layout.state / "last-request.json")["error_code"] == "backend_read_failed"
    assert controller.worker.cleaned and not controller.layout.unsafe
    assert not (controller.layout.state / "active-request.json").exists()
    assert not list((controller.layout.state / "requests").iterdir())


def test_mixed_success_and_failed_outcomes_reject_partial_evidence():
    record = {"ref": "fact:2", "statement": "Canonical partial evidence."}
    artifact = {"schema": 1, "nonce": "n", "calls": 2, "pool": {"fact:2": record}, "warnings": ["mandatory"],
                "outcomes": [{"tool": "brief", "ok": True}, {"tool": "search_documents", "ok": False}]}
    with pytest.raises(gateway.GatewayError, match="^backend_read_failed$"):
        gateway.hydrate('{"refs":["fact:2"],"missing":true}', artifact, {"nonce": "n", "max_calls": 2, "purpose": "both"}, 6000)


def test_successful_empty_read_preserves_warnings_and_rejects_unknown_refs():
    artifact = {"schema": 1, "nonce": "n", "calls": 1, "pool": {}, "warnings": ["mandatory warning"],
                "outcomes": [{"tool": "brief", "ok": True}]}
    scope = {"nonce": "n", "max_calls": 1, "purpose": "facts"}
    result = gateway.hydrate('{"refs":[],"missing":true}', artifact, scope, 6000)
    assert result["missing"] is True and result["evidence"] == [] and result["warnings"] == artifact["warnings"]
    with pytest.raises(gateway.GatewayError, match="^unknown_reference$"):
        gateway.hydrate('{"refs":["fact:999"],"missing":true}', artifact, scope, 6000)


@pytest.mark.parametrize("args", [{}, {"question": ""}, {"question": "x", "purpose": "write"},
    {"question": "x", "project": "p\nq"}, {"question": "x", "extra": 1}, {"question": 7}])
def test_read_validation_before_worker(controller, args):
    with pytest.raises(gateway.GatewayError):
        controller.execute("read", args)
    assert controller.worker.sessions == 0


def test_exact_scoped_query_no_trimming():
    assert gateway.read_arguments({"question": "  exact caller text  ", "project": "Team X", "purpose": "both"}) == (
        "both", "Team X:   exact caller text  ")


@pytest.mark.parametrize("purpose", ["facts", "history", "both"])
def test_read_input_retains_project_query_and_current_utc_date(layout, monkeypatch, purpose):
    worker = gateway.Worker(layout, None)
    captured = []
    scope = {"kind": "read", "purpose": purpose, "session_id": "ses_synthetic", "agent": "memory-luna-" + purpose,
             "query": 'Project Q:  exact "caller question" last month ', "project": "Project Q", "max_calls": 1 if purpose == "facts" else 2,
             "budget_tokens": 800, "document_limit": 2}
    real_datetime = gateway.datetime

    class Clock:
        @staticmethod
        def now(tz):
            assert tz is gateway.timezone.utc
            return real_datetime(2026, 10, 25, 0, 5, tzinfo=tz)

    monkeypatch.setattr(gateway, "datetime", Clock)

    def api(method, path, body=None, **kwargs):
        if method == "POST":
            captured.append(body)
            return {}
        return [{"info": {"role": "assistant", "providerID": "bemind-proxy", "modelID": "gpt-6-luna"},
                 "parts": [{"type": "text", "text": '{"refs":[],"missing":true}'}]}]

    monkeypatch.setattr(worker, "api", api)
    worker.infer(scope)
    content = json.loads(captured[0]["parts"][0]["text"])
    assert content == {"query": scope["query"], "project": "Project Q", "purpose": purpose,
                       "max_calls": scope["max_calls"], "current_date_utc": "2026-10-25"}
    assert scope["budget_tokens"] == 800 and scope["document_limit"] == 2
    prompt = gateway.agent_config(purpose)["prompt"]
    assert "current_date_utc" in prompt
    if purpose == "facts":
        assert "ledger_brief() once with empty arguments {}" in prompt
        assert "ledger_search_documents" not in prompt
    else:
        assert "ledger_search_documents(query) with only a query field" in prompt
        assert "one or two topical keywords" in prompt and "Preserve the explicit project" in prompt
        assert "Never invent new topic terms or repeat the full natural-language question" in prompt
        if purpose == "history":
            assert "at most two total" in prompt and "drop irrelevant terms" in prompt
        else:
            assert "one ledger_brief() call with empty arguments {}" in prompt
            assert "one history/search/as-of read only" in prompt and "Do not perform a second search" in prompt


@pytest.mark.parametrize("project", [None, "Project Q", " Project: exact name "])
def test_controller_scope_carries_validated_public_project(controller, monkeypatch, project):
    original = controller.worker.infer
    expected = project if project is not None else "global"

    def infer(scope):
        assert scope["project"] == expected
        assert scope["query"] == expected + ":  exact question "
        assert gateway.load(controller.layout.state / "active-request.json")["project"] == expected
        return original(scope)

    monkeypatch.setattr(controller.worker, "infer", infer)
    arguments = {"question": " exact question ", "purpose": "history"}
    if project is not None:
        arguments["project"] = project
    assert controller.execute("read", arguments)["evidence"]


def test_duplicate_write_returns_committed_without_model(controller):
    first = controller.execute("write", write_args())
    second = controller.execute("write", write_args())
    assert first == second and second["receipt"]["state"] == "committed"
    assert second["receipt"]["store_identity"] == {"device": 1, "inode": 2}
    assert controller.worker.calls == 1
    with pytest.raises(gateway.GatewayError, match="request_id_conflict"):
        controller.execute("write", write_args("changed"))
    assert controller.worker.calls == 1


@pytest.mark.parametrize("state", ["queued", "executing", "indeterminate", "committed"])
@pytest.mark.parametrize("identity", ["missing", None, {"device": 9, "inode": 2}, {"device": 1, "inode": 9},
                                     {"device": True, "inode": 2}])
def test_existing_receipt_missing_or_different_store_never_retries(controller, state, identity):
    args = write_args()
    canonical = Helpers.validate_write(args["operation"], args["payload"])
    receipt = {"schema": 1, "request_id": args["request_id"], "operation": args["operation"],
               "payload_hash": Helpers.write_hash(args["operation"], canonical), "state": state, "result": {"id": 7}}
    if identity != "missing":
        receipt["store_identity"] = identity
    path = Helpers.receipt_path(controller.layout.state, args["request_id"])
    gateway.save(path, receipt)
    assert controller.execute("write", args) == {"error": "receipt_store_changed", "retry_allowed": False}
    assert controller.worker.calls == controller.worker.sessions == 0
    assert gateway.load(path) == receipt


@pytest.mark.parametrize("receipt", [None, {}, [], {"schema": 1}])
def test_unbound_receipt_is_not_treated_as_missing_file(controller, receipt):
    path = Helpers.receipt_path(controller.layout.state, "stable-write-id")
    gateway.save(path, receipt)
    assert controller.execute("write", write_args()) == {"error": "receipt_store_changed", "retry_allowed": False}
    assert controller.worker.sessions == controller.worker.calls == 0
    assert gateway.load(path) == receipt


def test_duplicate_after_database_replacement_refuses_old_commit(controller, monkeypatch):
    first = controller.execute("write", write_args())
    monkeypatch.setattr(Helpers, "store_identity", lambda store: {"device": 1, "inode": 3})
    assert controller.execute("write", write_args()) == {"error": "receipt_store_changed", "retry_allowed": False}
    assert controller.worker.calls == 1
    assert gateway.load(Helpers.receipt_path(controller.layout.state, "stable-write-id")) == first["receipt"]


@pytest.mark.parametrize("lost_reply", [False, True])
def test_post_inference_receipt_identity_checked_before_acknowledgement(controller, monkeypatch, lost_reply):
    original = controller.worker.infer

    def infer(scope):
        result = original(scope)
        path = Helpers.receipt_path(controller.layout.state, scope["request_id"])
        receipt = gateway.load(path)
        receipt.pop("store_identity")
        gateway.save(path, receipt)
        if lost_reply:
            raise TimeoutError()
        return result

    monkeypatch.setattr(controller.worker, "infer", infer)
    assert controller.execute("write", write_args()) == {"error": "receipt_store_changed", "retry_allowed": False}
    assert controller.execute("write", write_args()) == {"error": "receipt_store_changed", "retry_allowed": False}
    assert controller.worker.calls == 1


@pytest.mark.parametrize("kind", ["read", "write", "failed_read"])
def test_store_lease_covers_identity_receipts_inference_hydration_and_cleanup(controller, monkeypatch, kind):
    import fcntl

    state = controller.layout.state
    store = Path(controller.layout.cfg["store"])
    phases = []

    def assert_restore_refused(phase):
        phases.append(phase)
        fd = os.open(store.parent / "synthetic-store-session.lock", os.O_RDWR)
        try:
            with pytest.raises(BlockingIOError):
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(fd)
        with pytest.raises(gateway.GatewayError, match="busy"):
            with gateway.lock(state, "request", wait=False):
                pass

    def identity(path):
        assert path == store
        assert_restore_refused("identity")
        return {"device": 1, "inode": 2}

    original_session, original_infer = controller.worker.session, controller.worker.infer
    original_cleanup, original_stop = controller.worker.cleanup, controller.worker.stop
    original_hydrate, original_receipt = gateway.hydrate, controller.receipt

    def session():
        assert_restore_refused("session")
        return original_session()

    def infer(scope):
        assert_restore_refused("inference_before_tools")
        assert scope["store_identity"] == {"device": 1, "inode": 2}
        assert gateway.load(state / "active-request.json")["store_identity"] == scope["store_identity"]
        result = original_infer(scope)
        assert_restore_refused("inference_after_tools")
        if kind == "failed_read":
            raise TimeoutError()
        return result

    def hydrate(*args):
        assert_restore_refused("hydrate")
        return original_hydrate(*args)

    def receipt(*args):
        assert_restore_refused("receipt")
        return original_receipt(*args)

    def cleanup(session_id):
        assert_restore_refused("cleanup")
        return original_cleanup(session_id)

    def stop():
        assert_restore_refused("stop")
        return original_stop()

    monkeypatch.setattr(Helpers, "store_identity", identity)
    monkeypatch.setattr(controller.worker, "session", session)
    monkeypatch.setattr(controller.worker, "infer", infer)
    monkeypatch.setattr(controller.worker, "cleanup", cleanup)
    monkeypatch.setattr(controller.worker, "stop", stop)
    monkeypatch.setattr(gateway, "hydrate", hydrate)
    monkeypatch.setattr(controller, "receipt", receipt)
    if kind == "failed_read":
        with pytest.raises(gateway.GatewayError, match="request_failed"):
            controller.execute("read", {"question": "x"})
        assert "stop" in phases
    elif kind == "write":
        first = controller.execute("write", write_args())
        assert controller.execute("write", write_args()) == first
        assert phases.count("receipt") == 3 and controller.worker.calls == 1
    else:
        assert controller.execute("read", {"question": "x"})["evidence"]
        assert "hydrate" in phases
    assert phases[0] == "identity"
    assert {"session", "inference_before_tools", "inference_after_tools", "cleanup"} <= set(phases)
    fd = os.open(store.parent / "synthetic-store-session.lock", os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        os.close(fd)


def test_store_lease_refusal_never_creates_receipt_or_calls_worker(controller, monkeypatch):
    @contextmanager
    def refusing_lease(store):
        raise RuntimeError("synthetic maintenance busy")
        yield

    monkeypatch.setattr(Helpers, "store_session", refusing_lease)
    with pytest.raises(RuntimeError, match="synthetic maintenance busy"):
        controller.execute("write", write_args())
    assert controller.worker.calls == controller.worker.sessions == 0
    assert not list((controller.layout.state / "receipts").iterdir())
    with gateway.lock(controller.layout.state, "request", wait=False):
        pass


def test_committed_receipt_survives_lost_reply(controller):
    controller.worker.raise_after_commit = True
    result = controller.execute("write", write_args())
    assert result["receipt"]["state"] == "committed" and result["warning"] == "request_failed"
    assert "SENSITIVE" not in json.dumps(result)
    assert controller.execute("write", write_args())["receipt"] == result["receipt"]
    assert controller.worker.calls == 1


@pytest.mark.parametrize("state", ["executing", "indeterminate"])
def test_uncertain_write_never_retried(controller, state):
    controller.worker.outcome = state
    first = controller.execute("write", write_args())
    second = controller.execute("write", write_args())
    assert first["error"] == second["error"] == "write_indeterminate"
    assert second["retry_allowed"] is False and controller.worker.calls == 1


def test_pending_receipt_precedes_model_and_only_id_operation_sent(controller, monkeypatch):
    original = controller.worker.infer

    def infer(scope):
        receipt = gateway.load(Helpers.receipt_path(controller.layout.state, scope["request_id"]))
        assert receipt["state"] == "queued"
        assert receipt["store_identity"] == scope["store_identity"] == {"device": 1, "inode": 2}
        assert scope["project"] is None
        assert scope["arguments"] == {"statement": "Exact caller data", "source": "user_said"}
        worker = gateway.Worker(controller.layout, None)
        messages = []

        def api(method, path, body=None, **kwargs):
            if method == "POST":
                messages.append(body)
                return {}
            return [{"info": {"role": "assistant", "providerID": "bemind-proxy", "modelID": "gpt-6-luna"},
                     "parts": [{"type": "text", "text": "ignored"}]}]

        monkeypatch.setattr(worker, "api", api)
        worker.infer(scope)
        text = json.loads(messages[0]["parts"][0]["text"])
        assert text == {"operation": "remember", "request_id": "stable-write-id"}
        assert [name for name, enabled in messages[0]["tools"].items() if enabled] == ["ledger_apply_approved_write"]
        return original(scope)

    monkeypatch.setattr(controller.worker, "infer", infer)
    controller.execute("write", write_args())


def test_invalid_write_creates_no_receipt_or_session(controller):
    args = write_args()
    args["payload"]["path"] = "/private/secret"
    with pytest.raises(gateway.GatewayError, match="invalid_write"):
        controller.execute("write", args)
    assert not list((controller.layout.state / "receipts").iterdir())
    assert controller.worker.sessions == 0


def test_timeout_unproved_idle_quarantines_and_revokes_scope(controller, monkeypatch):
    controller.worker.idle = False
    monkeypatch.setattr(controller.worker, "infer", lambda scope: (_ for _ in ()).throw(TimeoutError("private")))
    with pytest.raises(gateway.GatewayError, match="quarantined"):
        controller.execute("read", {"question": "secret question"})
    assert not (controller.layout.state / "active-request.json").exists()
    assert not (controller.layout.state / "verified-model.json").exists()
    assert gateway.load(controller.layout.state / "quarantine.json")["code"] == "idle_unproved"
    with pytest.raises(gateway.GatewayError, match="quarantined"):
        controller.execute("read", {"question": "new"})
    with pytest.raises(gateway.GatewayError, match="quarantined"):
        gateway.control(controller.layout.path, "enable")
    assert controller.worker.sessions == 1


def test_committed_but_unproved_idle_returns_receipt_and_quarantines(controller):
    controller.worker.idle = False
    result = controller.execute("write", write_args())
    assert result["receipt"]["state"] == "committed" and result["warning"] == "quarantined"


def test_committed_receipt_not_hidden_by_scope_cleanup_storage_error(controller, monkeypatch):
    original = gateway.save

    def fail_expiration(path, value):
        if path.name == "active-request.json" and value.get("expires_at") == 0:
            raise OSError("synthetic full disk")
        original(path, value)

    monkeypatch.setattr(gateway, "save", fail_expiration)
    result = controller.execute("write", write_args())
    assert result["receipt"]["state"] == "committed" and result["warning"] == "quarantined"
    assert not (controller.layout.state / "active-request.json").exists()
    with pytest.raises(gateway.GatewayError, match="quarantined"):
        controller.execute("read", {"question": "x"})


def test_queued_write_retries_only_after_proven_idle(controller, monkeypatch):
    original = controller.worker.infer
    monkeypatch.setattr(controller.worker, "infer", lambda scope: (_ for _ in ()).throw(TimeoutError()))
    with pytest.raises(gateway.GatewayError, match="request_failed"):
        controller.execute("write", write_args())
    assert gateway.load(Helpers.receipt_path(controller.layout.state, "stable-write-id"))["state"] == "queued"
    monkeypatch.setattr(controller.worker, "infer", original)
    assert controller.execute("write", write_args())["receipt"]["state"] == "committed"
    assert controller.worker.sessions == 2


def test_read_cache_explicitly_disabled_even_for_identical_requests(controller):
    controller.execute("read", {"question": "x"})
    controller.execute("read", {"question": "x"})
    assert controller.worker.calls == 2


@pytest.mark.parametrize("error", [TimeoutError("PRIVATE_PROVIDER_BODY"), gateway.GatewayError("model_failed"),
                                  gateway.GatewayError("missing_model_output")])
def test_matching_guard_diagnostic_used_for_response_and_last_request(controller, monkeypatch, error):
    def infer(scope):
        gateway.save(controller.layout.state / "guard-last-error.json", {"nonce": scope["nonce"], "code": "wire_tool_set_mismatch"})
        raise error

    monkeypatch.setattr(controller.worker, "infer", infer)
    with pytest.raises(gateway.GatewayError, match="^wire_tool_set_mismatch$"):
        controller.execute("read", {"question": "PRIVATE_PROMPT"})
    last = gateway.load(controller.layout.state / "last-request.json")
    assert last["error_code"] == "wire_tool_set_mismatch" and "PRIVATE_" not in json.dumps(last)
    assert not controller.layout.unsafe


@pytest.mark.parametrize("change", [
    {"nonce": "stale-nonce"}, {"code": "PRIVATE_PROVIDER_TEXT"}, {"code": ["wire_model_or_cap"]},
    {"body": "PRIVATE_MODEL_TEXT"},
])
def test_stale_or_uncontrolled_guard_diagnostic_ignored(controller, monkeypatch, change):
    def infer(scope):
        value = {"nonce": scope["nonce"], "code": "wire_model_or_cap", **change}
        gateway.save(controller.layout.state / "guard-last-error.json", value)
        raise TimeoutError("PRIVATE_PROVIDER_BODY")

    monkeypatch.setattr(controller.worker, "infer", infer)
    with pytest.raises(gateway.GatewayError, match="^request_failed$"):
        controller.execute("read", {"question": "x"})
    assert gateway.load(controller.layout.state / "last-request.json")["error_code"] == "request_failed"


def test_old_guard_diagnostic_cleared_before_session(controller, monkeypatch):
    target = controller.layout.state / "guard-last-error.json"
    gateway.save(target, {"nonce": "a" * 64, "code": "wire_model_or_cap"})
    original = controller.worker.session

    def session():
        assert not target.exists()
        return original()

    monkeypatch.setattr(controller.worker, "session", session)
    assert controller.execute("read", {"question": "x"})["evidence"]
    assert not target.exists()


def test_diagnostic_io_failure_does_not_change_default_failure(controller, monkeypatch):
    original_load, original_remove = gateway.load, gateway.remove

    def fail_read(path, default=None):
        if path.name == "guard-last-error.json":
            raise OSError("PRIVATE diagnostic read error")
        return original_load(path, default)

    def fail_remove(path):
        if path.name == "guard-last-error.json":
            raise OSError("PRIVATE diagnostic removal error")
        return original_remove(path)

    monkeypatch.setattr(gateway, "load", fail_read)
    monkeypatch.setattr(gateway, "remove", fail_remove)
    monkeypatch.setattr(controller.worker, "infer", lambda scope: (_ for _ in ()).throw(TimeoutError()))
    with pytest.raises(gateway.GatewayError, match="^request_failed$"):
        controller.execute("read", {"question": "x"})
    assert not controller.layout.unsafe


def test_guard_diagnostic_never_overrides_quarantine(controller, monkeypatch):
    controller.worker.idle = False

    def infer(scope):
        gateway.save(controller.layout.state / "guard-last-error.json", {"nonce": scope["nonce"], "code": "reported_model_mismatch"})
        raise TimeoutError()

    monkeypatch.setattr(controller.worker, "infer", infer)
    with pytest.raises(gateway.GatewayError, match="^quarantined$"):
        controller.execute("read", {"question": "x"})
    assert gateway.load(controller.layout.state / "last-request.json")["error_code"] == "quarantined"
    assert controller.layout.unsafe


def test_guard_diagnostic_preserves_committed_receipt_and_duplicate_behavior(controller, monkeypatch):
    original = controller.worker.infer
    target = controller.layout.state / "guard-last-error.json"

    def infer(scope):
        original(scope)
        gateway.save(target, {"nonce": scope["nonce"], "code": "reported_model_mismatch"})
        raise TimeoutError()

    monkeypatch.setattr(controller.worker, "infer", infer)
    result = controller.execute("write", write_args())
    assert result["receipt"]["state"] == "committed" and result["warning"] == "reported_model_mismatch"
    assert controller.execute("write", write_args())["receipt"] == result["receipt"]
    assert controller.worker.calls == 1 and not target.exists()


def test_lock_busy_is_cross_process(layout):
    context = multiprocessing.get_context("fork")
    receiving, sending = context.Pipe(duplex=False)

    def attempt():
        try:
            with gateway.lock(layout.state, "request", wait=False):
                sending.send("acquired")
        except gateway.GatewayError as exc:
            sending.send(exc.code)

    with gateway.lock(layout.state, "request"):
        child = context.Process(target=attempt)
        child.start()
        assert receiving.recv() == "busy"
        child.join(5)
        assert child.exitcode == 0


def test_whole_request_serialization(controller):
    controller.worker.delay = 0.08
    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = [executor.submit(controller.execute, "read", {"question": "x"}) for _ in range(4)]
        assert all(future.result()["evidence"] for future in futures)
    assert controller.worker.calls == 4
    assert len(controller.worker.cleaned) == 4


def test_settings_identity_and_symlinks_fail_closed(layout, tmp_path):
    identity = gateway.load(layout.state / "identity.json")
    gateway.save(layout.state / "identity.json", {**identity, "store": "/different"})
    with pytest.raises(gateway.GatewayError, match="ownership_mismatch"):
        gateway.Layout(layout.path)
    gateway.save(layout.state / "identity.json", identity)
    outside = tmp_path.resolve() / "outside"
    outside.write_text("private unchanged")
    outside.chmod(0o600)
    (layout.state / "active-request.json").symlink_to(outside)
    with pytest.raises(gateway.GatewayError, match="unsafe_path"):
        gateway.save(layout.state / "active-request.json", {})
    assert outside.read_text() == "private unchanged"


def test_metadata_identity_and_permissions(layout):
    gateway.save(layout.state / "supervisor.json", {"identity": {**layout.identity, "owner": "other"}, "port": 1, "secret": "x" * 40})
    with pytest.raises(gateway.GatewayError, match="ownership_mismatch"):
        layout.metadata()
    gateway.remove(layout.state / "supervisor.json")
    (layout.state / "identity.json").chmod(0o644)
    with pytest.raises(gateway.GatewayError, match="unsafe_permissions"):
        layout.current()


def test_stopped_runtime_handoff_retains_receipts_and_refuses_live_owner(layout, monkeypatch):
    receipt = Helpers.receipt_path(layout.state, "old-receipt")
    gateway.save(receipt, {"state": "committed", "result": {"id": 7}})
    new_runtime = layout.path.parent / "new-runtime"
    new_runtime.mkdir(mode=0o700)
    monkeypatch.setattr(gateway, "ROOT", new_runtime)
    with gateway.lock(layout.state, "lifetime"):
        with pytest.raises(gateway.GatewayError, match="busy"):
            gateway.Layout(layout.path)
    assert gateway.Layout(layout.path, create=False).rebind_required is True
    assert gateway.load(layout.state / "identity.json")["runtime"] != str(new_runtime)
    updated = gateway.Layout(layout.path)
    assert gateway.load(updated.state / "identity.json")["runtime"] == str(new_runtime)
    assert gateway.load(receipt)["state"] == "committed"
    with pytest.raises(gateway.GatewayError, match="ownership_mismatch"):
        layout.current()


def wait_until(predicate, timeout=5):
    until = time.monotonic() + timeout
    while time.monotonic() < until:
        if predicate():
            return
        time.sleep(0.025)
    raise AssertionError("condition timed out")


def run_synthetic_supervisor(settings_path, delay=0):
    class SlowWorker(FakeWorker):
        def __init__(self, *args):
            super().__init__(*args)
            self.delay = delay

    gateway.Worker = SlowWorker
    sys.modules["gateway_backend_contract"] = Helpers
    gateway.supervisor(gateway.Layout(settings_path))


@pytest.fixture
def running_supervisor(layout):
    context = multiprocessing.get_context("spawn")

    process = context.Process(target=run_synthetic_supervisor, args=(str(layout.path), 0.4))
    process.start()
    try:
        wait_until(lambda: layout.metadata() is not None)
        yield process
    finally:
        if process.is_alive():
            try:
                gateway.control(layout.path, "stop")
            except Exception:
                process.terminate()
        process.join(10)


def test_singleton_busy_stop_blocks_new_then_enable(layout, running_supervisor):
    with pytest.raises(gateway.GatewayError, match="busy"):
        gateway.supervisor(layout)
    frontend = gateway.Frontend(layout.path)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(frontend.request, "read", {"question": "x"})
        wait_until(lambda: (layout.state / "test-active.json").exists())
        assert gateway.control(layout.path, "status")["busy"] is True
        with pytest.raises(gateway.GatewayError, match="busy"):
            gateway.control(layout.path, "stop")
        assert gateway.settings(layout.path)["enabled"] is False
        with pytest.raises(gateway.GatewayError, match="disabled"):
            frontend.request("read", {"question": "new"})
        assert future.result()["evidence"]
    assert gateway.control(layout.path, "stop") == {"stopped": True}
    running_supervisor.join(5)
    assert running_supervisor.exitcode == 0
    assert gateway.control(layout.path, "enable") == {"enabled": True}
    assert gateway.control(layout.path, "status")["running"] is False


def test_supervisor_idle_exit_status_does_not_keep_alive(layout, running_supervisor):
    frontend = gateway.Frontend(layout.path)
    until = time.monotonic() + 4
    while running_supervisor.is_alive() and time.monotonic() < until:
        gateway.control(layout.path, "status")
        time.sleep(0.1)
    running_supervisor.join(1)
    assert running_supervisor.exitcode == 0
    assert layout.metadata() is None
    with gateway.lock(layout.state, "lifetime", wait=False):
        pass


def test_startup_singleton_and_restart_after_idle(layout, monkeypatch):
    context = multiprocessing.get_context("spawn")
    children = []

    class Child:
        def __init__(self, *args, **kwargs):
            self.process = context.Process(target=run_synthetic_supervisor, args=(str(layout.path),))
            self.process.start()
            children.append(self.process)

        def poll(self):
            return self.process.exitcode

    monkeypatch.setattr(gateway.subprocess, "Popen", Child)
    try:
        with ThreadPoolExecutor(max_workers=4) as executor:
            endpoints = list(executor.map(lambda _: gateway.Frontend(layout.path).ensure(), range(4)))
        assert len(children) == 1 and all(endpoint == endpoints[0] for endpoint in endpoints)
        children[0].join(5)
        assert children[0].exitcode == 0
        gateway.Frontend(layout.path).ensure()
        assert len(children) == 2
        assert gateway.control(layout.path, "stop")["stopped"]
    finally:
        for child in children:
            if child.is_alive():
                child.terminate()
            child.join(5)


@pytest.fixture
def private_store(layout):
    store = Path(layout.cfg["store"])
    store.mkdir(mode=0o700)
    database = store / "memory.db"
    database.write_bytes(b"SYNTHETIC non-SQLite sentinel: metadata checks must not read this.")
    database.chmod(0o600)
    return store


def test_status_and_preflight_do_not_start_or_touch_other_configs(layout, private_store, monkeypatch):
    unrelated = layout.path.parent / "real-config-sentinel"
    unrelated.write_text("UNCHANGED")
    before = unrelated.read_bytes()
    monkeypatch.setattr(gateway.subprocess, "Popen", lambda *a, **kw: pytest.fail("Must not start"))
    assert gateway.control(layout.path, "status")["running"] is False
    monkeypatch.setitem(sys.modules, "gateway_backend_contract", Helpers)
    monkeypatch.setattr(Path, "is_file", lambda _: True)
    result = gateway.preflight(layout.path)
    assert result["inference"] is False and result["pricing_cap_eur"] is None
    assert result["store_security"] == "metadata_checked"
    assert unrelated.read_bytes() == before


@pytest.mark.parametrize("name", ["store_session", "store_identity"])
def test_preflight_requires_store_lease_and_identity_helpers(layout, private_store, monkeypatch, name):
    class MissingHelper(Helpers):
        pass

    setattr(MissingHelper, name, None)
    monkeypatch.setitem(sys.modules, "gateway_backend_contract", MissingHelper)
    monkeypatch.setattr(Path, "is_file", lambda _: True)
    with pytest.raises(gateway.GatewayError, match="backend_contract_missing"):
        gateway.preflight(layout.path)


def test_store_preflight_uses_only_metadata_without_scope_or_backend_environment(layout, private_store, monkeypatch):
    monkeypatch.setitem(sys.modules, "gateway_backend_contract", Helpers)
    monkeypatch.setattr(Helpers, "store_session", lambda store: pytest.fail("Preflight must not acquire a store session"))
    monkeypatch.setattr(Helpers, "store_identity", lambda store: pytest.fail("Preflight must not call store identity helper"))
    monkeypatch.setenv("LEDGER_HOME", "/synthetic-unrelated-store")
    before_env = dict(os.environ)
    database = private_store / "memory.db"
    before_stat = database.stat()
    original_open, original_load = gateway.os.open, gateway.load

    def no_store_open(path, *args, **kwargs):
        if isinstance(path, (str, os.PathLike)) and Path(path).is_relative_to(private_store):
            pytest.fail("Preflight must not open store data")
        return original_open(path, *args, **kwargs)

    def no_scope(path, default=None):
        if path.name in ("active-request.json", "verified-model.json"):
            pytest.fail("Preflight must not use model scope")
        return original_load(path, default)

    monkeypatch.setattr(gateway.os, "open", no_store_open)
    monkeypatch.setattr(gateway, "load", no_scope)
    monkeypatch.setattr(gateway.subprocess, "Popen", lambda *a, **kw: pytest.fail("No worker/model start"))
    monkeypatch.setattr(Path, "is_file", lambda _: True)
    assert gateway.preflight(layout.path)["store_security"] == "metadata_checked"
    assert dict(os.environ) == before_env
    after_stat = database.stat()
    assert (after_stat.st_ino, after_stat.st_mode, after_stat.st_size, after_stat.st_mtime_ns) == (
        before_stat.st_ino, before_stat.st_mode, before_stat.st_size, before_stat.st_mtime_ns)


@pytest.mark.parametrize("problem,code", [
    ("directory_permissions", "store_directory_needs_attention"),
    ("database_permissions", "store_database_needs_attention"),
    ("missing_database", "store_database_needs_attention"),
    ("database_directory", "store_database_needs_attention"),
    ("database_symlink", "store_database_needs_attention"),
    ("restore_marker", "store_recovery_required"),
    ("dangling_restore_marker", "store_recovery_required"),
])
def test_store_preflight_reports_attention_without_repair(private_store, monkeypatch, problem, code):
    database = private_store / "memory.db"
    marker = private_store / "restore-in-progress.json"
    if problem == "directory_permissions":
        private_store.chmod(0o755)
    elif problem == "database_permissions":
        database.chmod(0o644)
    elif problem in ("missing_database", "database_directory", "database_symlink"):
        database.unlink()
        if problem == "database_directory":
            database.mkdir(mode=0o700)
        elif problem == "database_symlink":
            database.symlink_to(private_store / "nonexistent-target")
    elif problem == "restore_marker":
        marker.write_text("Do not read restore contents")
    elif problem == "dangling_restore_marker":
        marker.symlink_to(private_store / "nonexistent-target")
    before_directory = private_store.stat().st_mode
    before_database = database.lstat().st_mode if problem != "missing_database" else None
    monkeypatch.setattr(gateway.os, "chmod", lambda *a, **kw: pytest.fail("Never autochmod a user store"))
    with pytest.raises(gateway.GatewayError, match="^" + code + "$"):
        gateway.store_preflight(private_store)
    assert private_store.stat().st_mode == before_directory
    if before_database is not None:
        assert database.lstat().st_mode == before_database


@pytest.mark.parametrize("target,code", [("directory", "store_directory_needs_attention"),
                                        ("database", "store_database_needs_attention")])
def test_store_preflight_rejects_foreign_ownership(private_store, monkeypatch, target, code):
    original = Path.lstat
    foreign = private_store if target == "directory" else private_store / "memory.db"

    def foreign_owner(path, *args, **kwargs):
        info = original(path, *args, **kwargs)
        if path == foreign:
            values = list(info)
            values[4] = os.getuid() + 1
            return os.stat_result(values)
        return info

    monkeypatch.setattr(Path, "lstat", foreign_owner)
    with pytest.raises(gateway.GatewayError, match="^" + code + "$"):
        gateway.store_preflight(private_store)


def test_store_preflight_does_not_create_missing_store(layout):
    store = Path(layout.cfg["store"])
    with pytest.raises(gateway.GatewayError, match="^store_directory_needs_attention$"):
        gateway.store_preflight(store)
    assert not store.exists()


def test_orphan_endpoint_never_kills_or_restarts(layout, monkeypatch):
    gateway.save(layout.state / "supervisor.json", {"identity": layout.identity, "port": 1, "secret": "x" * 40})
    monkeypatch.setattr(gateway.subprocess, "Popen", lambda *a, **kw: pytest.fail("No orphan restart"))
    monkeypatch.setattr(gateway.os, "killpg", lambda *a: pytest.fail("No metadata PID kills"))
    with pytest.raises(gateway.GatewayError, match="orphaned_supervisor"):
        gateway.Frontend(layout.path).ensure()


def test_worker_cleanup_idle_proof_and_private_session_only(layout, monkeypatch):
    worker = gateway.Worker(layout, None)
    calls = []

    def api(method, path, body=None):
        calls.append((method, path))
        if path == "/session/status":
            return {"main-session": {"type": "busy"}}
        if method == "DELETE":
            raise OSError("simulate delete failure")
        return True

    monkeypatch.setattr(worker, "api", api)
    assert worker.cleanup("ses_private") is True
    assert gateway.load(layout.state / "cleanup-pending.json") == ["ses_private"]
    assert not any("main-session" in path for _, path in calls)
    monkeypatch.setattr(worker, "api", lambda *a, **kw: {"ses_private": {"type": "busy"}})
    assert worker.cleanup("ses_private") is False


def test_effective_config_rejects_hidden_mcp_and_agent(layout, monkeypatch):
    worker = gateway.Worker(layout, None)
    config = {"model": gateway.MODEL, "small_model": gateway.MODEL, "instructions": [], "share": "disabled",
              "permission": {"*": "deny"}, "mcp": {"ledger": {"enabled": True}},
              "agent": {"memory-luna-" + mode: gateway.agent_config(mode) for mode in gateway.TOOLS}}
    worker.expected = json.loads(json.dumps(config))
    monkeypatch.setattr(worker, "api", lambda *a, **kw: config)
    worker.validate_config()
    config["agent"]["memory-luna-facts"]["options"] = {}
    worker.validate_config()
    config["agent"]["memory-luna-facts"]["options"] = {"unapproved": True}
    with pytest.raises(gateway.GatewayError, match="unsafe_worker_config"):
        worker.validate_config()
    config["agent"]["memory-luna-facts"].pop("options")
    config["mcp"]["hidden"] = {"enabled": True}
    with pytest.raises(gateway.GatewayError, match="unsafe_worker_config"):
        worker.validate_config()
    config["mcp"]["hidden"]["enabled"] = False
    config["agent"]["general"] = {"prompt": "unsafe"}
    with pytest.raises(gateway.GatewayError, match="unsafe_worker_config"):
        worker.validate_config()


def test_worker_stop_signals_only_retained_owned_group(layout):
    owned = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], start_new_session=True)
    other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], start_new_session=True)
    worker = gateway.Worker(layout, None)
    worker.process = owned
    try:
        assert worker.stop() is True
        assert other.poll() is None
    finally:
        for process in (owned, other):
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=5)


def test_worker_environment_auth_and_session_db_isolation(layout, monkeypatch):
    captured = {}
    monkeypatch.setenv("OPENCODE_CONFIG_CONTENT", "normal config must not leak")
    monkeypatch.setenv("OPENAI_API_KEY", "test-value-not-a-real-key")
    monkeypatch.setenv("LEDGER_HOME", "/normal/store")
    network = {key: "synthetic-override-" + key for key in (
        "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy",
        "NODE_EXTRA_CA_CERTS", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE", "BEMIND_PROXY_URL", "BEMIND_WEBAPP_URL")}
    blocked = ("NODE_OPTIONS", "NODE_TLS_REJECT_UNAUTHORIZED", "BEMIND_DEV_AUTH_BYPASS", "BEMIND_API_KEY", "ARBITRARY_SECRET")
    for key, value in network.items():
        monkeypatch.setenv(key, value)
    for key in blocked:
        monkeypatch.setenv(key, "do-not-forward")
    original_cache = str(layout.path.parent / "owner-cache")
    monkeypatch.setenv("XDG_CACHE_HOME", original_cache)

    class Child:
        def __init__(self, command, **kwargs):
            captured.update(command=command, **kwargs)

        def poll(self):
            return None

    worker = gateway.Worker(layout, 123)
    monkeypatch.setattr(gateway.subprocess, "Popen", Child)
    monkeypatch.setattr(worker, "api", lambda *a, **kw: {"healthy": True})
    worker.start()
    env = captured["env"]
    assert env["XDG_DATA_HOME"] == layout.cfg["bemind_data_home"]
    assert env["HOME"] == str(layout.state / "home")
    assert env["OPENCODE_DB"] == str(layout.state / "sessions.db")
    assert env["OPENCODE_DISABLE_PROJECT_CONFIG"] == env["OPENCODE_DISABLE_EXTERNAL_SKILLS"] == "1"
    assert not {"OPENCODE_CONFIG_CONTENT", "OPENAI_API_KEY", "LEDGER_HOME"} & env.keys()
    assert all(env[key] == value for key, value in network.items())
    assert not set(blocked) & env.keys()
    assert env["XDG_CACHE_HOME"] == str(layout.state / "cache")
    assert captured["pass_fds"] == (123,) and captured["start_new_session"] is True
    assert captured["stdout"] == captured["stderr"] == subprocess.DEVNULL
    config = gateway.load(layout.state / "config" / "bemind" / "bemind.jsonc")
    assert [key for key, value in config["mcp"].items() if value["enabled"]] == ["ledger"]
    assert config["mcp"]["ledger"]["command"][-2:] == ["--settings", str(layout.path)]
    assert config["mcp"]["ledger"]["environment"] == {
        "HOME": str(Path.home()), "LEDGER_GATEWAY_OWNER_HOME": str(Path.home()),
        "LEDGER_GATEWAY_OWNER_CACHE": json.dumps(original_cache),
    }
    assert set(config["agent"]) == {"memory-luna-" + mode for mode in gateway.TOOLS}
    assert not Path(layout.cfg["bemind_data_home"]).exists()


def test_private_database_symlink_never_opened(layout, monkeypatch):
    normal = layout.path.parent / "normal-sessions.db"
    normal.write_bytes(b"untouched")
    (layout.state / "sessions.db").symlink_to(normal)
    monkeypatch.setattr(gateway.subprocess, "Popen", lambda *a, **kw: pytest.fail("must not spawn"))
    with pytest.raises(gateway.GatewayError, match="unsafe_path"):
        gateway.Worker(layout, 1).start()
    assert normal.read_bytes() == b"untouched"


def test_windows_acl_support_explicitly_fails_closed(monkeypatch):
    monkeypatch.setattr(gateway.os, "name", "nt")
    with pytest.raises(gateway.GatewayError, match="unsupported_platform_acl"):
        gateway.safe_path(None)


def test_public_stdio_exposes_only_read_write_and_disabled_never_starts(layout):
    import anyio
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    cfg = gateway.settings(layout.path)
    cfg["enabled"] = False
    gateway.save(layout.path, cfg)

    async def inspect():
        params = StdioServerParameters(command=sys.executable,
            args=["-I", "-B", str(gateway.ROOT / "memory_gateway.py"), "serve", "--settings", str(layout.path)],
            env={"HOME": str(layout.state / "home"), "PATH": os.environ.get("PATH", "")})
        with open(os.devnull, "w") as errors:
            async with stdio_client(params, errlog=errors) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    tools = {tool.name: tool for tool in (await session.list_tools()).tools}
                    assert set(tools) == {"read", "write"}
                    assert tools["read"].annotations.read_only_hint is True
                    assert tools["write"].annotations.read_only_hint is False
                    response = await session.call_tool("read", {"question": "Synthetic disabled check"})
                    assert "disabled" in " ".join(part.text for part in response.content if hasattr(part, "text"))
        assert layout.metadata() is None

    anyio.run(inspect)
