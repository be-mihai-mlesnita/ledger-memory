"""Pinned model files are verified before they can become an active cache."""
from __future__ import annotations

import hashlib
import importlib
import copy
import io
import multiprocessing
import ssl
import sys
import types
import time
from pathlib import Path

import pytest


@pytest.fixture
def bundle(tmp_path):
    source = tmp_path / "bundle"
    source.mkdir()
    blobs = {"onnx/model.onnx": b"synthetic-model", "onnx/tokenizer.json": b'{"version":"1.0"}'}
    for relative, contents in blobs.items():
        destination = source / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(contents)
    manifest = {
        "repository": "BAAI/bge-m3",
        "revision": "5617a9f61b028005a4858fdac845db406aefb181",
        "license": "MIT",
        "model_file": "onnx/model.onnx",
        "tokenizer_file": "onnx/tokenizer.json",
        "dimensions": 1024,
        "max_tokens": 8192,
        "recipe_version": "dense-cls-segments-v1",
        "files": {name: {"size": len(contents), "sha256": hashlib.sha256(contents).hexdigest()}
                  for name, contents in blobs.items()},
    }
    return source, manifest, blobs


def test_import_verifies_all_files(tmp_path, bundle):
    artifacts = importlib.import_module("ledger_memory.model_artifacts")
    source, manifest, blobs = bundle
    cache = tmp_path / "cache"
    result = artifacts.import_model_bundle(source, cache, manifest=manifest)
    assert result.root.resolve().is_relative_to(cache.resolve())
    assert result.model_path.read_bytes() == blobs["onnx/model.onnx"]
    assert result.tokenizer_path.read_bytes() == blobs["onnx/tokenizer.json"]
    assert result.model_fingerprint
    assert result.files == tuple(sorted(blobs))
    verified = artifacts.verify_model(result.root, manifest=manifest)
    assert verified.model_fingerprint == result.model_fingerprint
    other = artifacts.import_model_bundle(source, tmp_path / "other-cache", manifest=manifest)
    assert other.model_fingerprint == result.model_fingerprint


