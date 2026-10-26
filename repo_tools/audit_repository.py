"""Read-only publication audit of candidate/staged project files and nested archives.

This is a fail-closed heuristic aid, not a guarantee that all sensitive data is detected.
It never prints matched credentials. Reviewed synthetic literals can be allowlisted by
their exact match digest, with a reason, in security-fixtures.json beside this script.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import tarfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]
MAX_FILE = 25 * 1024 * 1024
MAX_EXPANDED = 300 * 1024 * 1024
MAX_DEPTH = 4
PRIVATE_NAMES = {"auth.json", "auth.key", "reader.token", "credentials.json", "supervisor.json",
                 "active-request.json", "verified-model.json", "local.settings.json"}
PRIVATE_SUFFIXES = (".db", ".db-wal", ".db-shm", ".db.xz", ".sqlite", ".sqlite3", ".sqlite3-wal",
                    ".sqlite3-shm", ".pem", ".p12", ".pfx", ".key", ".gguf", ".safetensors", ".onnx", ".onnx_data", ".onnx.data")
PATTERNS = {
    "private_key": re.compile(r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----\s+[A-Za-z0-9+/=\r\n]{64,}"),
    "jwt": re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),
    "github_token": re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{50,})\b"),
    "openai_key": re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{24,}\b"),
    "aws_access_key": re.compile(r"\bAKIA[A-Z0-9]{16}\b"),
    "bearer_literal": re.compile(r"\bBearer[ \t]+[A-Za-z0-9._~+/-]{24,}=*", re.I),
    "credential_assignment": re.compile(r"\b(?:api[_ -]?key|access[_ -]?token|refresh[_ -]?token|client[_ -]?secret|password)[\"']?\s*[:=]\s*[\"'][^\s\"'\r\n]{20,}[\"']", re.I),
    "signed_url": re.compile(r"https?://[^\s\"']+[?&](?:sig|x-amz-signature|x-goog-signature)=[A-Za-z0-9%+/=_-]{24,}", re.I),
}
BENIGN_BINARY = {".png", ".jpg", ".jpeg", ".gif", ".ico", ".pdf", ".woff", ".woff2", ".ttf"}


def git(*arguments):
    return subprocess.check_output(["git", *arguments], cwd=ROOT)


class Audit:
    def __init__(self):
        allow = Path(__file__).with_name("security-fixtures.json")
        self.allow = json.loads(allow.read_text()) if allow.exists() else {}
        if any(not re.fullmatch(r"[0-9a-f]{64}", key) or not isinstance(value, str) or not value.strip()
               for key, value in self.allow.items()):
            raise ValueError("Invalid synthetic fixture allowlist")
        self.findings = []
        self.expanded = 0
        self.files = 0
        self.archives = 0
        self.seen = set()
        self.allowed_matches = 0
        self.top_inventory = []
        self.policy_paths = []
        self.policy_text = []
        policy = ROOT / ".repo-local/publication-policy.json"
        self.policy_sha256 = None
        if policy.is_file():
            raw = policy.read_bytes()
            try:
                document = json.loads(raw)
                for key, target in (("path_patterns", self.policy_paths), ("content_patterns", self.policy_text)):
                    for rule in document[key]:
                        if not isinstance(rule["id"], str) or not rule["id"]:
                            raise ValueError()
                        target.append((rule["id"], re.compile(rule["regex"])))
                if not self.policy_paths or not self.policy_text:
                    raise ValueError()
            except (ValueError, KeyError, TypeError, re.error):
                raise ValueError("Invalid local publication policy") from None
            self.policy_sha256 = hashlib.sha256(raw).hexdigest()

    def issue(self, path, code, **extra):
        self.findings.append({"path": path, "code": code, **extra})

    def path_check(self, path):
        for identifier, pattern in self.policy_paths:
            if pattern.search(path):
                self.issue(path, "publication_policy_path", rule=identifier)
        name = path.rsplit("!", 1)[-1]
        parts = PurePosixPath(name).parts
        base = PurePosixPath(name).name.lower()
        if "\\" in name or name.startswith("/") or ".." in parts or any(part.lower() == ".git" for part in parts):
            self.issue(path, "unsafe_path")
        if base in PRIVATE_NAMES or base.endswith(PRIVATE_SUFFIXES) or base == ".env" or (
                base.startswith(".env.") and base not in (".env.example", ".env.template")):
            self.issue(path, "private_or_runtime_file")
        if any(part in (".venv", "node_modules", "__pycache__") for part in parts):
            self.issue(path, "generated_environment")

    def scan(self, path, data, depth=0):
        self.path_check(path)
        if len(data) > MAX_FILE:
            self.issue(path, "file_too_large", bytes=len(data))
            return
        self.files += 1
        digest = hashlib.sha256(data).hexdigest()
        if digest in self.seen:
            return
        self.seen.add(digest)
        self.expanded += len(data)
        if self.expanded > MAX_EXPANDED or depth > MAX_DEPTH:
            self.issue(path, "audit_size_or_depth_limit")
            return
        lower = path.lower()
        try:
            if lower.endswith(".zip"):
                self.archives += 1
                with zipfile.ZipFile(io.BytesIO(data)) as archive:
                    if len(archive.infolist()) > 20000:
                        self.issue(path, "archive_member_limit")
                        return
                    for item in archive.infolist():
                        nested = path + "!" + item.filename
                        self.path_check(nested)
                        mode = stat.S_IFMT(item.external_attr >> 16)
                        if mode not in (0, stat.S_IFREG, stat.S_IFDIR):
                            self.issue(nested, "archive_special_file")
                        if item.file_size > MAX_FILE:
                            self.issue(nested, "file_too_large", bytes=item.file_size)
                        elif not item.is_dir():
                            if self.expanded + item.file_size > MAX_EXPANDED:
                                self.issue(path, "audit_size_or_depth_limit")
                                break
                            self.scan(nested, archive.read(item), depth + 1)
                return
            if lower.endswith((".tar.gz", ".tgz", ".tar.xz", ".tar")):
                self.archives += 1
                with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as archive:
                    for index, item in enumerate(archive):
                        if index >= 20000:
                            self.issue(path, "archive_member_limit")
                            break
                        nested = path + "!" + item.name
                        self.path_check(nested)
                        if not (item.isfile() or item.isdir()):
                            self.issue(nested, "archive_special_file")
                        elif item.size > MAX_FILE:
                            self.issue(nested, "file_too_large", bytes=item.size)
                        elif item.isfile():
                            if self.expanded + item.size > MAX_EXPANDED:
                                self.issue(path, "audit_size_or_depth_limit")
                                break
                            with archive.extractfile(item) as stream:
                                self.scan(nested, stream.read(MAX_FILE + 1), depth + 1)
                return
        except (ValueError, OSError, zipfile.BadZipFile, tarfile.TarError, RuntimeError):
            self.issue(path, "archive_unreadable")
            return
        try:
            text = data.decode("utf-8")
            if "\x00" in text:
                raise UnicodeError()
        except UnicodeError:
            if PurePosixPath(path).suffix.lower() not in BENIGN_BINARY:
                self.issue(path, "unreviewed_binary")
            return
        for identifier, pattern in self.policy_text:
            match = pattern.search(text)
            if match:
                self.issue(path, "publication_policy_content", rule=identifier,
                           line=text.count("\n", 0, match.start()) + 1)
        for code, pattern in PATTERNS.items():
            for match in pattern.finditer(text):
                fingerprint = hashlib.sha256(match.group().encode()).hexdigest()
                if fingerprint in self.allow:
                    self.allowed_matches += 1
                    continue
                line = text.count("\n", 0, match.start()) + 1
                start = text.rfind("\n", 0, match.start()) + 1
                end = text.find("\n", match.end())
                context = text[start:len(text) if end < 0 else end]
                for redactor in PATTERNS.values():
                    context = redactor.sub("[REDACTED CANDIDATE]", context)
                self.issue(path, code, line=line, fingerprint=fingerprint, context=context[:240])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--staged", action="store_true", help="Scan Git index blobs, not working-tree copies")
    parser.add_argument("--require-policy", action="store_true", help="Refuse without an approved local publication policy")
    parser.add_argument("--report", type=Path, help="Optional owner-private local JSON output")
    args = parser.parse_args()
    scan = Audit()
    if args.require_policy and scan.policy_sha256 is None:
        raise SystemExit("Local publication policy is required")
    files = git("ls-files", "-z", "--cached", *( [] if args.staged else ["--others", "--exclude-standard"]))
    paths = sorted(set(path for path in files.decode().split("\0") if path))
    for relative in paths:
        target = ROOT / relative
        if args.staged:
            entries = git("ls-files", "-s", "--", relative).decode().splitlines()
            if len(entries) != 1 or not entries[0].startswith(("100644 ", "100755 ")):
                scan.issue(relative, "non_regular_index_entry")
                continue
            data = git("show", ":" + relative)
        else:
            if not target.exists():
                continue
            if target.is_symlink() or not target.is_file() or target.stat().st_nlink != 1:
                scan.issue(relative, "non_regular_file")
                continue
            if target.stat().st_size > MAX_FILE:
                scan.issue(relative, "file_too_large", bytes=target.stat().st_size)
                continue
            data = target.read_bytes()
        scan.top_inventory.append({"path": relative, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()})
        scan.scan(relative, data)
    result = {"status": "passed" if not scan.findings else "review_required", "staged": args.staged,
              "local_policy_applied": scan.policy_sha256 is not None, "local_policy_sha256": scan.policy_sha256,
              "top_level_files": len(scan.top_inventory), "file_visits_including_archives": scan.files,
              "unique_blobs": len(scan.seen), "archives_opened": scan.archives,
              "unique_bytes_examined": scan.expanded, "reviewed_synthetic_matches": scan.allowed_matches,
              "findings": scan.findings, "inventory": scan.top_inventory,
              "limits": "Heuristic checks; binary allowlist, size/depth limits and literal-pattern coverage do not prove perfect secrecy."}
    if args.report:
        fd = os.open(args.report, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as stream:
            json.dump(result, stream, indent=2)
            stream.write("\n")
    print(json.dumps({key: value for key, value in result.items() if key != "inventory"}, indent=2))
    raise SystemExit(0 if not scan.findings else 1)


if __name__ == "__main__":
    main()
