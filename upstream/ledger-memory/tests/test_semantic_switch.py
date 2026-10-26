"""Opt-in local semantic policy, exercised with isolated synthetic models and stores."""
from __future__ import annotations

import pytest
from pathlib import Path
import json

from ledger_memory import cli
from ledger_memory.embedding_recipe import resolve_embedding_recipe
from ledger_memory.api import Ledger


def test_semantic_status_does_not_start_worker_or_download(home, monkeypatch, capsys):
    from ledger_memory import config, embedding_worker, model_artifacts

    calls = []

    def forbidden(*args, **kwargs):
        calls.append(True)
        raise AssertionError("status started an active operation")

    monkeypatch.setattr(embedding_worker, "ensure_worker", forbidden)
    monkeypatch.setattr(model_artifacts, "download_model_bundle", forbidden)
    assert cli.main(["semantic", "status"]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "semantic": "off", "state": "disabled", "reason": None,
        "pending_rows": 0, "failed_rows": 0, "model_fingerprint": None,
        "worker_running": False,
    }
    assert not home.exists()
    ledger = Ledger().ensure()
    config.set_config(ledger.db_path, key="embedding.backend", value="bge-m3", actor_id=ledger.owner_id)
    root = home / "isolated-cache"
    profile = embedding_worker.WorkerProfile(root, root / "auth.key", "unused-synthetic-address", "AF_UNIX")
    monkeypatch.setattr(embedding_worker, "worker_profile", lambda: profile)
    assert cli.main(["semantic", "status"]) == 0
    enabled = json.loads(capsys.readouterr().out)
    assert enabled["semantic"] == "on"
    assert enabled["state"] == "degraded"
    assert enabled["worker_running"] is False
    assert calls == []
    assert not profile.root.exists()


@pytest.mark.parametrize("state", ["downloading", "warming", "indexing", "ready", "degraded"])
def test_semantic_status_preserves_readiness_measurements(home, monkeypatch, capsys, state):
    from ledger_memory import semantic_readiness
    Ledger().ensure()

    report = semantic_readiness.SemanticReadiness(
        False, False, False, "synthetic reason", 7, 2, state, "synthetic-fingerprint", True)
    monkeypatch.setattr(semantic_readiness, "readiness", lambda *args: report)
    assert cli.main(["semantic", "status"]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "semantic": "on", "state": state, "reason": "synthetic reason",
        "pending_rows": 7, "failed_rows": 2, "model_fingerprint": "synthetic-fingerprint",
        "worker_running": True,
    }


