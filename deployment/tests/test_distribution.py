"""Runtime source packaging invariants using synthetic release fixtures."""
import ast
import importlib.util
import io
import json
from pathlib import Path
import stat
import shutil
import zipfile

import pytest

SPEC = importlib.util.spec_from_file_location("prepare_distribution", Path(__file__).resolve().parents[1] / "prepare_distribution.py")
dist = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(dist)


def fixture_archive(extra=()):
    source = b'def error(path, seconds):\n    return f"busy {path} after {seconds:g} s; retry after shutdown"\n'
    members = [("pyproject.toml", b'[project]\nversion = "0.3.0rc6"\n'),
               ("src/ledger_memory/db_packaging.py", source),
               ("src/ledger_memory/other.py", b"VALUE = 12\n"),
               ("LICENSE", b"Synthetic license grant\n"), ("NOTICE", b"Synthetic notice\n"),
               ("tests/test_sample.py", b"assert True\n")]
    documents = [(f"release-{i}.md", f"Release note {i}\n".encode()) for i in range(6)]
    members.extend(documents)
    members.extend(extra)
    raw = io.BytesIO()
    with zipfile.ZipFile(raw, "w") as archive:
        for name, data in members:
            archive.writestr("ledger-memory/" + name, data)
    recipe = {
        "schema": 1, "upstream_sha256": dist.sha256(raw.getvalue()), "core_version": "0.3.0rc6",
        "distribution_revision": "bemind-runtime-1", "archive_root": "ledger-memory",
        "member_count": len(members), "document_sha256": [dist.sha256(data) for _, data in documents],
        "tests_prefix": "tests/", "tests_count": 1,
        "target": {"path": "src/ledger_memory/db_packaging.py", "sha256": dist.sha256(source),
                   "line_number": 2, "line_sha256": dist.sha256(source.splitlines(keepends=True)[1]),
                   "replacement_line": '    return f"busy {path} after {seconds:g} s; stop processes using this memory and retry"\n'},
    }
    return raw.getvalue(), recipe


def test_correct_transform_and_retained_bytes():
    raw, recipe = fixture_archive()
    blob, evidence = dist.prepare(raw, recipe)
    original = dist.read_archive(raw, "ledger-memory")
    result = dist.read_archive(blob, "ledger-memory")
    target = recipe["target"]["path"]
    assert result.keys() == {"pyproject.toml", target, "src/ledger_memory/other.py", "LICENSE", "NOTICE", "DISTRIBUTION.json"}
    for name in result.keys() - {target, "DISTRIBUTION.json"}:
        assert result[name] == original[name]
    old = list(ast.walk(ast.parse(original[target][0])))
    new = list(ast.walk(ast.parse(result[target][0])))
    changes = [(a, b) for a, b in zip(old, new) if isinstance(a, ast.Constant) and a.value != b.value]
    assert len(changes) == 1
    assert isinstance(changes[0][0].value, str)
    namespace = {}
    exec(result[target][0], namespace)
    assert namespace["error"]("sample.db", 2.0) == "busy sample.db after 2 s; stop processes using this memory and retry"
    assert evidence["ast_changed_string_constants"] == 1
    assert evidence["unchanged_retained_files"] == 4
    metadata = json.loads(result["DISTRIBUTION.json"][0])
    assert metadata["core_version"] == "0.3.0rc6"
    assert metadata["distribution_revision"] == "bemind-runtime-1"


@pytest.mark.parametrize("field", ["upstream", "source", "line", "document", "count", "tests", "schema", "prefix"])
def test_unrecognized_recipe_refused(field):
    raw, recipe = fixture_archive()
    if field == "upstream":
        recipe["upstream_sha256"] = "0" * 64
    elif field == "source":
        recipe["target"]["sha256"] = "0" * 64
    elif field == "line":
        recipe["target"]["line_sha256"] = "0" * 64
    elif field == "document":
        recipe["document_sha256"][0] = "0" * 64
    elif field == "count":
        recipe["member_count"] += 1
    elif field == "tests":
        recipe["tests_count"] += 1
    elif field == "schema":
        recipe["extra"] = True
    else:
        recipe["tests_prefix"] = "src/"
    with pytest.raises(ValueError):
        dist.prepare(raw, recipe)


@pytest.mark.parametrize("line", [
    '    return f"busy {path} after {seconds:g} s; stop processes using this memory and retry" + "extra"\n',
    '    return f"busy {path} after {seconds + 1:g} s; stop processes using this memory and retry"\n',
    '    return f"busy {path} after {seconds:g} s; different text"\n',
    '    return f"changed {path} after {seconds:g} s; stop processes using this memory and retry"\n',
])
def test_non_diagnostic_change_refused(line):
    raw, recipe = fixture_archive()
    recipe["target"]["replacement_line"] = line
    with pytest.raises(ValueError):
        dist.prepare(raw, recipe)


@pytest.mark.parametrize("name", ["LICENSE.md", "NOTICE.md", "COPYING.md", "tests/LICENSE", "tests/sub/NOTICE.txt"])
def test_license_omission_refused(name):
    raw, recipe = fixture_archive([(name, b"Additional legal terms\n")])
    if name.startswith("tests/"):
        recipe["tests_count"] += 1
    else:
        recipe["document_sha256"][0] = dist.sha256(b"Additional legal terms\n")
    with pytest.raises(ValueError, match="License or notice"):
        dist.prepare(raw, recipe)


