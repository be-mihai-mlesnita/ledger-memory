"""Small, synthetic pinned bundles only: never contact a model host."""
import hashlib
import json
from pathlib import Path
import stat
import subprocess
from types import SimpleNamespace

import pytest

from test_deploy import d
import model_download as m


@pytest.fixture
def fixture(tmp_path, monkeypatch):
    runtime = tmp_path / "runtime"
    manifest_path = runtime / "src/ledger_memory/bge-m3-manifest.json"
    manifest_path.parent.mkdir(parents=True)
    files = {"onnx/model.onnx": b"model", "README.md": b"readme"}
    manifest = {"repository": m.REPOSITORY, "revision": m.REVISION,
                "files": {name: {"size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
                          for name, data in files.items()}}
    payload = json.dumps(manifest).encode()
    manifest_path.write_bytes(payload)
    monkeypatch.setattr(m, "MANIFEST_SHA256", hashlib.sha256(payload).hexdigest())
    monkeypatch.setattr(m, "FILES", frozenset(files))
    return runtime, manifest, files


@pytest.mark.parametrize("tamper", ["repository", "revision", "file", "path", "hash", "manifest_link"])
def test_manifest_rejects_unpinned_or_unsafe_input(fixture, monkeypatch, tamper):
    runtime, manifest, files = fixture
    path = runtime / "src/ledger_memory/bge-m3-manifest.json"
    if tamper == "manifest_link":
        target = path.with_name("replacement.json")
        path.rename(target)
        path.symlink_to(target)
    else:
        if tamper == "repository":
            manifest["repository"] = "other/model"
        elif tamper == "revision":
            manifest["revision"] = "not-pinned"
        elif tamper == "file":
            manifest["files"]["extra"] = manifest["files"].pop("README.md")
        elif tamper == "path":
            manifest["files"]["../README.md"] = manifest["files"].pop("README.md")
        else:
            manifest["files"]["README.md"]["sha256"] = "0" * 64
        data = json.dumps(manifest).encode()
        path.write_bytes(data)
        if tamper == "path":
            monkeypatch.setattr(m, "MANIFEST_SHA256", hashlib.sha256(data).hexdigest())
            monkeypatch.setattr(m, "FILES", frozenset(manifest["files"]))
    with pytest.raises(m.ModelDownloadError):
        m.pinned_manifest(runtime)


def test_cache_verification_uses_child_runtime_without_inference_or_output(fixture, monkeypatch):
    runtime, _, _ = fixture
    python = runtime / ".venv/bin/python"
    python.parent.mkdir(parents=True)
    python.write_text("fake")
    calls = []
    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=3 if len(calls) == 1 else 0)
    monkeypatch.setattr(m.subprocess, "run", fake_run)
    assert not m.cached_model(runtime, {"HOME": "/safe", "API_TOKEN": "never-forward"})
    assert m.cached_model(runtime, {"HOME": "/safe"})
    assert calls[0][0][1:4] == ["-I", "-B", "-c"]
    assert "verify_model" in calls[0][0][-1] and "ensure_worker" not in calls[0][0][-1]
    assert calls[0][1]["env"] == {"HOME": "/safe", "PYTHONNOUSERSITE": "1"}
    assert calls[0][1]["stdout"] == subprocess.DEVNULL