@pytest.mark.parametrize("action", ["disable", "off"])
def test_semantic_disable_aliases_return_status(home, capsys, action):
    from ledger_memory import config

    ledger = Ledger().ensure()
    config.set_config(ledger.db_path, key="embedding.backend", value="bge-m3", actor_id=ledger.owner_id)
    assert cli.main(["semantic", action]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["semantic"] == "off"
    assert result["state"] == "disabled"
    assert result["pending_rows"] == result["failed_rows"] == 0
    assert config.get_config(ledger.db_path, "embedding.backend") == "none"


@pytest.mark.parametrize("action", ["disable", "off"])
def test_semantic_disable_refuses_environment_override_before_store(home, monkeypatch, capsys, action):
    monkeypatch.setenv("LEDGER_EMBEDDING_BACKEND", "bge-m3")
    assert cli.main(["semantic", action]) == 2
    result = capsys.readouterr()
    assert result.out == ""
    assert "unset LEDGER_EMBEDDING_BACKEND" in result.err
    assert not home.exists()


def test_semantic_status_never_ensures_or_changes_existing_store(home, monkeypatch, capsys):
    Ledger().ensure()
    before = {str(p.relative_to(home)): p.read_bytes() for p in home.rglob("*") if p.is_file()}
    calls = []
    def forbidden(*args, **kwargs):
        calls.append(True)
        raise AssertionError("status must never ensure or upgrade")
    monkeypatch.setattr(Ledger, "ensure", forbidden)
    assert cli.main(["semantic", "status"]) == 0
    assert json.loads(capsys.readouterr().out)["state"] == "disabled"
    assert calls == []
    assert {str(p.relative_to(home)): p.read_bytes() for p in home.rglob("*") if p.is_file()} == before


def test_semantic_status_missing_store_with_override_is_unavailable(home, monkeypatch, capsys):
    from ledger_memory import embedding_worker
    monkeypatch.setenv("LEDGER_EMBEDDING_BACKEND", "bge-m3")
    calls = []
    def forbidden(*args, **kwargs):
        calls.append(True)
        raise AssertionError("missing store status accessed a worker profile")
    monkeypatch.setattr(embedding_worker, "worker_profile", forbidden)
    monkeypatch.setattr(Ledger, "ensure", forbidden)
    assert cli.main(["semantic", "status"]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "semantic": "on", "state": "degraded", "reason": "memory store unavailable",
        "pending_rows": None, "failed_rows": None, "model_fingerprint": None,
        "worker_running": False,
    }
    assert calls == []
    assert not home.exists()


@pytest.mark.parametrize("arguments", [
    ["import"], ["status", "unexpected"], ["off", "unexpected"],
    ["status", "--model", "bge-m3"], ["disable", "--model", "bge-m3"],
    ["import", "directory", "--model", "bge-m3"],
])
def test_semantic_invalid_options_refused_before_store(home, monkeypatch, arguments):
    calls = []
    def forbidden(*args, **kwargs):
        calls.append(True)
        raise AssertionError("invalid options initialized a store")
    monkeypatch.setattr(Ledger, "ensure", forbidden)
    with pytest.raises(SystemExit) as caught:
        cli.main(["semantic", *arguments])
    assert caught.value.code == 2
    assert calls == []
    assert not home.exists()


def test_legacy_backend_starts_with_keyword_fallback(home, capsys):
    from ledger_memory import config
    ledger = Ledger().ensure()
    ledger.remember("Synthetic retained lexical marker", source="user_said")
    config.set_config(ledger.db_path, key="embedding.backend", value="lmstudio", actor_id=ledger.owner_id)
    assert cli.main(["semantic", "status"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["state"] == "degraded"
    assert "semantic enable" in result["reason"]
    recalled = ledger.recall("lexical marker")
    assert recalled["results"]
    assert any("lexical marker" in row.get("statement", "") for row in recalled["results"]), recalled
    assert any("keyword search only" in warning for warning in recalled["warnings"]), recalled
    assert all("semantic" not in row["lanes"] for row in recalled["results"])


@pytest.fixture
def enabled_fixture(home, monkeypatch):
    from ledger_memory import embedding_worker, model_artifacts, semantic_pending, semantic_rebuild, config
    from ledger_memory.embeddings import DeterministicFakeBackend
    ledger = Ledger().ensure()
    ledger.remember("Synthetic enable source", source="user_said")
    manifest, fingerprint = model_artifacts._manifest(None)
    root = home / "synthetic-profile"
    profile = embedding_worker.WorkerProfile(root, root / "auth.key", "unused", "AF_UNIX")
    monkeypatch.setattr(embedding_worker, "worker_profile", lambda: profile)
    verified = model_artifacts.VerifiedModel(root, root / "model", root / "tokenizer", fingerprint)
    monkeypatch.setattr(model_artifacts, "verify_model", lambda *args: verified)
    calls = []
    def warm(**kwargs):
        calls.append(("warm", config.get_config(ledger.db_path, "embedding.backend")))
        return profile
    monkeypatch.setattr(embedding_worker, "ensure_worker", warm)
    monkeypatch.setattr(model_artifacts, "download_model_bundle", lambda *args: pytest.fail("cached enable downloaded"))
    monkeypatch.setattr(semantic_pending, "request_drain", lambda *args: calls.append(("drain",)) or True)
    class Backend(DeterministicFakeBackend):
        @property
        def model_name(self):
            return f"bge-m3:onnx:{fingerprint}"
    monkeypatch.setattr(semantic_rebuild, "get_embedding_backend", lambda *args: Backend(dimensions=1024))
    return ledger, calls, fingerprint, verified


@pytest.mark.parametrize("action", ["enable", "on"])
def test_semantic_cli_enable_disable_and_aliases(enabled_fixture, capsys, action):
    from ledger_memory import config
    from ledger_memory.semantic_binding import read_receipt
    ledger, calls, fingerprint, _ = enabled_fixture
    assert cli.main(["semantic", action]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["semantic"] == "on"
    assert status["model_fingerprint"] == fingerprint
    assert calls == [("warm", None), ("drain",)]
    assert config.get_config(ledger.db_path, "embedding.backend") == "bge-m3"
    assert resolve_embedding_recipe(ledger.db_path).revision_hash == fingerprint
    receipt = read_receipt(ledger.vectors_path)
    assert receipt.embedding_model == f"bge-m3:onnx:{fingerprint}"
    assert receipt.dimensions == 1024
    assert cli.main(["semantic", "disable"]) == 0
    assert json.loads(capsys.readouterr().out)["semantic"] == "off"


@pytest.mark.parametrize("failure", ["warm", "policy-race", "rebuild"])
def test_enable_failure_is_scrubbed_and_preserves_policy(enabled_fixture, monkeypatch, capsys, failure):
    from ledger_memory import config, embedding_worker, semantic_rebuild
    ledger, calls, _, _ = enabled_fixture
    def fail(*args, **kwargs):
        raise RuntimeError("synthetic-secret-detail")
    if failure == "warm":
        monkeypatch.setattr(embedding_worker, "ensure_worker", fail)
    elif failure == "policy-race":
        monkeypatch.setattr(embedding_worker, "ensure_worker", lambda **kwargs: config.set_config(
            ledger.db_path, key="embedding.backend", value="none", actor_id=ledger.owner_id))
    else:
        monkeypatch.setattr(semantic_rebuild, "replace_active_model_rebuild", fail)
    assert cli.main(["semantic", "enable"]) == 2
    output = capsys.readouterr()
    assert "synthetic-secret-detail" not in output.err
    assert "incomplete" in output.err
    assert output.out == ""
    assert config.get_config(ledger.db_path, "embedding.backend") == {
        "warm": None, "policy-race": "none", "rebuild": "bge-m3"}[failure]
    assert ("drain",) not in calls


def test_enable_reuses_owned_generation_and_downloads_only_when_missing(enabled_fixture, monkeypatch, capsys):
    from ledger_memory import model_artifacts, semantic_rebuild
    ledger, calls, _, verified = enabled_fixture
    monkeypatch.setattr(model_artifacts, "verify_model", lambda *args: (_ for _ in ()).throw(
        model_artifacts.ModelArtifactError("synthetic missing")))
    monkeypatch.setattr(model_artifacts, "download_model_bundle",
                        lambda *args: calls.append(("download",)) or verified)
    assert cli.main(["semantic", "enable"]) == 0
    assert calls[:2] == [("download",), ("warm", None)]
    capsys.readouterr()
    monkeypatch.setattr(model_artifacts, "verify_model", lambda *args: verified)
    monkeypatch.setattr(semantic_rebuild, "replace_active_model_rebuild",
                        lambda *args, **kwargs: pytest.fail("owned generation was replaced again"))
    assert cli.main(["semantic", "on"]) == 0
    assert json.loads(capsys.readouterr().out)["semantic"] == "on"
    assert calls.count(("download",)) == 1


def test_enable_refuses_foreign_generation_before_provisioning(enabled_fixture, capsys):
    from ledger_memory import config
    from ledger_memory.semantic_binding import GenerationReceipt, write_receipt
    ledger, calls, fingerprint, _ = enabled_fixture
    write_receipt(ledger.vectors_path, GenerationReceipt("synthetic-foreign", f"bge-m3:onnx:{fingerprint}",
                  1024, 0, 1, "synthetic", "synthetic"))
    assert cli.main(["semantic", "enable"]) == 2
    assert "another database" in capsys.readouterr().err
    assert config.get_config(ledger.db_path, "embedding.backend") is None
    assert calls == []


@pytest.mark.parametrize("backend,receipt,expected", [
    ("none", {}, []), ("lmstudio", {}, []), ("fake", {}, []),
    ("bge-m3", {}, ["warm", "drain"]),
    ("bge-m3", {"warm_fingerprint": "current"}, ["drain"]),
    ("bge-m3", {"warm_fingerprint": "old"}, ["warm", "drain"]),
    ("bge-m3", {"state": "warm"}, ["warm", "drain"]),
])
def test_mcp_first_start_requests_warmup_only_when_enabled(home, monkeypatch, backend, receipt, expected):
    from ledger_memory import config, mcp_server, embedding_worker, model_artifacts, semantic_pending
    ledger = Ledger().ensure()
    config.set_config(ledger.db_path, key="embedding.backend", value=backend, actor_id=ledger.owner_id)
    profile = object()
    calls = []
    profile_calls = []
    monkeypatch.setattr(embedding_worker, "worker_profile", lambda: profile_calls.append(True) or profile)
    monkeypatch.setattr(embedding_worker, "read_worker_state", lambda actual: receipt)
    monkeypatch.setattr(model_artifacts, "_manifest", lambda value: ({}, "current"))
    monkeypatch.setattr(embedding_worker, "ensure_worker", lambda **kwargs: calls.append("warm"))
    monkeypatch.setattr(semantic_pending, "request_drain", lambda *args: calls.append("drain"))
    monkeypatch.setattr(model_artifacts, "download_model_bundle", lambda *args: pytest.fail("MCP startup downloaded"))
    assert mcp_server._semantic_startup(ledger) == []
    assert calls == expected
    assert bool(profile_calls) is (backend == "bge-m3")


def test_mcp_startup_failures_are_scrubbed_and_server_still_runs(home, monkeypatch, caplog):
    from ledger_memory import config, mcp_server, embedding_worker, model_artifacts, semantic_pending
    ledger = Ledger().ensure()
    config.set_config(ledger.db_path, key="embedding.backend", value="bge-m3", actor_id=ledger.owner_id)
    calls = []
    monkeypatch.setattr(embedding_worker, "worker_profile", lambda: object())
    monkeypatch.setattr(embedding_worker, "read_worker_state", lambda profile: {})
    monkeypatch.setattr(model_artifacts, "_manifest", lambda value: ({}, "current"))
    def fail(**kwargs):
        calls.append("warm")
        raise RuntimeError("synthetic-private-detail")
    monkeypatch.setattr(embedding_worker, "ensure_worker", fail)
    def drain(*args):
        calls.append("drain")
        raise RuntimeError("synthetic-private-detail")
    monkeypatch.setattr(semantic_pending, "request_drain", drain)
    monkeypatch.setattr(mcp_server, "L", lambda: ledger)
    monkeypatch.setattr(mcp_server.server, "run", lambda **kwargs: calls.append("serve"))
    mcp_server.main()
    assert calls == ["warm", "drain", "serve"]
    assert "synthetic-private-detail" not in caplog.text
    assert "warmup unavailable" in caplog.text
    assert "maintenance unavailable" in caplog.text


@pytest.fixture
def short_fresh_home():
    import shutil, tempfile
    fresh = Path(tempfile.mkdtemp(prefix="lm-", dir="/tmp"))  # short: a socket path must fit 104 bytes
    yield fresh
    shutil.rmtree(fresh, ignore_errors=True)


@pytest.mark.skipif(__import__("os").name == "nt", reason="POSIX permission bits")
def test_import_creates_private_service_folder_on_fresh_profile(home, short_fresh_home, monkeypatch, tmp_path, capsys):
    """Regression: the model cache mkdir created the service folder 0755 and the worker then refused it."""
    import os
    import stat
    from ledger_memory import embedding_worker, model_artifacts
    monkeypatch.setenv("HOME", str(short_fresh_home))
    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
    def fake_import(source, cache, **kwargs):
        cache.mkdir(parents=True, exist_ok=True, mode=0o700)
        return model_artifacts.VerifiedModel(cache, cache / "m", cache / "t", "f" * 64, ("onnx/model.onnx",))
    monkeypatch.setattr(model_artifacts, "import_model_bundle", fake_import)
    source = tmp_path / "bundle"
    source.mkdir()
    old_umask = os.umask(0o022)
    try:
        assert cli.main(["semantic", "import", str(source)]) == 0
    finally:
        os.umask(old_umask)
    assert json.loads(capsys.readouterr().out)["enabled"] is False
    root = embedding_worker.worker_profile().root
    assert root.is_relative_to(short_fresh_home)
    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    embedding_worker._private_directory(root)  # the worker's own check accepts it


@pytest.mark.skipif(__import__("os").name == "nt", reason="POSIX permission bits")
def test_existing_non_private_service_folder_is_refused_before_download(home, short_fresh_home, monkeypatch, tmp_path, capsys):
    from ledger_memory import embedding_worker, model_artifacts
    monkeypatch.setenv("HOME", str(short_fresh_home))
    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
    root = embedding_worker.worker_profile().root
    root.mkdir(parents=True)
    root.chmod(0o755)
    called = []
    monkeypatch.setattr(model_artifacts, "import_model_bundle", lambda *a, **k: called.append(True))
    source = tmp_path / "bundle"
    source.mkdir()
    assert cli.main(["semantic", "import", str(source)]) == 2
    assert called == [], "the import must be refused before any cache write"
    assert "refused" in capsys.readouterr().err


def test_legacy_backend_recall_points_to_semantic_enable_not_reindex(home):
    from ledger_memory import config
    ledger = Ledger().ensure()
    config.set_config(ledger.db_path, key="embedding.backend", value="lmstudio", actor_id=ledger.owner_id)
    ledger.remember("Synthetic legacy configured fact", source="observed")
    result = ledger.recall("legacy configured")
    assert [row["id"] for row in result["results"]], "keyword fallback lost the fact"
    lane = [w for w in result["warnings"] if w.startswith("semantic lane disabled")]
    assert lane and "ledger semantic enable" in lane[0] and "reindex" not in lane[0]
