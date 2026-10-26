"""Verify pinned model bundles and publish them without executing their contents."""
from __future__ import annotations

import hashlib
import json
import math
import multiprocessing
import os
import re
import shutil
import ssl
import stat
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path

from .locking import exclusive


class ModelArtifactError(RuntimeError):
    """A bundle does not match the pinned artifact contract."""


@dataclass(frozen=True)
class VerifiedModel:
    root: Path
    model_path: Path
    tokenizer_path: Path
    model_fingerprint: str
    files: tuple[str, ...] = ()


def model_cache_path(profile_root: Path) -> Path:
    """The import/download container, not a loadable model directory."""
    return Path(profile_root) / "models" / "bge-m3"


def model_bundle_path(profile_root: Path) -> Path:
    """Resolve the pinned revision published by both artifact entry points."""
    _, fingerprint = _manifest(None)
    return model_cache_path(profile_root) / fingerprint


def _relative(value: object) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ModelArtifactError("invalid manifest path")
    if any(character in value for character in ("\\", ":", "\0")):
        raise ModelArtifactError("invalid manifest path")
    if any(part in ("", ".", "..") for part in value.split("/")):
        raise ModelArtifactError("invalid manifest path")
    return value


def _manifest(value: dict | None) -> tuple[dict, str]:
    if value is None:
        try:
            value = json.loads(Path(__file__).with_name("bge-m3-manifest.json").read_text("utf-8"))
        except (OSError, ValueError) as exc:
            raise ModelArtifactError("pinned manifest unavailable") from exc
    if not isinstance(value, dict):
        raise ModelArtifactError("manifest must be an object")
    for field in ("repository", "revision", "license", "recipe_version"):
        if not isinstance(value.get(field), str) or not value[field].strip():
            raise ModelArtifactError(f"invalid manifest {field}")
    for field in ("dimensions", "max_tokens"):
        if type(value.get(field)) is not int or value[field] <= 0:
            raise ModelArtifactError(f"invalid manifest {field}")
    files = value.get("files")
    if not isinstance(files, dict) or not files:
        raise ModelArtifactError("manifest files must be a nonempty object")
    for name, info in files.items():
        _relative(name)
        if not isinstance(info, dict) or type(info.get("size")) is not int or info["size"] < 0:
            raise ModelArtifactError("invalid manifest file size")
        if not isinstance(info.get("sha256"), str) or not re.fullmatch("[0-9a-f]{64}", info["sha256"]):
            raise ModelArtifactError("invalid manifest checksum")
    for field in ("model_file", "tokenizer_file"):
        if _relative(value.get(field)) not in files:
            raise ModelArtifactError(f"manifest {field} must name a verified file")
    try:
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ModelArtifactError("manifest is not canonical JSON") from exc
    # Freeze caller-owned metadata before a long streaming import.
    return json.loads(encoded), hashlib.sha256(encoded).hexdigest()


def _is_link(path: Path) -> bool:
    return path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction())


def _root(path: Path) -> Path:
    path = Path(os.path.abspath(path))
    if _is_link(path):
        raise ModelArtifactError("bundle/cache root must not be a symlink")
    return path.parent.resolve() / path.name


def _inventory(root: Path, manifest: dict) -> None:
    if not root.is_dir():
        raise ModelArtifactError("bundle directory missing")
    actual = set()
    def walk_error(exc):
        raise ModelArtifactError("bundle enumeration failed") from exc
    for parent, directories, filenames in os.walk(root, followlinks=False, onerror=walk_error):
        for name in directories + filenames:
            if _is_link(Path(parent) / name):
                raise ModelArtifactError("bundle must not contain symlinks")
        actual.update((Path(parent) / name).relative_to(root).as_posix() for name in filenames)
    expected = set(manifest["files"])
    if actual - expected:
        raise ModelArtifactError("unexpected bundle file")
    if expected - actual:
        raise ModelArtifactError("missing bundle file")


def _stream(path: Path, info: dict, destination: Path | None = None) -> None:
    if _is_link(path):
        raise ModelArtifactError("bundle must not contain symlinks")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    flags |= getattr(os, "O_BINARY", 0)
    try:
        fd = os.open(path, flags)
        with os.fdopen(fd, "rb") as source:
            before = os.fstat(source.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_size != info["size"]:
                raise ModelArtifactError("model file type/size mismatch")
            digest = hashlib.sha256()
            size = 0
            output = None
            try:
                if destination is not None:
                    output_fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                                        | getattr(os, "O_BINARY", 0), 0o600)
                    output = os.fdopen(output_fd, "wb")
                while block := source.read(1024 * 1024):
                    size += len(block)
                    if size > info["size"]:
                        raise ModelArtifactError("model file grew during verification")
                    digest.update(block)
                    if output is not None:
                        output.write(block)
                if size != info["size"] or digest.hexdigest() != info["sha256"]:
                    raise ModelArtifactError("model checksum mismatch")
                if output is not None:
                    output.flush()
                    os.fsync(output.fileno())
            finally:
                if output is not None:
                    output.close()
    except OSError as exc:
        raise ModelArtifactError("model file access failed") from exc