def test_curl_download_verifies_and_imports_private_stage(fixture, monkeypatch, capsys):
    runtime, _, files = fixture
    manifest = m.pinned_manifest(runtime)
    calls, imports = [], []
    monkeypatch.setattr(m.shutil, "which", lambda _: "/usr/bin/curl")
    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        path = Path(command[command.index("--output") + 1])
        assert stat.S_IMODE(kwargs["cwd"].stat().st_mode) == 0o700
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        path.write_bytes(files[str(path.relative_to(kwargs["cwd"]))])
        path.chmod(0o600)
        return SimpleNamespace(returncode=0)
    # Stages are private, so tests never need a real downloader or executable.
    monkeypatch.setattr(m.subprocess, "run", fake_run)
    monkeypatch.setattr(d, "run", lambda command, runtime, env, phase: imports.append((command, phase)))
    m.curl_bundle(runtime, {"HTTPS_PROXY": "https://secret@proxy.invalid", "API_TOKEN": "private",
                            "LEDGER_HOME": "sensitive memory path", "LEDGER_CA_BUNDLE": "/ca.pem"}, runtime, manifest)
    assert len(calls) == len(files)
    for command, kwargs in calls:
        assert command[1:4] == ["--disable", "--fail", "--location"]
        assert command[command.index("--proto") + 1] == "=https"
        assert command[command.index("--proto-redir") + 1] == "=https"
        assert command[-1].startswith("https://huggingface.co/BAAI/bge-m3/resolve/" + m.REVISION)
        assert kwargs["env"]["HTTPS_PROXY"] == "https://secret@proxy.invalid"
        assert "API_TOKEN" not in kwargs["env"] and "LEDGER_HOME" not in kwargs["env"]
        assert kwargs["stdout"] == kwargs["stderr"] == subprocess.DEVNULL
        assert command[command.index("--max-filesize") + 1].isdigit()
    assert imports[0][0][1:3] == ["semantic", "import"]
    assert not list(runtime.glob(".model-download-*"))
    assert "secret" not in capsys.readouterr().out and "sensitive memory" not in capsys.readouterr().out


def test_streaming_hash_checks_multiple_blocks_without_model_download(fixture, monkeypatch):
    runtime, _, _ = fixture
    path = runtime / "sample"
    sample = b"a" * (2 * 1024 * 1024 + 13)
    path.write_bytes(sample)
    path.chmod(0o600)
    original = hashlib.sha256
    blocks = []
    class StreamingHash:
        def __init__(self):
            self.value = original()
        def update(self, block):
            blocks.append(len(block))
            self.value.update(block)
        def hexdigest(self):
            return self.value.hexdigest()
    monkeypatch.setattr(m.hashlib, "sha256", StreamingHash)
    m._verify_file(path, len(sample), original(sample).hexdigest())
    assert blocks == [1024 * 1024, 1024 * 1024, 13]


def test_stage_cleaned_after_import_failure(fixture, monkeypatch):
    runtime, _, files = fixture
    monkeypatch.setattr(m.shutil, "which", lambda _: "/usr/bin/curl")
    def fake_run(command, **kwargs):
        path = Path(command[command.index("--output") + 1])
        path.write_bytes(files[str(path.relative_to(kwargs["cwd"]))])
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(m.subprocess, "run", fake_run)
    monkeypatch.setattr(d, "run", lambda *args: (_ for _ in ()).throw(d.DeployError("import failed")))
    with pytest.raises(d.DeployError, match="import failed"):
        m.curl_bundle(runtime, {}, runtime, m.pinned_manifest(runtime))
    assert not list(runtime.glob(".model-download-*"))


@pytest.mark.parametrize("failure", ["wrong_hash", "wrong_size", "symlink", "curl_error", "timeout"])
def test_bad_download_never_imports_or_leaves_stage(fixture, monkeypatch, failure):
    runtime, _, files = fixture
    manifest = m.pinned_manifest(runtime)
    monkeypatch.setattr(m.shutil, "which", lambda _: "/usr/bin/curl")
    monkeypatch.setattr(d, "run", lambda *args, **kwargs: pytest.fail("Incomplete bundle must never import"))
    def fake_run(command, **kwargs):
        if failure == "timeout":
            raise subprocess.TimeoutExpired(command, 1)
        if failure == "curl_error":
            return SimpleNamespace(returncode=22)
        path = Path(command[command.index("--output") + 1])
        if failure == "symlink":
            path.unlink()
            path.symlink_to(runtime / "src/ledger_memory/bge-m3-manifest.json")
        elif failure == "wrong_size":
            path.write_bytes(b"x")
        else:
            path.write_bytes(b"z" * len(files["onnx/model.onnx"]))
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(m.subprocess, "run", fake_run)
    with pytest.raises(m.ModelDownloadError):
        m.curl_bundle(runtime, {}, runtime, manifest)
    assert not list(runtime.glob(".model-download-*"))
