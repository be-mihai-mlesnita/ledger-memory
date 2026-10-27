"""Validate a digest-pinned deployment ZIP completely before private extraction."""
from __future__ import annotations

import hashlib
from pathlib import Path
import re
import shutil
import stat
import sys
import zipfile


# This is a code allowlist, not a list supplied by the untrusted ZIP/manifest.
FILES = frozenset({
    "manage.py", "bemind_deploy.py", "jsonc_edit.py", "bootstrap_verify.py",
    "install.sh", "uninstall.sh", "run.sh", "install.ps1", "uninstall.ps1", "run.ps1",
    "README.md", "VALIDATION.md", "MEMORY-RULES.md", "distribution.json",
    "gateway/memory_gateway.py", "gateway/ledger_backend.py",
    "gateway/gateway_backend_contract.py", "gateway/worker_guard.mjs",
})
EXECUTABLES = FILES - {"README.md", "VALIDATION.md", "MEMORY-RULES.md", "distribution.json"}
MAX_FILES = 128
MAX_BYTES = 512 * 1024 * 1024


def extract(archive: Path, destination: Path, layout: str, digest: str) -> None:
    if layout not in {"root", "deployment"} or not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ValueError("Invalid layout or SHA-256")
    if archive.is_symlink() or not archive.is_file() or archive.stat().st_size > MAX_BYTES:
        raise ValueError("Invalid or oversized ZIP")
    if destination.exists() or destination.is_symlink():
        raise ValueError("Extraction destination must not exist")
    prefix = "deployment/" if layout == "deployment" else ""
    # Hash and extract through the same open descriptor, not a reopened input path.
    with archive.open("rb") as source:
        if hashlib.file_digest(source, "sha256").hexdigest() != digest:
            raise ValueError("ZIP SHA-256 mismatch")
        source.seek(0)
        with zipfile.ZipFile(source) as bundle:
            members = bundle.infolist()
            if not members or len(members) > MAX_FILES:
                raise ValueError("Invalid ZIP member count")
            seen: set[str] = set()
            files: set[str] = set()
            total = 0
            core = []
            for info in members:
                name = info.filename
                # orig_filename detects NUL truncation performed by zipfile.
                if info.orig_filename != name or not name.isascii() or "\\" in name:
                    raise ValueError("Unsafe ZIP member name")
                path = name[:-1] if info.is_dir() else name
                parts = path.split("/")
                if any(not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", p) or p in {".", ".."} for p in parts):
                    raise ValueError("Unsafe ZIP member path")
                key = path.casefold()
                if key in seen:
                    raise ValueError("Duplicate/case-colliding ZIP member")
                seen.add(key)
                mode = info.external_attr >> 16
                kind = stat.S_IFMT(mode)
                if info.extra or info.flag_bits & 1 or mode & 0o7000:
                    # No ZIP extra fields: in particular, no Unix hardlink metadata.
                    raise ValueError("ZIP metadata, encryption or special permissions refused")
                if kind not in {0, stat.S_IFDIR if info.is_dir() else stat.S_IFREG}:
                    raise ValueError("ZIP links/special files refused")
                total += info.file_size
                if total > MAX_BYTES or info.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}:
                    raise ValueError("Oversized ZIP or unsupported compression")
                if info.is_dir():
                    if info.file_size != 0:
                        raise ValueError("Nonempty ZIP directory refused")
                    if path not in ({"deployment", "deployment/gateway"} if prefix else {"gateway"}):
                        raise ValueError("Unexpected ZIP directory")
                    continue
                if not name.startswith(prefix):
                    raise ValueError("Member outside declared layout")
                relative = name[len(prefix):]
                is_core = re.fullmatch(r"ledger-memory-[0-9]+\.[0-9]+\.[0-9]+(?:-rc[0-9]+)?\.zip(?:\.sha256)?", relative)
                if relative not in FILES and not is_core:
                    raise ValueError("File outside kit allowlist")
                if mode & 0o111 and relative not in EXECUTABLES:
                    raise ValueError("Executable outside code allowlist")
                if relative.endswith(".zip"):
                    core.append(relative)
                files.add(name)
            required = {prefix + p for p in ("manage.py", "bemind_deploy.py", "install.sh", "uninstall.sh", "run.sh")}
            if not required <= files:
                raise ValueError("Missing required kit entry points")
            if len(core) != 1 or prefix + core[0] + ".sha256" not in files:
                raise ValueError("Kit must contain one core ZIP and its checksum")
            for name in files:
                if any("/".join(name.split("/")[:i]) in files for i in range(1, len(name.split("/")))):
                    raise ValueError("ZIP file/directory collision")
            # Read every member and validate CRC before creating anything on disk.
            if bundle.testzip() is not None:
                raise ValueError("Corrupt ZIP member")
            destination.mkdir(mode=0o700)
            try:
                for info in members:
                    target = destination / info.filename
                    if info.is_dir():
                        target.mkdir(mode=0o700, parents=True, exist_ok=True)
                        continue
                    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                    with bundle.open(info) as src, target.open("xb") as dst:
                        shutil.copyfileobj(src, dst)
                    relative = info.filename[len(prefix):]
                    target.chmod(0o700 if relative in EXECUTABLES else 0o600)
            except BaseException:
                shutil.rmtree(destination)
                raise


if __name__ == "__main__":
    try:
        if len(sys.argv) != 5:
            raise ValueError("Usage: bootstrap_verify.py KIT.zip NEW_DIRECTORY root|deployment SHA256")
        extract(Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3], sys.argv[4])
    except (ValueError, OSError, zipfile.BadZipFile, RuntimeError) as exc:
        print("Kit verification failed: " + str(exc), file=sys.stderr)
        sys.exit(1)
