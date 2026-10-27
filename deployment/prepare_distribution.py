"""Build a deterministic, separately identified runtime source distribution."""
from __future__ import annotations

import argparse
import ast
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import tomllib
import unicodedata
import zipfile

MAX_ARCHIVE = 25 * 1024 * 1024
MAX_MEMBER = 8 * 1024 * 1024
MAX_EXPANDED = 64 * 1024 * 1024
MAX_MEMBERS = 2000


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def safe_path(name):
    parts = (name[:-1] if name.endswith("/") else name).split("/")
    if (not name or len(name) > 240 or "\\" in name or
            any(not p or p in (".", "..") or p.endswith((".", " ")) or
                re.search(r'[\x00-\x1f\x7f:<>"|?*]', p) or
                re.fullmatch(r"(?i)(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?", p)
                for p in parts)):
        raise ValueError("Unsafe archive path")
    return parts


def read_archive(raw, root):
    if len(raw) > MAX_ARCHIVE:
        raise ValueError("Archive size limit")
    files, seen, total = {}, {}, 0
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        if len(archive.infolist()) > MAX_MEMBERS:
            raise ValueError("Archive member limit")
        for item in archive.infolist():
            if item.orig_filename != item.filename:
                raise ValueError("Archive filename was truncated")
            parts = safe_path(item.filename)
            key = unicodedata.normalize("NFC", "/".join(parts)).casefold()
            if parts[0] != root or key in seen:
                raise ValueError("Unexpected root or duplicate archive member")
            seen[key] = item.is_dir()
            mode = stat.S_IFMT(item.external_attr >> 16)
            if (mode not in (0, stat.S_IFREG, stat.S_IFDIR) or
                    (mode == stat.S_IFDIR and not item.is_dir()) or
                    (mode == stat.S_IFREG and item.is_dir()) or item.flag_bits & 1 or
                    item.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)):
                raise ValueError("Unsupported archive member")
            total += item.file_size
            if item.file_size > MAX_MEMBER or total > MAX_EXPANDED:
                raise ValueError("Expanded archive size limit")
            if item.is_dir():
                if item.file_size:
                    raise ValueError("Nonempty archive directory")
                continue
            if len(parts) < 2:
                raise ValueError("File replaces archive root")
            files["/".join(parts[1:])] = (archive.read(item), (item.external_attr >> 16) & 0o777)
    for key in seen:
        for parent in PurePosixPath(key).parents:
            if seen.get(str(parent)) is False:
                raise ValueError("File conflicts with archive directory")
    return files


def validate_recipe(recipe):
    keys = {"schema", "upstream_sha256", "core_version", "distribution_revision", "archive_root",
            "document_sha256", "tests_prefix", "tests_count", "target", "member_count"}
    target_keys = {"path", "sha256", "line_number", "line_sha256", "replacement_line"}
    if (set(recipe) != keys or recipe["schema"] != 1 or
            not isinstance(recipe["target"], dict) or set(recipe["target"]) != target_keys):
        raise ValueError("Unrecognized recipe schema")
    target = recipe["target"]
    hashes = [recipe["upstream_sha256"], target["sha256"], target["line_sha256"]]
    docs = recipe["document_sha256"]
    if not isinstance(docs, list) or len(docs) != 6 or len(set(docs)) != 6:
        raise ValueError("Recipe requires six distinct document hashes")
    if any(not isinstance(h, str) or not re.fullmatch("[0-9a-f]{64}", h) for h in hashes + docs):
        raise ValueError("Invalid recipe hash")
    if (recipe["tests_prefix"] != "tests/" or
            any(type(recipe[k]) is not int or not 0 < recipe[k] <= MAX_MEMBERS
                for k in ("tests_count", "member_count")) or
            type(target["line_number"]) is not int or target["line_number"] < 1 or
            not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)+", recipe["distribution_revision"]) or
            not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+rc[0-9]+", recipe["core_version"]) or
            len(safe_path(recipe["archive_root"])) != 1 or recipe["archive_root"].endswith("/") or
            not target["path"].startswith("src/") or not target["path"].endswith(".py")):
        raise ValueError("Invalid recipe bounds or identity")
    safe_path(target["path"])
    line = target["replacement_line"]
    if not isinstance(line, str) or not line.endswith("\n") or line.count("\n") != 1 or "\r" in line:
        raise ValueError("Replacement must be one complete source line")