def verify_model(root: Path, *, manifest: dict | None = None) -> VerifiedModel:
    manifest, fingerprint = _manifest(manifest)
    root = _root(root)
    _inventory(root, manifest)
    for name, info in manifest["files"].items():
        _stream(root / name, info)
    return VerifiedModel(root, root / manifest["model_file"], root / manifest["tokenizer_file"], fingerprint,
                         tuple(sorted(manifest["files"])))


def import_model_bundle(source: Path, cache: Path, *, manifest: dict | None = None,
                        _publication_stage: Path | None = None) -> VerifiedModel:
    manifest, fingerprint = _manifest(manifest)
    source, cache = _root(source), _root(cache)
    # Refuse invalid input before creating any cache files.
    verify_model(source, manifest=manifest)
    cache.mkdir(parents=True, exist_ok=True, mode=0o700)
    with exclusive(cache / ".model-import.lock"):
        target = cache / fingerprint
        corrupt = False
        if target.exists() or target.is_symlink():
            if _is_link(target) or not target.is_dir():
                raise ModelArtifactError("unsafe existing model cache target")
            try:
                return verify_model(target, manifest=manifest)
            except ModelArtifactError:
                # Retain damaged entries for rollback, never move a symlink tree.
                for parent, directories, filenames in os.walk(target, followlinks=False):
                    if any(_is_link(Path(parent) / name) for name in directories + filenames):
                        raise ModelArtifactError("existing model cache contains symlinks")
                corrupt = True
        if _publication_stage is None:
            stage = Path(tempfile.mkdtemp(prefix=".model-import-", dir=cache))
        else:
            stage = _root(_publication_stage)
            if (stage.parent != cache or not stage.name.startswith(".model-import-")
                    or not stage.is_dir() or any(stage.iterdir())):
                raise ModelArtifactError("invalid publication staging directory")
        quarantine = None
        try:
            for name, info in manifest["files"].items():
                destination = stage / name
                destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                _stream(source / name, info, destination)
            verify_model(stage, manifest=manifest)
            if corrupt:
                quarantine = cache / f".invalid-model-{uuid.uuid4().hex}"
                target.rename(quarantine)
            try:
                stage.rename(target)
                verified = verify_model(target, manifest=manifest)
            except BaseException:
                if target.exists() and not _is_link(target):
                    target.rename(stage)
                if quarantine is not None:
                    quarantine.rename(target)
                raise
        finally:
            # This unique directory belongs only to this invocation, never another import.
            if stage.exists():
                shutil.rmtree(stage)
        return verified


def _trust_context():
    try:
        import truststore
    except ImportError as exc:
        raise ModelArtifactError("semantic dependencies unavailable; install the semantic extra") from exc
    context = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    if ca_bundle := os.environ.get("LEDGER_CA_BUNDLE"):
        context.load_verify_locations(cafile=ca_bundle)
    return context


def _require_https(url: str) -> None:
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ModelArtifactError("HTTPS required for model downloads")


class HTTPSOnlyRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _require_https(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _download_in_child(cache: Path, manifest: dict | None = None,
                       *, staging_parent: Path | None = None,
                       publication_stage: Path | None = None) -> VerifiedModel:
    """Internal download body: caller must supervise this in a killable child.

    Socket timeouts alone cannot bound DNS or a peer that trickles bytes forever.
    This is deliberately not wired into a user command until that supervisor exists.
    """
    manifest, _ = _manifest(manifest)
    if manifest["repository"] != "BAAI/bge-m3" or not re.fullmatch("[0-9a-f]{40}", manifest["revision"]):
        raise ModelArtifactError("download requires the official pinned repository and revision")
    opener = urllib.request.build_opener(
        urllib.request.HTTPSHandler(context=_trust_context()), HTTPSOnlyRedirect())
    with tempfile.TemporaryDirectory(prefix="ledger-model-download-", dir=staging_parent) as directory:
        staging = Path(directory)
        for name, info in manifest["files"].items():
            url = (f'https://huggingface.co/{manifest["repository"]}/resolve/'
                   f'{manifest["revision"]}/{urllib.parse.quote(name, safe="/")}')
            request = urllib.request.Request(url, headers={
                "Accept-Encoding": "identity", "User-Agent": "ledger-model-downloader/1.0"})
            destination = staging / name
            destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            try:
                with opener.open(request, timeout=30) as response:
                    _require_https(response.geturl())
                    if response.status != 200:
                        raise ModelArtifactError("model download HTTP status refused")
                    if response.headers.get("Content-Encoding", "identity").lower() != "identity":
                        raise ModelArtifactError("model download encoding refused")
                    length = response.headers.get("Content-Length")
                    if length is not None:
                        try:
                            valid_length = int(length) == info["size"]
                        except ValueError:
                            valid_length = False
                        if not valid_length:
                            raise ModelArtifactError("model content-length mismatch")
                    fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                                 | getattr(os, "O_BINARY", 0), 0o600)
                    with os.fdopen(fd, "wb") as output:
                        digest = hashlib.sha256()
                        total = 0
                        read = getattr(response, "read1", response.read)
                        while block := read(1024 * 1024):
                            total += len(block)
                            if total > info["size"]:
                                raise ModelArtifactError("model download size exceeded")
                            digest.update(block)
                            output.write(block)
                        if total != info["size"]:
                            raise ModelArtifactError("model download size mismatch")
                        if digest.hexdigest() != info["sha256"]:
                            raise ModelArtifactError("model download checksum mismatch")
                        output.flush()
                        os.fsync(output.fileno())
            except OSError as exc:
                raise ModelArtifactError("model download network or file access failed") from exc
        return import_model_bundle(staging, cache, manifest=manifest, _publication_stage=publication_stage)


def download_in_progress(cache: Path) -> bool:
    """Observe actual downloader ownership, never the age/existence of a marker."""
    from .locking import is_held
    return is_held(_root(cache) / ".model-download.lock")


def _download_entry(cache: Path, manifest: dict, staging_parent: Path, publication_stage: Path) -> None:
    try:
        # The killable child retains ownership even if its caller exits early.
        with exclusive(_root(cache) / ".model-download.lock", timeout=0):
            _download_in_child(cache, manifest, staging_parent=staging_parent, publication_stage=publication_stage)
    except Exception:
        # Do not emit exception chains containing proxy URLs or signed storage URLs.
        raise SystemExit(2)


def _wait_for_download(process, *, timeout: float) -> None:
    try:
        process.join(timeout=max(0.0, timeout))
        if process.is_alive():
            raise ModelArtifactError("model download exceeded its hard deadline")
        if process.exitcode != 0:
            raise ModelArtifactError("model download failed; check semantic dependencies, TLS roots and pinned artifacts")
    finally:
        if process.is_alive():
            process.terminate()
            process.join(timeout=1)
        if process.is_alive():
            process.kill()
            process.join(timeout=2)
        if process.is_alive():
            raise ModelArtifactError("model download child could not be reaped")


def download_model_bundle(cache: Path, *, timeout: float = 1800,
                          manifest: dict | None = None) -> VerifiedModel:
    """Explicit download with a parent-enforced wall deadline; never enables semantic mode."""
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
        raise ValueError("download timeout must be a finite positive number")
    if not math.isfinite(timeout) or not 0 < timeout <= 3600:
        raise ValueError("download timeout must be positive and at most 3600 seconds")
    deadline = time.monotonic() + timeout
    manifest, fingerprint = _manifest(manifest)
    cache = _root(cache)
    cache.mkdir(parents=True, exist_ok=True, mode=0o700)
    context = multiprocessing.get_context("spawn")
    # The parent owns scratch lifetime even when it must kill the download child.
    directory = Path(tempfile.mkdtemp(prefix="ledger-download-owner-"))
    publication = None
    process = None
    try:
        publication = Path(tempfile.mkdtemp(prefix=".model-import-", dir=cache))
        process = context.Process(target=_download_entry, args=(cache, manifest, directory, publication))
        process.start()
        _wait_for_download(process, timeout=deadline - time.monotonic())
    finally:
        if process is not None and process.pid is not None:
            if process.is_alive():
                process.kill()
                process.join(timeout=2)
            if process.is_alive():
                raise ModelArtifactError("download child could not be reaped; staging retained for operator")
            process.close()
        for owned in (directory, publication):
            if owned is not None and owned.exists():
                shutil.rmtree(owned)
    # The completed child already re-read and verified final files. No inference here.
    root = cache / fingerprint
    return VerifiedModel(root, root / manifest["model_file"], root / manifest["tokenizer_file"], fingerprint,
                         tuple(sorted(manifest["files"])))
