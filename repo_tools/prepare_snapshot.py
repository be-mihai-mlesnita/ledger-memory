"""Select source and technical reference files from a checksum-verified archive."""
from __future__ import annotations

import hashlib
import argparse
import io
import json
import os
from pathlib import Path
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.dont_write_bytecode = True
sys.path.insert(0, str(ROOT / "deployment"))
from bemind_deploy import verified_archive

ROOT_FILES = {".gitignore", ".gitattributes", "README.md", "MEMORY-RULES.md", "START-PROMPT.md",
              "pyproject.toml", "uv.lock", "install.sh", "install.ps1", "uninstall.sh",
              "LICENSE", "LICENSE.md", "LICENSE.txt", "NOTICE"}


def selected(relative):
    return relative.parts[0] in {"src", "tests", "install"} or relative.as_posix() in ROOT_FILES


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--refresh-metadata-only", action="store_true")
    args = parser.parse_args()
    archive_path = ROOT / "builds/ledger-memory-0.3.0-rc3.zip"
    target = ROOT / "upstream/ledger-memory"
    manifest = ROOT / "upstream/RELEASE.json"
    if (target.exists() or manifest.exists()) and not args.refresh_metadata_only:
        raise SystemExit("Snapshot already exists. Review Git/local changes before any refresh; no overwrite performed.")
    if args.refresh_metadata_only and not target.is_dir():
        raise SystemExit("No existing source snapshot to verify")
    blob, checksum, version = verified_archive(archive_path)
    inventory = []
    with zipfile.ZipFile(io.BytesIO(blob)) as archive:
        if any(".git" in Path(info.filename).parts for info in archive.infolist()):
            raise SystemExit("Nested Git metadata refused")
        if not args.refresh_metadata_only:
            target.mkdir()
        for info in archive.infolist():
            relative = Path(info.filename).relative_to("ledger-memory")
            if relative == Path("."):
                continue
            if not selected(relative):
                continue
            destination = target / relative
            if info.is_dir():
                if not args.refresh_metadata_only:
                    destination.mkdir(parents=True, exist_ok=True)
                continue
            content = archive.read(info)
            executable = bool((info.external_attr >> 16) & 0o111)
            if not args.refresh_metadata_only:
                destination.parent.mkdir(parents=True, exist_ok=True)
                with destination.open("xb") as stream:
                    stream.write(content)
                destination.chmod(0o755 if executable else 0o644)
            assert destination.read_bytes() == content
            inventory.append({"path": relative.as_posix(), "sha256": hashlib.sha256(content).hexdigest(),
                              "bytes": len(content), "executable": executable})
    inventory.sort(key=lambda item: item["path"])
    tree_hash = hashlib.sha256(json.dumps(inventory, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    record = {"version": version, "archive": archive_path.relative_to(ROOT).as_posix(), "sha256": checksum,
              "snapshot": "upstream/ledger-memory", "file_count": len(inventory), "tree_sha256": tree_hash,
              "content_modified": False, "complete_archive": False, "files": inventory}
    pending = manifest.with_suffix(".json.pending")
    with pending.open("x") as stream:
        json.dump(record, stream, indent=2)
        stream.write("\n")
    os.replace(pending, manifest)
    print(json.dumps({key: record[key] for key in ("version", "sha256", "file_count", "tree_sha256", "content_modified")}, indent=2))


if __name__ == "__main__":
    main()