def transform(source, target):
    if sha256(source) != target["sha256"]:
        raise ValueError("Target source hash mismatch")
    lines = source.splitlines(keepends=True)
    index = target["line_number"] - 1
    if index >= len(lines) or sha256(lines[index]) != target["line_sha256"]:
        raise ValueError("Target line hash mismatch")
    replacement = target["replacement_line"].encode("utf-8")
    expected = (lines[index].rsplit(b"; ", 1)[0] +
                b"; stop processes using this memory and retry" + lines[index][-2:])
    if not lines[index].endswith((b'"\n', b"'\n")) or replacement != expected:
        raise ValueError("Replacement changes bytes beyond the diagnostic suffix")
    lines[index] = replacement
    result = b"".join(lines)
    before, after = ast.parse(source), ast.parse(result)
    old_nodes, new_nodes = list(ast.walk(before)), list(ast.walk(after))
    changes = []
    if len(old_nodes) != len(new_nodes):
        raise ValueError("AST structure changed")
    for old, new in zip(old_nodes, new_nodes):
        if type(old) is not type(new):
            raise ValueError("AST node type changed")
        if isinstance(old, ast.Constant) and old.value != new.value:
            changes.append((old, new))
    if len(changes) != 1:
        raise ValueError("Expected exactly one AST constant change")
    old, new = changes[0]
    if (not isinstance(old.value, str) or not isinstance(new.value, str) or
            old.lineno != target["line_number"] or new.lineno != target["line_number"] or
            "; " not in old.value or
            old.value.rsplit("; ", 1)[0] != new.value.rsplit("; ", 1)[0] or
            new.value.rsplit("; ", 1)[-1] != "stop processes using this memory and retry"):
        raise ValueError("Change is not the approved diagnostic suffix")
    # Normalize only the approved value, never executable structure or other constants.
    new.value = old.value
    canonical = ast.dump(before, include_attributes=False)
    if canonical != ast.dump(after, include_attributes=False):
        raise ValueError("AST differs beyond the approved constant")
    return result, sha256(canonical.encode())


