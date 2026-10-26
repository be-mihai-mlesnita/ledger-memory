import json
import pytest

from ledger_memory import acceptance, cli
from ledger_memory.api import Ledger, LedgerError


def test_acceptance_uses_only_synthetic_scratch_state(tmp_path, monkeypatch, capsys):
    home = tmp_path / "real-home"
    external = tmp_path / "external-vectors"
    external.write_bytes(b"preserve")
    monkeypatch.setenv("LEDGER_HOME", str(home))
    monkeypatch.setenv("LEDGER_VECTORS_PATH", str(external))
    monkeypatch.setenv("LEDGER_EMBEDDING_BACKEND", "bge-m3")
    def ensure(self, *args, **kwargs):
        pytest.fail("acceptance initialized a store in the caller process")
    monkeypatch.setattr(Ledger, "ensure", ensure)
    output = tmp_path / "report.json"
    assert cli.main(["--home", str(home), "acceptance", "--out", str(output)]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary == {"report_path": str(output), "status": "partial"}
    report = json.loads(output.read_text())
    assert not home.exists()
    assert external.read_bytes() == b"preserve"
    assert any(row["name"] == "store created" and row["status"] == "passed" for row in report["checks"])
    assert str(home) not in output.read_text()
    assert acceptance.os.environ["LEDGER_EMBEDDING_BACKEND"] == "bge-m3"
    assert acceptance.os.environ["LEDGER_VECTORS_PATH"] == str(external)


def test_acceptance_reports_unverified_platform_checks_honestly(tmp_path, monkeypatch):
    monkeypatch.setattr(acceptance.platform, "system", lambda: "Windows")
    monkeypatch.setattr(acceptance, "_bounded_checks", lambda: [{"name": "synthetic", "status": "passed", "reason": None}])
    output = tmp_path / "report.json"
    assert acceptance.run(output)["status"] == "partial"
    report = json.loads(output.read_text())
    assert report["platform"] == "Windows"
    assert report["native_windows_verified"] is False
    assert {"native_windows", "corporate_proxy", "defender_antivirus", "official_model_inference"} == set(report["unverified_checks"])
    assert all(row["status"] == "unverified" for row in report["checks"] if row["name"] in report["unverified_checks"])
    assert report["package_version"] and report["python_version"]


def test_acceptance_refuses_overwriting_existing_report(tmp_path, monkeypatch):
    output = tmp_path / "report.json"
    output.write_bytes(b"existing evidence")
    monkeypatch.setattr(acceptance, "_bounded_checks", lambda: pytest.fail("checks started before output refusal"))
    with pytest.raises(LedgerError, match="choose a new"):
        acceptance.run(output)
    assert output.read_bytes() == b"existing evidence"
    link = tmp_path / "dangling.json"
    link.symlink_to(tmp_path / "absent")
    with pytest.raises(LedgerError):
        acceptance.run(link)
    assert not link.resolve().exists()


def test_acceptance_failure_is_redacted_and_returns_nonzero(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(acceptance, "_bounded_checks", lambda: [{"name": "synthetic execution", "status": "failed", "reason": "synthetic assertion failed"}])
    output = tmp_path / "failed.json"
    assert cli.main(["acceptance", "--out", str(output)]) == 1
    assert json.loads(capsys.readouterr().out)["status"] == "failed"
    assert "private-sentinel" not in output.read_text()
    assert any(row["status"] == "failed" for row in json.loads(output.read_text())["checks"])


def test_acceptance_closes_reserved_output_when_report_build_fails(tmp_path, monkeypatch):
    streams = []
    original = acceptance.os.fdopen
    def wrap(*args, **kwargs):
        stream = original(*args, **kwargs)
        streams.append(stream)
        return stream
    def fail():
        raise ValueError("private-sentinel")
    monkeypatch.setattr(acceptance.os, "fdopen", wrap)
    monkeypatch.setattr(acceptance, "_collect_report", fail)
    output = tmp_path / "incomplete.json"
    with pytest.raises(LedgerError, match="incomplete evidence") as error:
        acceptance.run(output)
    assert "private-sentinel" not in str(error.value)
    assert streams and streams[0].closed
    assert output.exists()
    with pytest.raises(LedgerError, match="choose a new"):
        acceptance.run(output)


@pytest.mark.parametrize("stage", ["fdopen", "metadata"])
def test_acceptance_setup_failures_use_fixed_refusal(tmp_path, monkeypatch, capsys, stage):
    from importlib.metadata import PackageNotFoundError
    def fail(*args, **kwargs):
        raise (OSError("private-sentinel") if stage == "fdopen" else PackageNotFoundError("private-sentinel"))
    monkeypatch.setattr(acceptance, "_bounded_checks", lambda: [{"name": "synthetic", "status": "passed", "reason": None}])
    if stage == "fdopen":
        monkeypatch.setattr(acceptance.os, "fdopen", fail)
    else:
        monkeypatch.setattr(acceptance, "version", fail)
    output = tmp_path / "incomplete.json"
    assert cli.main(["acceptance", "--out", str(output)]) == 2
    captured = capsys.readouterr()
    assert "private-sentinel" not in captured.err
    assert "incomplete evidence" in captured.err
    assert output.exists()


def test_acceptance_skips_are_unverified_and_absent_environment_unchanged(tmp_path, monkeypatch):
    monkeypatch.delenv("LEDGER_VECTORS_PATH", raising=False)
    monkeypatch.setattr(acceptance, "_bounded_checks", lambda: [{"name": "optional synthetic", "status": "unverified", "reason": "synthetic check not executed"}])
    output = tmp_path / "partial.json"
    assert acceptance.run(output)["status"] == "partial"
    report = json.loads(output.read_text())
    assert report["checks"][0]["status"] == "unverified"
    assert "optional synthetic" in report["unverified_checks"]
    assert "LEDGER_VECTORS_PATH" not in acceptance.os.environ


def test_acceptance_child_timeout_is_reaped_without_parent_env_change(monkeypatch):
    import multiprocessing
    monkeypatch.setenv("LEDGER_EMBEDDING_BACKEND", "bge-m3")
    before = {child.pid for child in multiprocessing.active_children()}
    checks = acceptance._bounded_checks(timeout=0.001)
    assert checks[0]["status"] == "failed"
    assert "deadline" in checks[0]["reason"]
    assert acceptance.os.environ["LEDGER_EMBEDDING_BACKEND"] == "bge-m3"
    assert {child.pid for child in multiprocessing.active_children()} == before


def test_acceptance_child_skips_and_failures_are_redacted(tmp_path, monkeypatch):
    def core(ledger, report):
        report.skip("optional synthetic", "private-sentinel")
        raise RuntimeError("private-sentinel")
    monkeypatch.setattr(acceptance, "_core", core)
    rows = acceptance._synthetic_checks(str(tmp_path))
    assert rows[0]["status"] == "unverified"
    assert rows[1]["status"] == "failed"
    assert "private-sentinel" not in json.dumps(rows)


def test_acceptance_constructor_failure_cleans_parent_scratch(tmp_path, monkeypatch):
    from types import SimpleNamespace
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    def fail(**kwargs):
        raise OSError("private-sentinel")
    context = SimpleNamespace(RawArray=lambda kind, size: bytearray(size), Process=fail)
    monkeypatch.setattr(acceptance.multiprocessing, "get_context", lambda method: context)
    monkeypatch.setattr(acceptance.tempfile, "mkdtemp", lambda **kwargs: str(scratch))
    report, status = acceptance._collect_report()
    assert status == "failed"
    assert not scratch.exists()
    assert "private-sentinel" not in json.dumps(report)


def test_acceptance_unreapable_child_preserves_recovery_diagnostic(tmp_path, monkeypatch, capsys):
    from types import SimpleNamespace
    scratch = tmp_path / "retained"
    scratch.mkdir()
    calls = []
    process = SimpleNamespace(pid=123, exitcode=None, start=lambda: None,
        join=lambda timeout: calls.append("join"), is_alive=lambda: True,
        terminate=lambda: calls.append("terminate"), kill=lambda: calls.append("kill"),
        close=lambda: pytest.fail("closed a live process"))
    context = SimpleNamespace(RawArray=lambda kind, size: bytearray(size), Process=lambda **kwargs: process)
    monkeypatch.setattr(acceptance.multiprocessing, "get_context", lambda method: context)
    monkeypatch.setattr(acceptance.tempfile, "mkdtemp", lambda **kwargs: str(scratch))
    output = tmp_path / "incomplete.json"
    assert cli.main(["acceptance", "--out", str(output)]) == 2
    assert "child could not be reaped; scratch retained" in capsys.readouterr().err
    assert scratch.exists()
    assert calls == ["join", "terminate", "join", "kill", "join"]
    assert output.exists()