def test_cli_import_verifies_bundle_without_store_or_worker(home, tmp_path, bundle, monkeypatch, capsys):
    import json
    from ledger_memory import cli, embedding_worker, model_artifacts as artifacts

    source, manifest, blobs = bundle
    root = tmp_path / "synthetic-profile"
    profile = embedding_worker.WorkerProfile(root, root / "auth.key", "unused", "AF_UNIX")
    monkeypatch.setattr(embedding_worker, "worker_profile", lambda: profile)
    original_import = artifacts.import_model_bundle
    monkeypatch.setattr(artifacts, "import_model_bundle",
                        lambda source, cache: original_import(source, cache, manifest=manifest))
    calls = []
    def forbidden(*args, **kwargs):
        calls.append(True)
        raise AssertionError("import must not initialize a store or worker")
    monkeypatch.setattr(cli.Ledger, "ensure", forbidden)
    monkeypatch.setattr(embedding_worker, "ensure_worker", forbidden)
    monkeypatch.setattr(artifacts, "download_model_bundle", forbidden)
    assert cli.main(["semantic", "import", str(source)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["imported_files"] == sorted(blobs)
    assert result["enabled"] is False
    verified = artifacts.verify_model(artifacts.model_cache_path(root) / result["model_fingerprint"], manifest=manifest)
    assert verified.model_fingerprint == result["model_fingerprint"]
    assert not home.exists()
    assert not (root / "auth.key").exists()
    assert calls == []
    (source / "onnx/model.onnx").write_bytes(b"invalid")
    assert cli.main(["semantic", "import", str(source)]) == 2
    refused = capsys.readouterr()
    assert refused.out == ""
    assert "refused:" in refused.err
    assert str(source) not in refused.err
    assert calls == []


@pytest.mark.parametrize("failure", [False, True])
def test_download_lifecycle_tracks_actual_body_not_stale_lock(tmp_path, monkeypatch, failure):
    from ledger_memory import model_artifacts as artifacts, locking
    if locking.fcntl is None:
        pytest.skip("shared download observation is unavailable on Windows")
    cache = tmp_path / "cache"
    assert artifacts.download_in_progress(cache) is False
    assert not cache.exists()
    observed = []
    def body(*args, **kwargs):
        observed.append(artifacts.download_in_progress(cache))
        if failure:
            raise OSError("synthetic download failure")
    monkeypatch.setattr(artifacts, "_download_in_child", body)
    if failure:
        with pytest.raises(SystemExit) as caught:
            artifacts._download_entry(cache, {}, tmp_path, tmp_path)
        assert caught.value.code == 2
    else:
        artifacts._download_entry(cache, {}, tmp_path, tmp_path)
    assert observed == [True]
    assert (cache / ".model-download.lock").exists()
    assert artifacts.download_in_progress(cache) is False


def test_download_lifecycle_refuses_concurrent_owner_before_network(tmp_path, monkeypatch):
    import threading
    from ledger_memory import model_artifacts as artifacts, locking
    cache = tmp_path / "cache"
    entered, release = threading.Event(), threading.Event()
    errors = []
    def owner():
        try:
            with locking.exclusive(cache / ".model-download.lock"):
                entered.set()
                if not release.wait(5):
                    raise RuntimeError("synthetic release timeout")
        except BaseException as exc:
            errors.append(exc)
    calls = []
    monkeypatch.setattr(artifacts, "_download_in_child", lambda *args, **kwargs: calls.append(True))
    thread = threading.Thread(target=owner)
    thread.start()
    try:
        assert entered.wait(5)
        with pytest.raises(SystemExit) as caught:
            artifacts._download_entry(cache, {}, tmp_path, tmp_path)
        assert caught.value.code == 2
        assert calls == []
    finally:
        release.set()
        thread.join(5)
    assert not thread.is_alive()
    assert errors == []


def test_imported_bundle_is_the_proxy_and_worker_model_root(tmp_path, bundle, monkeypatch):
    artifacts = importlib.import_module("ledger_memory.model_artifacts")
    worker = importlib.import_module("ledger_memory.embedding_worker")
    backend = importlib.import_module("ledger_memory.adapters.onnx_bge")
    source, manifest, _ = bundle
    profile = types.SimpleNamespace(root=tmp_path / "profile", family="AF_PIPE", address="synthetic")
    profile.key = profile.root / "auth.key"
    profile.root.mkdir(mode=0o700)
    cache = artifacts.model_cache_path(profile.root)
    verified = artifacts.import_model_bundle(source, cache, manifest=manifest)
    original_manifest = artifacts._manifest
    monkeypatch.setattr(artifacts, "_manifest", lambda value: original_manifest(manifest if value is None else value))
    assert artifacts.model_bundle_path(profile.root) == verified.root
    observed = []
    token_marker = object()
    class Tokenizer:
        @staticmethod
        def from_file(path):
            observed.append(Path(path))
            return token_marker
    monkeypatch.setitem(sys.modules, "tokenizers", types.SimpleNamespace(Tokenizer=Tokenizer))
    monkeypatch.setattr(worker, "worker_profile", lambda: profile)
    proxy = backend.ONNXServiceBackend()
    assert proxy._get_tokenizer() is token_marker
    assert observed == [verified.tokenizer_path]
    roots = []
    def service(address, **options):
        roots.append(options["model_root"])
        return {"completed": 0}
    monkeypatch.setattr(worker, "serve_worker", service)
    worker.run_user_worker(profile=profile)
    assert roots == [verified.root]


def test_import_refuses_symlink(tmp_path, bundle):
    artifacts = importlib.import_module("ledger_memory.model_artifacts")
    source, manifest, blobs = bundle
    outside = tmp_path / "outside-model"
    outside.write_bytes(blobs["onnx/model.onnx"])
    model = source / "onnx/model.onnx"
    model.unlink()
    try:
        model.symlink_to(outside)
    except OSError:
        pytest.skip("OS does not permit a synthetic test symlink")
    with pytest.raises(artifacts.ModelArtifactError, match="symlink"):
        artifacts.import_model_bundle(source, tmp_path / "cache", manifest=manifest)
    assert outside.read_bytes() == blobs["onnx/model.onnx"]


def test_import_refuses_unknown_files(tmp_path, bundle):
    artifacts = importlib.import_module("ledger_memory.model_artifacts")
    source, manifest, _ = bundle
    (source / "unexpected.py").write_text("do not import this file")
    with pytest.raises(artifacts.ModelArtifactError, match="unexpected|unknown"):
        artifacts.import_model_bundle(source, tmp_path / "cache", manifest=manifest)


def test_loader_rechecks_verified_files(tmp_path, bundle):
    artifacts = importlib.import_module("ledger_memory.model_artifacts")
    source, manifest, blobs = bundle
    result = artifacts.import_model_bundle(source, tmp_path / "cache", manifest=manifest)
    result.model_path.write_bytes(b"X" * len(blobs["onnx/model.onnx"]))
    assert result.model_path.stat().st_size == len(blobs["onnx/model.onnx"])
    with pytest.raises(artifacts.ModelArtifactError, match="checksum|hash"):
        artifacts.verify_model(result.root, manifest=manifest)


@pytest.mark.parametrize("relative", ["../escape", "/escape", "C:/escape", "onnx/../escape", "onnx\\escape"])
def test_manifest_paths_rejected_before_cache_writes(tmp_path, bundle, relative):
    artifacts = importlib.import_module("ledger_memory.model_artifacts")
    source, original, _ = bundle
    manifest = copy.deepcopy(original)
    manifest["files"][relative] = manifest["files"]["onnx/model.onnx"]
    cache = tmp_path / "cache"
    with pytest.raises(artifacts.ModelArtifactError):
        artifacts.import_model_bundle(source, cache, manifest=manifest)
    assert not cache.exists()


def test_import_refuses_symlink_bundle_root(tmp_path, bundle):
    artifacts = importlib.import_module("ledger_memory.model_artifacts")
    source, manifest, _ = bundle
    alias = tmp_path / "alias"
    try:
        alias.symlink_to(source, target_is_directory=True)
    except OSError:
        pytest.skip("OS does not permit a synthetic test symlink")
    with pytest.raises(artifacts.ModelArtifactError, match="symlink"):
        artifacts.import_model_bundle(alias, tmp_path / "cache", manifest=manifest)


def test_import_corruption_does_not_publish_cache(tmp_path, bundle):
    artifacts = importlib.import_module("ledger_memory.model_artifacts")
    source, manifest, blobs = bundle
    (source / "onnx/model.onnx").write_bytes(b"X" * len(blobs["onnx/model.onnx"]))
    cache = tmp_path / "cache"
    with pytest.raises(artifacts.ModelArtifactError, match="checksum|hash"):
        artifacts.import_model_bundle(source, cache, manifest=manifest)
    assert not cache.exists()


class _DownloadResponse(io.BytesIO):
    status = 200

    def __init__(self, body, url, length=None):
        super().__init__(body)
        self.url = url
        self.headers = {"Content-Length": str(len(body) if length is None else length)}

    def geturl(self):
        return self.url


def _fake_downloads(monkeypatch, artifacts, manifest, blobs, *, mutation=None):
    calls = []
    context = object()
    monkeypatch.setattr(artifacts, "_trust_context", lambda: context)

    class Opener:
        def open(self, request, timeout):
            assert 0 < timeout <= 30
            calls.append(request.full_url)
            prefix = f'https://huggingface.co/{manifest["repository"]}/resolve/{manifest["revision"]}/'
            assert request.full_url.startswith(prefix)
            name = request.full_url[len(prefix):]
            data = blobs[name]
            response = _DownloadResponse(data, request.full_url)
            return mutation(name, response, data) if mutation else response

    monkeypatch.setattr(artifacts.urllib.request, "build_opener", lambda *handlers: Opener())
    return calls


def test_enable_downloads_only_pinned_official_files(tmp_path, bundle, monkeypatch):
    artifacts = importlib.import_module("ledger_memory.model_artifacts")
    _, manifest, blobs = bundle
    calls = _fake_downloads(monkeypatch, artifacts, manifest, blobs)
    result = artifacts._download_in_child(tmp_path / "cache", manifest=manifest)
    assert len(calls) == len(blobs)
    assert result.model_path.read_bytes() == blobs["onnx/model.onnx"]
    assert artifacts.verify_model(result.root, manifest=manifest) == result


def test_download_uses_system_trust(monkeypatch):
    artifacts = importlib.import_module("ledger_memory.model_artifacts")
    protocols = []
    context = object()
    def factory(protocol):
        protocols.append(protocol)
        return context
    monkeypatch.setitem(sys.modules, "truststore", types.SimpleNamespace(SSLContext=factory))
    monkeypatch.delenv("LEDGER_CA_BUNDLE", raising=False)
    assert artifacts._trust_context() is context
    assert protocols == [ssl.PROTOCOL_TLS_CLIENT]


def test_download_adds_explicit_corporate_ca(monkeypatch):
    artifacts = importlib.import_module("ledger_memory.model_artifacts")
    roots = []
    context = types.SimpleNamespace(load_verify_locations=lambda **kw: roots.append(kw))
    monkeypatch.setitem(sys.modules, "truststore", types.SimpleNamespace(SSLContext=lambda protocol: context))
    monkeypatch.setenv("LEDGER_CA_BUNDLE", "synthetic-company-ca.pem")
    assert artifacts._trust_context() is context
    assert roots == [{"cafile": "synthetic-company-ca.pem"}]


def test_download_refuses_wrong_hash(tmp_path, bundle, monkeypatch):
    artifacts = importlib.import_module("ledger_memory.model_artifacts")
    _, manifest, blobs = bundle
    def mutate(name, response, data):
        response.close()
        return _DownloadResponse(b"X" * len(data), response.url)
    _fake_downloads(monkeypatch, artifacts, manifest, blobs, mutation=mutate)
    cache = tmp_path / "cache"
    with pytest.raises(artifacts.ModelArtifactError, match="checksum|hash"):
        artifacts._download_in_child(cache, manifest=manifest)
    assert not cache.exists()


@pytest.mark.parametrize("extra", [-1, 1])
def test_download_refuses_wrong_length(tmp_path, bundle, monkeypatch, extra):
    artifacts = importlib.import_module("ledger_memory.model_artifacts")
    _, manifest, blobs = bundle
    def mutate(name, response, data):
        response.headers["Content-Length"] = str(len(data) + extra)
        return response
    _fake_downloads(monkeypatch, artifacts, manifest, blobs, mutation=mutate)
    with pytest.raises(artifacts.ModelArtifactError, match="length|size"):
        artifacts._download_in_child(tmp_path / "cache", manifest=manifest)
    assert not (tmp_path / "cache").exists()


def test_partial_download_never_becomes_active(tmp_path, bundle, monkeypatch):
    artifacts = importlib.import_module("ledger_memory.model_artifacts")
    source, manifest, blobs = bundle
    cache = tmp_path / "cache"
    previous = artifacts.import_model_bundle(source, cache, manifest=manifest)
    before = {path.relative_to(cache): path.read_bytes() for path in cache.rglob("*") if path.is_file()}
    def mutate(name, response, data):
        response.close()
        result = _DownloadResponse(data[:-1], response.url)
        result.headers = {}
        return result
    _fake_downloads(monkeypatch, artifacts, manifest, blobs, mutation=mutate)
    with pytest.raises(artifacts.ModelArtifactError, match="length|size|checksum"):
        artifacts._download_in_child(cache, manifest=manifest)
    assert {path.relative_to(cache): path.read_bytes() for path in cache.rglob("*") if path.is_file()} == before
    assert artifacts.verify_model(previous.root, manifest=manifest) == previous


def test_download_refuses_tls_downgrade(tmp_path, bundle, monkeypatch):
    artifacts = importlib.import_module("ledger_memory.model_artifacts")
    _, manifest, blobs = bundle
    def mutate(name, response, data):
        response.url = "http://example.invalid/unsafe"
        return response
    _fake_downloads(monkeypatch, artifacts, manifest, blobs, mutation=mutate)
    with pytest.raises(artifacts.ModelArtifactError, match="HTTPS|TLS|scheme"):
        artifacts._download_in_child(tmp_path / "cache", manifest=manifest)
    assert not (tmp_path / "cache").exists()


@pytest.mark.parametrize("extra", [-1, 1])
def test_download_bounds_body_without_content_length(tmp_path, bundle, monkeypatch, extra):
    artifacts = importlib.import_module("ledger_memory.model_artifacts")
    _, manifest, blobs = bundle
    def mutate(name, response, data):
        response.close()
        result = _DownloadResponse(data[:-1] if extra < 0 else data + b"X", response.url)
        result.headers = {}
        return result
    _fake_downloads(monkeypatch, artifacts, manifest, blobs, mutation=mutate)
    with pytest.raises(artifacts.ModelArtifactError, match="size"):
        artifacts._download_in_child(tmp_path / "cache", manifest=manifest)
    assert not (tmp_path / "cache").exists()


def test_redirect_rejects_http_before_following():
    artifacts = importlib.import_module("ledger_memory.model_artifacts")
    request = artifacts.urllib.request.Request("https://example.invalid/model")
    with pytest.raises(artifacts.ModelArtifactError, match="HTTPS"):
        artifacts.HTTPSOnlyRedirect().redirect_request(
            request, None, 302, "Found", {}, "http://example.invalid/model")


def test_download_rejects_unofficial_repository_before_network(tmp_path, bundle, monkeypatch):
    artifacts = importlib.import_module("ledger_memory.model_artifacts")
    _, original, _ = bundle
    manifest = copy.deepcopy(original)
    manifest["repository"] = "untrusted/replacement"
    def refuse():
        pytest.fail("repository validation happened after TLS/network setup")
    monkeypatch.setattr(artifacts, "_trust_context", refuse)
    with pytest.raises(artifacts.ModelArtifactError, match="official"):
        artifacts._download_in_child(tmp_path / "cache", manifest=manifest)
    assert not (tmp_path / "cache").exists()


def _stall_download_child(started):
    started.set()
    time.sleep(3600)


def _completed_download_child(started):
    started.set()


def test_download_supervisor_kills_and_reaps_stalled_child():
    artifacts = importlib.import_module("ledger_memory.model_artifacts")
    context = multiprocessing.get_context("spawn")
    started = context.Event()
    process = context.Process(target=_stall_download_child, args=(started,))
    process.start()
    try:
        assert started.wait(5), "stalled child never reached the measured download body"
        before = time.monotonic()
        with pytest.raises(artifacts.ModelArtifactError, match="deadline"):
            artifacts._wait_for_download(process, timeout=0.1)
        assert time.monotonic() - before < 5
        assert not process.is_alive(), "deadline returned with child still alive"
        assert process.exitcode is not None
    finally:
        if process.is_alive():
            process.kill()
        process.join(timeout=3)
        process.close()


def test_download_supervisor_accepts_completed_child():
    artifacts = importlib.import_module("ledger_memory.model_artifacts")
    context = multiprocessing.get_context("spawn")
    started = context.Event()
    process = context.Process(target=_completed_download_child, args=(started,))
    process.start()
    try:
        assert started.wait(5), "child did not execute"
        artifacts._wait_for_download(process, timeout=5)
        assert not process.is_alive()
        assert process.exitcode == 0
    finally:
        if process.is_alive():
            process.kill()
        process.join(timeout=3)
        process.close()


def test_verified_import_can_replace_corrupt_cache(tmp_path, bundle):
    artifacts = importlib.import_module("ledger_memory.model_artifacts")
    source, manifest, blobs = bundle
    cache = tmp_path / "cache"
    old = artifacts.import_model_bundle(source, cache, manifest=manifest)
    old.model_path.write_bytes(b"X" * len(blobs["onnx/model.onnx"]))
    repaired = artifacts.import_model_bundle(source, cache, manifest=manifest)
    assert repaired.model_path.read_bytes() == blobs["onnx/model.onnx"]
    assert artifacts.verify_model(repaired.root, manifest=manifest) == repaired


def test_failed_replacement_preserves_existing_cache(tmp_path, bundle, monkeypatch):
    artifacts = importlib.import_module("ledger_memory.model_artifacts")
    source, manifest, blobs = bundle
    cache = tmp_path / "cache"
    old = artifacts.import_model_bundle(source, cache, manifest=manifest)
    damaged = b"X" * len(blobs["onnx/model.onnx"])
    old.model_path.write_bytes(damaged)
    original = Path.rename
    def refuse_publication(path, target):
        if path.name.startswith(".model-import-"):
            raise PermissionError("synthetic rename refusal")
        return original(path, target)
    monkeypatch.setattr(Path, "rename", refuse_publication)
    with pytest.raises(PermissionError, match="synthetic"):
        artifacts.import_model_bundle(source, cache, manifest=manifest)
    assert old.model_path.read_bytes() == damaged
    assert not list(cache.glob(".model-import-*"))


def _stall_publication_child(source, cache, manifest, scratch, publication, started):
    from ledger_memory import model_artifacts as artifacts
    original = artifacts._stream
    def stream(path, info, destination=None):
        original(path, info, destination)
        if destination is not None:
            started.set()
            time.sleep(3600)
    artifacts._stream = stream
    artifacts.import_model_bundle(source, cache, manifest=manifest, _publication_stage=publication)


def test_killed_download_cleans_its_publication_stage(tmp_path, bundle, monkeypatch):
    artifacts = importlib.import_module("ledger_memory.model_artifacts")
    source, manifest, _ = bundle
    cache = tmp_path / "cache"
    previous_manifest = copy.deepcopy(manifest)
    previous_manifest["recipe_version"] = "previous-synthetic-recipe"
    previous = artifacts.import_model_bundle(source, cache, manifest=previous_manifest)
    before = {path.relative_to(cache): path.read_bytes() for path in cache.rglob("*") if path.is_file()}
    context = multiprocessing.get_context("spawn")
    started = context.Event()
    class Context:
        def Process(self, *, target, args):
            assert len(args) == 4, "parent must allocate the publication stage it owns"
            child_cache, child_manifest, scratch, publication = args
            return context.Process(target=_stall_publication_child,
                args=(source, child_cache, child_manifest, scratch, publication, started))
    monkeypatch.setattr(artifacts.multiprocessing, "get_context", lambda method: Context())
    with pytest.raises(artifacts.ModelArtifactError, match="deadline"):
        artifacts.download_model_bundle(cache, timeout=2, manifest=manifest)
    assert started.is_set(), "fixture never reached actual cache staging"
    assert not list(cache.glob(".model-import-*"))
    assert {path.relative_to(cache): path.read_bytes() for path in cache.rglob("*") if path.is_file()} == before
    assert artifacts.verify_model(previous.root, manifest=previous_manifest) == previous


def test_unreaped_child_keeps_its_staging_for_operator(tmp_path, bundle, monkeypatch):
    artifacts = importlib.import_module("ledger_memory.model_artifacts")
    _, manifest, _ = bundle
    arguments = []
    class Unreapable:
        pid = 1
        exitcode = None
        def start(self): pass
        def join(self, timeout): pass
        def is_alive(self): return True
        def terminate(self): pass
        def kill(self): pass
    class Context:
        def Process(self, *, target, args):
            arguments.extend(args)
            return Unreapable()
    monkeypatch.setattr(artifacts.multiprocessing, "get_context", lambda method: Context())
    with pytest.raises(artifacts.ModelArtifactError, match="reap|retained"):
        artifacts.download_model_bundle(tmp_path / "cache", timeout=0.1, manifest=manifest)
    assert arguments[2].is_dir(), "scratch was removed without confirmed child death"
    assert arguments[3].is_dir(), "publication stage was removed while child could still write"
    # Synthetic process only; no real child exists. Fixture cleanup owns these paths.
    import shutil
    shutil.rmtree(arguments[2])


def test_semantic_off_never_downloads(home, monkeypatch, capsys):
    """With meaning-based search off, ordinary use never downloads, imports or starts anything."""
    import urllib.request
    from ledger_memory import cli, embedding_worker, model_artifacts
    def forbidden(*args, **kwargs):
        raise AssertionError("semantic off must not download, import or start the worker")
    for name in ("download_model_bundle", "_download_in_child", "import_model_bundle"):
        monkeypatch.setattr(model_artifacts, name, forbidden)
    monkeypatch.setattr(embedding_worker, "ensure_worker", forbidden)
    monkeypatch.setattr(urllib.request, "urlopen", forbidden)
    for argv in (["remember", "Synthetic offline fact"], ["recall", "offline"], ["status"],
                 ["semantic", "status"], ["semantic", "disable"]):
        assert cli.main(argv) == 0, argv
    out = capsys.readouterr().out
    assert '"semantic": "off"' in out
    assert '"state": "disabled"' in out


def test_missing_semantic_dependencies_report_keyword_fallback(home, monkeypatch, capsys):
    import json
    import shutil
    import sys
    import tempfile
    from pathlib import Path
    from ledger_memory import cli, config, model_artifacts
    from ledger_memory.api import Ledger
    for name in ("onnxruntime", "tokenizers", "truststore"):
        monkeypatch.setitem(sys.modules, name, None)  # import fails as if the runtime were not installed
    with pytest.raises(model_artifacts.ModelArtifactError, match="semantic dependencies unavailable"):
        model_artifacts._trust_context()
    profile_home = Path(tempfile.mkdtemp(prefix="lm-", dir="/tmp"))  # short: a socket path must fit 104 bytes
    try:
        monkeypatch.setenv("HOME", str(profile_home))
        monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
        ledger = Ledger().ensure()
        ledger.remember("Synthetic bread dough rests overnight", source="user_said")
        config.set_config(ledger.db_path, key="embedding.backend", value="bge-m3", actor_id=ledger.owner_id)
        hits = ledger.recall("bread dough", limit=3)["results"]
        assert any("bread dough" in (row.get("statement") or "") for row in hits), "keyword fallback lost the fact"
        assert cli.main(["semantic", "status"]) == 0
        status = json.loads(capsys.readouterr().out)
        assert status["semantic"] == "on" and status["state"] == "degraded"
        assert status["reason"].startswith("semantic dependencies unavailable")
        assert status["worker_running"] is False
    finally:
        shutil.rmtree(profile_home, ignore_errors=True)
