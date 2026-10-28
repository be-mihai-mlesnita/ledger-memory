"""Consent-gated, curl-only acquisition of the installed core's pinned BGE-M3 bundle."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tempfile
from urllib.parse import quote


REPOSITORY = "BAAI/bge-m3"
REVISION = "5617a9f61b028005a4858fdac845db406aefb181"
# Exact released manifest bytes also pin the complete file inventory, sizes and hashes.
MANIFEST_SHA256 = "8d5389e21547bfac2e2a574640fb95ecc91a3a8de60542a3bf1982b452267597"
FILES = frozenset(("README.md", "onnx/model.onnx", "onnx/model.onnx_data", "onnx/tokenizer.json",
                   "onnx/sentencepiece.bpe.model", "onnx/Constant_7_attr__value", "onnx/config.json",
                   "onnx/special_tokens_map.json", "onnx/tokenizer_config.json"))
VERIFY_CACHE = """from ledger_memory.embedding_worker import worker_profile
from ledger_memory.model_artifacts import ModelArtifactError, model_bundle_path, verify_model
try:
    verify_model(model_bundle_path(worker_profile().root))
except ModelArtifactError:
    raise SystemExit(3)
"""


class ModelDownloadError(RuntimeError):
    """Content-free download or verification failure."""


def pinned_manifest(runtime: Path) -> dict:
    path = runtime / "src/ledger_memory/bge-m3-manifest.json"
    try:
        if path.is_symlink():
            raise ModelDownloadError("Pinned model manifest is unsafe.")
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != MANIFEST_SHA256:
            raise ModelDownloadError("Installed model manifest differs from the approved pin.")
        manifest = json.loads(data)
        if (manifest["repository"] != REPOSITORY or manifest["revision"] != REVISION
                or not isinstance(manifest["files"], dict) or set(manifest["files"]) != FILES):
            raise ModelDownloadError("Installed model manifest differs from the approved pin.")
        for name, info in manifest["files"].items():
            if (not re.fullmatch(r"[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*", name)
                    or any(part in (".", "..") for part in name.split("/"))
                    or type(info["size"]) is not int or info["size"] < 0
                    or not isinstance(info["sha256"], str)
                    or not re.fullmatch(r"[0-9a-f]{64}", info["sha256"])):
                raise ModelDownloadError("Installed model manifest contains unsafe artifacts.")
        return manifest
    except (OSError, ValueError, TypeError, KeyError):
        raise ModelDownloadError("Installed pinned model manifest is unavailable.") from None


def cached_model(runtime: Path, env: dict[str, str]) -> bool:
    """Use the installed core for read-only cache verification, never inference."""
    python = runtime / ".venv/bin/python"
    if not python.is_file():
        raise ModelDownloadError("Installed runtime Python is unavailable for model verification.")
    safe_env = {key: env[key] for key in ("HOME", "XDG_CACHE_HOME", "LOCALAPPDATA", "USERPROFILE") if key in env}
    safe_env["PYTHONNOUSERSITE"] = "1"
    try:
        result = subprocess.run([str(python), "-I", "-B", "-c", VERIFY_CACHE], cwd=runtime,
                                env=safe_env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, timeout=1800, check=False)
    except (OSError, subprocess.SubprocessError):
        raise ModelDownloadError("Installed model cache verification failed.") from None
    if result.returncode not in (0, 3):
        raise ModelDownloadError("Installed model cache verification failed.")
    return result.returncode == 0


def _verify_file(path: Path, size: int, expected: str) -> None:
    try:
        before = path.lstat()
        if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size != size
                or before.st_uid != os.getuid() or before.st_mode & 0o077):
            raise ModelDownloadError("Downloaded model file type, size or permissions mismatch.")
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
        with os.fdopen(fd, "rb") as source:
            opened = os.fstat(source.fileno())
            if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
                raise ModelDownloadError("Downloaded model file changed during verification.")
            digest = hashlib.sha256()
            count = 0
            while block := source.read(1024 * 1024):
                count += len(block)
                if count > size:
                    raise ModelDownloadError("Downloaded model file exceeds pinned size.")
                digest.update(block)
            after = os.fstat(source.fileno())
            if (count != size or after.st_size != size or after.st_mtime_ns != opened.st_mtime_ns
                    or digest.hexdigest() != expected):
                raise ModelDownloadError("Downloaded model file failed pinned SHA-256 verification.")
    except OSError:
        raise ModelDownloadError("Downloaded model file verification failed.") from None


def curl_bundle(runtime: Path, env: dict[str, str], parent: Path, manifest: dict) -> None:
    """Import a complete verified stage; remove only our own temporary directory."""
    # Imported lazily to keep deployment's preflight independent of model operations.
    import bemind_deploy as deploy

    curl = shutil.which("curl")
    if not curl:
        raise ModelDownloadError("curl is required for the approved model download.")
    with tempfile.TemporaryDirectory(prefix=".model-download-", dir=parent) as name:
        stage = Path(name)
        stage.chmod(0o700)
        network_env = {key: env[key] for key in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy",
                       "ALL_PROXY", "all_proxy", "NO_PROXY", "no_proxy", "SSL_CERT_FILE", "SSL_CERT_DIR",
                       "CURL_CA_BUNDLE") if key in env}
        network_env.update(HOME=str(stage), CURL_HOME=str(stage), XDG_CONFIG_HOME=str(stage))
        for index, (filename, info) in enumerate(manifest["files"].items(), 1):
            target = stage / filename
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            target.parent.chmod(0o700)
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
            os.close(fd)
            url = f"https://huggingface.co/{REPOSITORY}/resolve/{REVISION}/{quote(filename, safe='/')}"
            command = [curl, "--disable", "--fail", "--location", "--proto", "=https",
                       "--proto-redir", "=https", "--connect-timeout", "30", "--max-time", "14400",
                       "--max-filesize", str(info["size"]), "--speed-limit", "1024", "--speed-time", "300",
                       "--header", "Accept-Encoding: identity",
                       "--user-agent", "bemind-pinned-model/1", "--output", str(target)]
            if env.get("LEDGER_CA_BUNDLE"):
                command.extend(("--cacert", env["LEDGER_CA_BUNDLE"]))
            command.append(url)
            print(f"Downloading pinned BGE-M3 file {index}/{len(manifest['files'])} ({info['size']} bytes)...", flush=True)
            try:
                result = subprocess.run(command, cwd=stage, env=network_env, stdin=subprocess.DEVNULL,
                                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=14500, check=False)
            except (OSError, subprocess.SubprocessError):
                raise ModelDownloadError("Pinned model download failed; base installation is retained.") from None
            if result.returncode:
                raise ModelDownloadError("Pinned model download failed; base installation is retained.")
            _verify_file(target, info["size"], info["sha256"])
        deploy.run([str(runtime / ".venv/bin/ledger"), "semantic", "import", str(stage)], runtime,
                   env, "Local model import")