def test_deterministic_build():
    raw, recipe = fixture_archive()
    first = dist.prepare(raw, recipe)
    second = dist.prepare(raw, json.loads(json.dumps(recipe)))
    assert first == second
    with zipfile.ZipFile(io.BytesIO(first[0])) as archive:
        assert all(i.date_time == (1980, 1, 1, 0, 0, 0) and i.compress_type == zipfile.ZIP_STORED for i in archive.infolist())


@pytest.mark.parametrize("name", ["../escape", "/absolute", "ledger-memory/../escape", "ledger-memory/a\\b", "ledger-memory/a//b", "ledger-memory/a//", "ledger-memory/./a", "ledger-memory/C:drive", "ledger-memory/NUL.txt", "ledger-memory/trailing.", "other/file", "ledger-memory/line\nbreak"])
def test_malformed_paths(name):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(name, b"sample")
    with pytest.raises(ValueError):
        dist.read_archive(buffer.getvalue(), "ledger-memory")


def test_embedded_nul_refused():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("ledger-memory/aXb", b"sample")
    raw = buffer.getvalue().replace(b"ledger-memory/aXb", b"ledger-memory/a\x00b")
    with pytest.raises(ValueError, match="truncated"):
        dist.read_archive(raw, "ledger-memory")


@pytest.mark.parametrize("names", [["a", "a"], ["a", "A"], ["a", "a/b"], ["a/b", "a"]])
def test_duplicate_and_conflicting_members(names):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name in names:
            archive.writestr("ledger-memory/" + name, b"sample")
    with pytest.raises(ValueError):
        dist.read_archive(buffer.getvalue(), "ledger-memory")


@pytest.mark.parametrize("mode", [stat.S_IFLNK, stat.S_IFIFO, stat.S_IFSOCK, stat.S_IFCHR])
def test_special_members_refused(mode):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        item = zipfile.ZipInfo("ledger-memory/link")
        item.create_system = 3
        item.external_attr = (mode | 0o644) << 16
        archive.writestr(item, "target")
    with pytest.raises(ValueError):
        dist.read_archive(buffer.getvalue(), "ledger-memory")


def test_bounds(monkeypatch):
    raw, _ = fixture_archive()
    for name, limit in [("MAX_ARCHIVE", 1), ("MAX_MEMBER", 1), ("MAX_EXPANDED", 1), ("MAX_MEMBERS", 1)]:
        with monkeypatch.context() as patch:
            patch.setattr(dist, name, limit)
            with pytest.raises(ValueError):
                dist.read_archive(raw, "ledger-memory")


@pytest.mark.parametrize("collision", ["archive", "sidecar", "symlink"])
def test_output_collision(tmp_path, collision):
    output = tmp_path / "runtime.zip"
    target = output if collision != "sidecar" else tmp_path / "runtime.zip.sha256"
    if collision == "symlink":
        target.symlink_to(tmp_path / "absent")
    else:
        target.write_bytes(b"existing")
    with pytest.raises(ValueError, match="collision"):
        dist.write_outputs(output, b"different")
    if collision != "symlink":
        assert target.read_bytes() == b"existing"
    if collision == "sidecar":
        assert not output.exists()


def test_same_output_is_idempotent(tmp_path):
    output = tmp_path / "runtime.zip"
    dist.write_outputs(output, b"same")
    before = output.stat().st_mtime_ns
    dist.write_outputs(output, b"same")
    assert output.stat().st_mtime_ns == before
    assert output.with_name(output.name + ".sha256").read_text() == dist.sha256(b"same") + "  runtime.zip\n"


@pytest.mark.parametrize("mode", ["pass", "source", "archive", "missing-policy"])
def test_real_audit_fail_closed(tmp_path, mode):
    root = next((p for p in Path(__file__).resolve().parents if (p / "repo_tools/audit_repository.py").is_file()), None)
    assert root is not None, "Run with the repository audit tool available"
    (tmp_path / "repo_tools").mkdir()
    shutil.copyfile(root / "repo_tools/audit_repository.py", tmp_path / "repo_tools/audit_repository.py")
    if mode != "missing-policy":
        (tmp_path / ".repo-local").mkdir()
        (tmp_path / ".repo-local/publication-policy.json").write_text(json.dumps({
            "path_patterns": [{"id": "path", "regex": "^local/"}],
            "content_patterns": [{"id": "text", "regex": "blocked-marker"}],
        }))
    source = tmp_path / "source.py"
    source.write_text("blocked-marker" if mode == "source" else "value = 1\n")
    raw, recipe = fixture_archive([("README.md", b"blocked-marker" if mode == "archive" else b"Neutral description")])
    blob, _ = dist.prepare(raw, recipe)
    paths = {"deployment/prepare_distribution.py": source}
    if mode == "pass":
        report = dist.audit_distribution(tmp_path, blob, paths)
        assert report["status"] == "passed" and report["policy_sha256"]
        assert report["archives"] == 1
    else:
        with pytest.raises(ValueError):
            dist.audit_distribution(tmp_path, blob, paths)
