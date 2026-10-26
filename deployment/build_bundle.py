"""Package only generic deployment code plus the unchanged verified rc3 release."""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import zipfile

sys.dont_write_bytecode = True
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from bemind_deploy import ARCHIVE_NAME, verified_archive

NAME = "bemind-ledger-memory-0.3.0-rc3-deployment.zip"
FILES = ["install.sh", "uninstall.sh", "run.sh", "install.ps1", "uninstall.ps1", "run.ps1",
         "bemind_deploy.py", "jsonc_edit.py", "MEMORY-RULES.md", "README.md", "VALIDATION.md",
         "gateway/memory_gateway.py", "gateway/ledger_backend.py",
         "gateway/gateway_backend_contract.py", "gateway/worker_guard.mjs"]


def publish(path: Path, blob: bytes):
    if not path.parent.is_dir():
        raise RuntimeError("Output directory must already exist")
    if path.is_symlink() or path.with_suffix(path.suffix + ".sha256").is_symlink():
        raise RuntimeError("Symlinked output refused")
    backup = None
    if path.exists():
        old = path.read_bytes()
        old_hash = hashlib.sha256(old).hexdigest()
        if old != blob:
            backup = path.with_name(path.stem + ".previous-" + old_hash[:12] + ".zip")
            if backup.exists():
                if backup.read_bytes() != old:
                    raise RuntimeError("Backup collision")
            else:
                with backup.open("xb") as stream:
                    stream.write(old)
            backup.with_suffix(backup.suffix + ".sha256").write_text(old_hash + "  " + backup.name + "\n")
    fd, pending = tempfile.mkstemp(prefix=".bundle-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(blob)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(pending, path)
    finally:
        if os.path.exists(pending):
            os.unlink(pending)
    sha = hashlib.sha256(blob).hexdigest()
    path.with_suffix(path.suffix + ".sha256").write_text(sha + "  " + path.name + "\n")
    return {"path": str(path), "sha256": sha, "previous": str(backup) if backup else None}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release", type=Path, default=HERE.parent / "builds" / ARCHIVE_NAME)
    parser.add_argument("--output", type=Path, action="append", required=True)
    args = parser.parse_args()
    release, release_hash, version = verified_archive(args.release)
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for relative in FILES:
            source = HERE / relative
            if not source.is_file() or source.is_symlink():
                raise RuntimeError("Missing or symlinked source: " + relative)
            archive.write(source, relative)
        archive.writestr(ARCHIVE_NAME, release)
        archive.writestr(ARCHIVE_NAME + ".sha256", release_hash + "  " + ARCHIVE_NAME + "\n")
    blob = output.getvalue()
    with zipfile.ZipFile(io.BytesIO(blob)) as archive:
        assert set(archive.namelist()) == set(FILES + [ARCHIVE_NAME, ARCHIVE_NAME + ".sha256"])
        assert archive.testzip() is None
        assert hashlib.sha256(archive.read(ARCHIVE_NAME)).hexdigest() == release_hash
    print(json.dumps({"release": version, "release_sha256": release_hash,
                      "files": len(FILES) + 2, "outputs": [publish(path, blob) for path in args.output]}, indent=2))


if __name__ == "__main__":
    main()