def prepare(raw, recipe):
    # Check exact release bytes before opening or interpreting the archive.
    if sha256(raw) != recipe.get("upstream_sha256"):
        raise ValueError("Upstream archive hash mismatch")
    validate_recipe(recipe)
    files = read_archive(raw, recipe["archive_root"])
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        if len(archive.infolist()) != recipe["member_count"]:
            raise ValueError("Archive member recipe mismatch")
    if "DISTRIBUTION.json" in files:
        raise ValueError("Input already has a distribution identity")
    if tomllib.loads(files["pyproject.toml"][0].decode())["project"]["version"] != recipe["core_version"]:
        raise ValueError("Core version mismatch")
    excluded, matched, tests = [], [], 0
    for name, (data, _) in files.items():
        digest = sha256(data)
        document = digest in recipe["document_sha256"]
        test = name.startswith(recipe["tests_prefix"])
        if not document and not test:
            continue
        if any(re.search(r"(?i)licen[cs]e|notice|copying|copyright", part) for part in PurePosixPath(name).parts):
            raise ValueError("License or notice exclusion refused")
        if document:
            if "/" in name or not name.endswith(".md") or test:
                raise ValueError("Document exclusion is not a root document")
            matched.append(digest)
        if test:
            tests += 1
        excluded.append(name)
    if sorted(matched) != sorted(recipe["document_sha256"]) or tests != recipe["tests_count"]:
        raise ValueError("Exclusion member recipe mismatch")
    target = recipe["target"]
    original = files[target["path"]][0]
    updated, ast_hash = transform(original, target)
    metadata = {
        "schema": 1, "upstream_sha256": recipe["upstream_sha256"],
        "core_version": recipe["core_version"], "distribution_revision": recipe["distribution_revision"],
        "change_scope": "diagnostic-text-only; one AST string constant; control and data logic unchanged",
        "target_source": {"path": target["path"], "old_sha256": sha256(original), "new_sha256": sha256(updated)},
        "normalized_ast_sha256": ast_hash,
    }
    retained = {name: value for name, value in files.items() if name not in excluded}
    retained[target["path"]] = (updated, files[target["path"]][1])
    retained["DISTRIBUTION.json"] = ((json.dumps(metadata, indent=2, sort_keys=True) + "\n").encode(), 0o644)
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_STORED) as archive:
        for name, (data, mode) in sorted(retained.items()):
            item = zipfile.ZipInfo(recipe["archive_root"] + "/" + name, (1980, 1, 1, 0, 0, 0))
            item.create_system = 3
            item.external_attr = (stat.S_IFREG | mode) << 16
            archive.writestr(item, data)
    evidence = {**metadata, "derived_sha256": sha256(output.getvalue()),
                "excluded_document_count": len(matched), "excluded_test_count": tests,
                "retained_original_files": len(retained) - 1,
                "unchanged_retained_files": len(retained) - 2,
                "ast_changed_string_constants": 1, "original_archive_sha256_verified": True}
    return output.getvalue(), evidence


def audit_distribution(audit_root, blob, source_paths):
    spec = importlib.util.spec_from_file_location("distribution_audit", audit_root / "repo_tools/audit_repository.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    audit = module.Audit()
    if not audit.policy_sha256 or not audit.policy_paths or not audit.policy_text:
        raise ValueError("Publication policy is required")
    for relative, path in source_paths.items():
        safe_path(relative)
        audit.scan(relative, path.read_bytes())
    audit.scan("runtime-source.zip", blob)
    report = {"policy_sha256": audit.policy_sha256, "files": audit.files,
              "archives": audit.archives, "findings": audit.findings,
              "status": "passed" if not audit.findings else "failed"}
    if audit.findings:
        raise ValueError("Distribution publication audit failed: " + json.dumps(report))
    return report


def write_outputs(output, blob):
    sidecar = output.with_name(output.name + ".sha256")
    payloads = {output: blob, sidecar: (sha256(blob) + "  " + output.name + "\n").encode()}
    for path, data in payloads.items():
        if path.is_symlink() or (path.exists() and (not path.is_file() or path.read_bytes() != data)):
            raise ValueError("Output collision")
    output.parent.mkdir(parents=True, exist_ok=True)
    for path, data in payloads.items():
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
        except FileExistsError:
            if path.is_symlink() or not path.is_file() or path.read_bytes() != data:
                raise ValueError("Output collision") from None
        else:
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", required=True, type=Path)
    parser.add_argument("--recipe", type=Path, default=Path(__file__).with_name("core-distribution.json"))
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--audit-root", required=True, type=Path)
    args = parser.parse_args()
    if args.archive.stat().st_size > MAX_ARCHIVE:
        raise ValueError("Archive size limit")
    raw = args.archive.read_bytes()
    recipe = json.loads(args.recipe.read_text())
    blob, evidence = prepare(raw, recipe)
    deployment = Path(__file__).resolve().parent
    evidence["audit"] = audit_distribution(args.audit_root, blob, {
        "deployment/prepare_distribution.py": Path(__file__),
        "deployment/core-distribution.json": args.recipe,
        "deployment/tests/test_distribution.py": deployment / "tests/test_distribution.py",
    })
    if sha256(args.archive.read_bytes()) != recipe["upstream_sha256"]:
        raise ValueError("Input archive changed during preparation")
    write_outputs(args.output, blob)
    print(json.dumps(evidence, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
