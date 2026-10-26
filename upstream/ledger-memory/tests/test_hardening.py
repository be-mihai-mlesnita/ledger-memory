"""Review follow-ups: portability, concurrency, upgrade safety, path confinement, budgets, permissions, text hygiene."""
from __future__ import annotations

import ast
import json
import multiprocessing
import os
import socket
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from ledger_memory import api, cli, db, reader, selftest
from ledger_memory.api import Ledger, LedgerError
from ledger_memory.token_budget import _estimate_text_tokens

from test_read_path import _make_0_1_0_memory
from conftest import process_boundary_offenders as _process_boundary_offenders

SRC = Path(api.__file__).parent


def _con(L):
    return sqlite3.connect(L.db_path)


# ---- B1: platforms without O_NOFOLLOW / O_BINARY

def test_backup_and_restore_work_without_o_nofollow_and_a_symlink_target_is_still_refused(home, tmp_path, monkeypatch):
    for name in ("O_NOFOLLOW", "O_BINARY", "O_DIRECTORY"):
        monkeypatch.delattr(os, name, raising=False)
    L = Ledger().ensure()
    L.remember("The rack is in room 4", source="user_said")
    b = L.backup()
    out = tmp_path / "restored.db"
    assert L.restore(b["archive_path"], str(out))["sha256_verified"] is True
    target = tmp_path / "victim.txt"; target.write_text("keep")
    link = tmp_path / "linked.db"; link.symlink_to(target)
    with pytest.raises(LedgerError):
        L.restore(b["archive_path"], str(link), force=True)
    assert target.read_text() == "keep"
    from ledger_memory import fs_safety
    planted = tmp_path / "planted"; planted.symlink_to(target)
    with pytest.raises(FileExistsError):
        fs_safety.exclusive_write_bytes(planted, b"x")


def test_no_unguarded_o_nofollow_in_shipped_code():
    for name in ("fs_safety.py", "db_packaging.py"):
        for node in ast.walk(ast.parse((SRC / name).read_text())):
            if isinstance(node, ast.Attribute) and node.attr in {"O_NOFOLLOW", "O_DIRECTORY", "O_BINARY"}:
                raise AssertionError(f"{name}:{node.lineno} uses os.{node.attr} unguarded")


# ---- S1: concurrent first start / upgrade

def _start_many(home: Path, n: int = 8) -> list[int]:
    code = "from ledger_memory.api import Ledger; Ledger().ensure()"
    env = {**os.environ, "LEDGER_HOME": str(home)}
    procs = [subprocess.Popen([sys.executable, "-c", code], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
             for _ in range(n)]
    results = []
    for p in procs:
        out, err = p.communicate(timeout=120)
        results.append((p.returncode, err.decode()[-400:]))
    assert all(rc == 0 for rc, _ in results), results
    return [rc for rc, _ in results]


def _active_readers(home: Path) -> int:
    con = sqlite3.connect(home / "memory.db")
    try:
        return con.execute("select count(*) from client_devices where name = 'local-reader' and revoked_at is null").fetchone()[0]
    finally:
        con.close()


def test_eight_processes_on_a_fresh_home(home):
    _start_many(home)
    assert _active_readers(home) == 1
    assert db.migration_status(home / "memory.db") == ("current", [])
    con = sqlite3.connect(home / "memory.db")
    assert con.execute("select count(*) from actors where name = 'owner'").fetchone()[0] == 1
    con.close()


def test_eight_processes_on_a_0_1_0_home_take_exactly_one_backup(home):
    _make_0_1_0_memory(home)
    _start_many(home)
    assert len(list((home / "backups").glob("*pre-upgrade*.xz"))) == 1
    assert _active_readers(home) == 1
    assert db.migration_status(home / "memory.db") == ("current", [])


def test_a_losing_migration_attempt_skips_instead_of_dying(tmp_path):
    p = tmp_path / "m.db"
    db.initialize_database(p)
    with db.connect(p) as c:
        name, sql = db._load_migrations()[1]
        assert db._apply_one_migration(c, name, sql) is False            # already applied: skipped, no exception


# ---- S2: upgrade failures are refusals; the backup is verified first

def test_cli_prints_refused_when_the_upgrade_fails(home, monkeypatch, capsys):
    _make_0_1_0_memory(home)
    monkeypatch.setattr(Ledger, "_verify_backup", lambda self, made: (_ for _ in ()).throw(LedgerError("upgrade aborted: test")))
    assert cli.main(["status"]) == 2
    err = capsys.readouterr().err
    assert err.startswith("refused: upgrade aborted") and "Traceback" not in err
    assert db.migration_status(home / "memory.db")[0] == "behind"        # nothing was migrated


def test_a_backup_that_does_not_verify_aborts_the_upgrade(home, monkeypatch):
    _make_0_1_0_memory(home)
    real = Ledger.backup

    def corrupt(self, label="ledger"):
        made = real(self, label=label)
        Path(made["archive_path"]).write_bytes(b"not the archive")        # sha256 no longer matches
        return made
    monkeypatch.setattr(Ledger, "backup", corrupt)
    with pytest.raises(LedgerError, match="did not verify"):
        Ledger().ensure()
    assert db.migration_status(home / "memory.db")[0] == "behind"


def test_a_backup_with_the_wrong_row_count_aborts_the_upgrade(home, monkeypatch):
    path = _make_0_1_0_memory(home)
    from ledger_memory import facts
    facts.add_fact(path, statement="one", fact_type="note", confidence=None, actor_id=1, authority_class="system_observed")
    L = Ledger()
    real = db_pack = api.db_packaging.package_database

    def stale(*a, **k):
        r = real(*a, **k)
        facts.add_fact(path, statement="written after the backup", fact_type="note", confidence=None, actor_id=1,
                       authority_class="system_observed")
        return r
    monkeypatch.setattr(api.db_packaging, "package_database", stale)
    with pytest.raises(LedgerError, match="holds"):
        L.ensure()
    assert db.migration_status(path)[0] == "behind"


# ---- S3: path confinement for the agent-facing ingest

def _mcp_ingest(L, path, cwd):
    from ledger_memory import mcp_server
    mcp_server.START_CWD = cwd.resolve()
    mcp_server._ledger = L
    return mcp_server.ingest_document(path=str(path))


def test_ingest_over_mcp_is_confined(home, tmp_path):
    L = Ledger().ensure()
    proj = tmp_path / "proj"; (proj / "docs").mkdir(parents=True); (proj / ".git").mkdir()
    ok = proj / "docs" / "note.txt"; ok.write_text("Project note about the rack.")
    hidden = proj / ".git" / "config"; hidden.write_text("secret")
    outside = tmp_path / "outside.txt"; outside.write_text("not in the project")
    ssh = tmp_path / ".ssh" / "id_x"; ssh.parent.mkdir(); ssh.write_text("key")
    (proj / "link.txt").symlink_to(outside)
    (proj / "dotlink.txt").symlink_to(hidden)
    assert _mcp_ingest(L, ok, proj)["chunks"] >= 1
    (L.home / "inbox").mkdir(exist_ok=True)
    inbox_file = L.home / "inbox" / "dropped.txt"; inbox_file.write_text("Dropped in the inbox.")
    assert _mcp_ingest(L, inbox_file, proj)["chunks"] >= 1
    for bad in (hidden, outside, ssh, proj / "link.txt", proj / "dotlink.txt", Path("~/.ssh/id_rsa").expanduser()):
        with pytest.raises(Exception, match="path not allowed.*Paste the text"):
            _mcp_ingest(L, bad, proj)
    assert L.ingest(text="pasted text is unaffected")["chunks"] >= 1


def test_the_cli_ingest_keeps_full_path_access(home, tmp_path, capsys):
    f = tmp_path / ".hidden" / "x.txt"; f.parent.mkdir(); f.write_text("A person typed this path.")
    assert cli.main(["ingest", str(f)]) == 0
    assert '"chunks": 1' in capsys.readouterr().out


# ---- S4: the whole payload fits the (raised) budget

@pytest.mark.parametrize("budget", [200, 500, 1500])
def test_brief_payload_estimate_fits_the_budget(home, budget):
    L = Ledger().ensure()
    for i in range(40):
        L.remember(f"The deployment checklist step {i} requires a signed ticket and a second reviewer", source="user_said")
    a = L.remember("The release window is Friday", source="user_said")
    b = L.remember("The release window is Sunday", source="observed")
    L.dispute(a["id"], reason="conflict", conflicts_with=b["id"])
    L.record_decision("Release window", summary="Use Friday", rationale="r", fact_id=a["id"])
    out = L.brief("deployment checklist ticket reviewer release window", budget_tokens=budget)
    payload = json.dumps(out, ensure_ascii=False, default=str)
    assert _estimate_text_tokens(payload) <= budget and out["tokens_estimated"] == _estimate_text_tokens(payload)
    assert out["facts"] or budget == 200


def test_a_budget_below_200_is_raised_to_200(home):
    L = Ledger().ensure()
    L.remember("The release window is Friday", source="user_said")
    out = L.brief("release window", budget_tokens=5)
    assert out["budget_tokens"] == 200 and out["tokens_estimated"] <= 200


# ---- S5: synthetic selftest never probes a real profile or model service

def test_default_selftest_makes_no_connection_when_no_backend_is_configured(home, monkeypatch, capsys):
    def refuse(*a, **k):
        raise AssertionError("network attempted")
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket.socket, "connect", refuse)
    assert selftest.run() == 0
    assert "not configured" in capsys.readouterr().out
    assert not home.exists()


@pytest.mark.parametrize("backend", ["lmstudio", "bge-m3", "none"])
def test_default_selftest_never_probes_configured_backend(home, monkeypatch, capsys, backend):
    monkeypatch.setenv("LEDGER_EMBEDDING_BACKEND", backend)
    external = home.parent / "external-vector.db"
    external.write_bytes(b"synthetic untouched sidecar sentinel")
    monkeypatch.setenv("LEDGER_VECTORS_PATH", str(external))
    from ledger_memory import embedding_worker, model_artifacts
    def refuse(*args, **kwargs):
        raise AssertionError("real profile or service attempted")
    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(embedding_worker, "worker_profile", refuse)
    monkeypatch.setattr(model_artifacts, "download_model_bundle", refuse)
    assert selftest.run() == 0
    output = capsys.readouterr().out
    assert "not configured" in output or "offline run" in output
    assert os.environ["LEDGER_EMBEDDING_BACKEND"] == backend
    assert os.environ["LEDGER_VECTORS_PATH"] == str(external)
    assert external.read_bytes() == b"synthetic untouched sidecar sentinel"
    assert not home.exists()


def _retired_adapter_imports(source):
    retired = "lm" + "studio"
    offenders = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if (module.endswith(f"adapters.{retired}")
                    or (module.endswith("adapters") or (not module and node.level))
                    and any(alias.name == retired for alias in node.names)):
                offenders.append(node.lineno)
        elif isinstance(node, ast.Import) and any(
                alias.name.endswith(f"adapters.{retired}") for alias in node.names):
            offenders.append(node.lineno)
    return offenders


def test_retired_server_adapter_and_imports_cannot_return():
    retired = "lm" + "studio"
    assert not (SRC / "adapters" / f"{retired}.py").exists()
    offenders = []
    for path in SRC.rglob("*.py"):
        offenders.extend((path.name, line) for line in _retired_adapter_imports(path.read_text()))
    assert offenders == []


@pytest.mark.parametrize("source", [
    "import ledger_memory.adapters.{retired}",
    "from ledger_memory.adapters.{retired} import Backend",
    "from ledger_memory.adapters import {retired}",
    "from .adapters import {retired}",
    "from . import {retired}",
])
def test_retired_adapter_guard_rejects_import_mutations(source):
    assert _retired_adapter_imports(source.format(retired="lm" + "studio")) == [1]
    assert _retired_adapter_imports("from ledger_memory.adapters import onnx_bge") == []


@pytest.mark.parametrize("extra,reason", [
    ("embedding_worker.py", "counted integration"),
    ("bge-m3-manifest.json", "counted integration"),
    ("unexpected.py", "unclassified output"),
])
def test_extractor_refuses_unsafe_replay_before_any_write(tmp_path, monkeypatch, extra, reason):
    import importlib.util
    tool_path = SRC.parent.parent / "tools" / "extract.py"
    spec = importlib.util.spec_from_file_location("isolated_extract_guard", tool_path)
    tool = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tool)
    destination = tmp_path / "output"
    destination.mkdir()
    (destination / "facts.py").write_text("synthetic preserved generated module")
    tool.preflight_output_tree(destination)  # Positive control: this known generated entry is visible.
    (destination / extra).write_text("synthetic preserved output")
    before = {p.name: p.read_bytes() for p in destination.iterdir()}
    monkeypatch.setattr(tool, "DEST", destination)
    with pytest.raises(SystemExit, match=reason):
        tool.copy_and_rewrite(tmp_path / "absent-source", tool.Edit(destination))
    assert {p.name: p.read_bytes() for p in destination.iterdir()} == before


def test_counted_integration_ports_match_current_generated_targets():
    import hashlib
    import importlib.util
    spec = importlib.util.spec_from_file_location("counted_port_inventory", SRC.parent.parent / "tools" / "extract.py")
    tool = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tool)
    required = {"embeddings.py", "semantic_rebuild.py", "semantic_search.py", "semantic_readiness.py",
                "semantic_binding.py", "rag_search.py", "chunk_embeddings.py", "chunkers.py",
                "embedding_recipe.py", "mental_model_compiler.py", "context_tool.py", "candidates.py"}
    assert set(tool.COUNTED_INTEGRATION_PORTS) == required
    assert all(name.removesuffix(".py") not in tool.HANDWRITTEN for name in required)
    for name, (before, replacements, after) in tool.COUNTED_INTEGRATION_PORTS.items():
        assert before != after
        assert replacements and all(old and old != new for old, new in replacements)
        assert hashlib.sha256((SRC / name).read_bytes()).hexdigest() == after


@pytest.mark.parametrize("mutation", ["none", "source-drift", "duplicate-anchor"])
def test_counted_integration_refuses_drift_and_ambiguous_anchors(tmp_path, monkeypatch, mutation):
    import hashlib
    import importlib.util
    spec = importlib.util.spec_from_file_location("counted_port_contract", SRC.parent.parent / "tools" / "extract.py")
    tool = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tool)
    initial = "prefix before suffix" if mutation != "duplicate-anchor" else "before before"
    path = tmp_path / "synthetic.py"
    path.write_text(initial)
    digest = lambda text: hashlib.sha256(text.encode()).hexdigest()
    monkeypatch.setattr(tool, "COUNTED_INTEGRATION_PORTS", {
        "synthetic.py": (digest("different" if mutation == "source-drift" else initial),
                         (("before", "after"),), digest("prefix after suffix"))})
    if mutation == "none":
        tool.apply_counted_integration_ports(tool.Edit(tmp_path))
        assert path.read_text() == "prefix after suffix"
    else:
        with pytest.raises(SystemExit, match="fingerprint drift|expected 1"):
            tool.apply_counted_integration_ports(tool.Edit(tmp_path))
        assert path.read_text() == initial


@pytest.mark.parametrize("mutation", ["later-source", "later-anchor", "posthash", "whole-file", "exempt"])
def test_integration_batch_validates_before_first_write(tmp_path, monkeypatch, mutation):
    import hashlib
    import importlib.util
    spec = importlib.util.spec_from_file_location("integration_batch_contract", SRC.parent.parent / "tools" / "extract.py")
    tool = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tool)
    digest = lambda text: hashlib.sha256(text.encode()).hexdigest()
    initial = "prefix before suffix"
    for name in ("first.py", "second.py"):
        (tmp_path / name).write_text(initial)
    ports = {name: (digest(initial), (("before", "after"),), digest("prefix after suffix"))
             for name in ("first.py", "second.py")}
    before, edits, after = ports["second.py"]
    if mutation == "later-source":
        before = digest("different")
    elif mutation == "later-anchor":
        edits = (("missing", "after"),)
    elif mutation == "posthash":
        after = digest("wrong result")
    elif mutation == "whole-file":
        edits = ((initial, "entire replacement"),)
    else:
        monkeypatch.setattr(tool, "HANDWRITTEN", tool.HANDWRITTEN | {"second"})
    ports["second.py"] = before, edits, after
    monkeypatch.setattr(tool, "COUNTED_INTEGRATION_PORTS", ports)
    with pytest.raises(SystemExit, match="INTEGRATION FAILED"):
        tool.apply_counted_integration_ports(tool.Edit(tmp_path))
    assert (tmp_path / "first.py").read_text() == initial
    assert (tmp_path / "second.py").read_text() == initial


def test_integrated_replay_preserves_complete_tree_and_owned_assets(tmp_path, monkeypatch):
    import hashlib
    import importlib.util
    import shutil
    source = SRC.parents[3] / "src" / ("ha" + "os")
    if not source.is_dir():
        pytest.skip("build-only extraction source unavailable in standalone package")
    spec = importlib.util.spec_from_file_location("complete_integrated_replay", SRC.parent.parent / "tools" / "extract.py")
    tool = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tool)
    destination = tmp_path / "src" / "ledger_memory"
    shutil.copytree(SRC, destination, ignore=shutil.ignore_patterns("__pycache__"))
    def hashes(root):
        return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in root.rglob("*") if p.is_file() and "__pycache__" not in p.parts}
    expected = hashes(SRC)
    generated = {f"{name}.py" for name in tool.MODULES}
    protected = {name: digest for name, digest in expected.items() if name not in generated}
    assert generated <= set(expected)
    assert tool.PACKAGE_OWNED <= set(protected)
    assert "acceptance.py" in protected
    assert any(name.startswith("migrations/") for name in protected)
    monkeypatch.setattr(tool, "ROOT", tmp_path)
    monkeypatch.setattr(tool, "DEST", destination)
    (destination / "embeddings.py").write_text("synthetic stale generated sentinel\n")
    assert hashes(destination)["embeddings.py"] != expected["embeddings.py"]
    for _ in range(2):
        tool.regenerate(source, integrated=True)
        actual = hashes(destination)
        assert set(actual) == set(expected)
        assert actual == expected
        assert {name: actual[name] for name in protected} == protected
        assert not (destination / "adapters" / ("lm" + "studio.py")).exists()


def test_integrated_copy_still_refuses_unknown_inventory_before_writes(tmp_path, monkeypatch):
    import importlib.util
    spec = importlib.util.spec_from_file_location("integrated_unknown_guard", SRC.parent.parent / "tools" / "extract.py")
    tool = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tool)
    destination = tmp_path / "output"
    destination.mkdir()
    known = destination / "facts.py"
    known.write_bytes(b"preserve generated output")
    (destination / "unknown.py").write_bytes(b"unclassified output")
    monkeypatch.setattr(tool, "DEST", destination)
    with pytest.raises(SystemExit, match="unclassified output"):
        tool.copy_and_rewrite(tmp_path / "absent-source", tool.Edit(destination), integrated=True)
    assert known.read_bytes() == b"preserve generated output"
    assert not (destination / "adapters").exists()


@pytest.mark.parametrize("fault", ["none", "generation", "protected", "destination", "source"])
def test_staged_extraction_validates_without_shared_writes(tmp_path, monkeypatch, fault):
    import importlib.util
    import shutil
    source = SRC.parents[3] / "src" / ("ha" + "os")
    if not source.is_dir():
        pytest.skip("build-only extraction source unavailable in standalone package")
    spec = importlib.util.spec_from_file_location("staged_extraction_contract", SRC.parent.parent / "tools" / "extract.py")
    tool = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tool)
    copied_source = tmp_path / "source"
    copied_source.mkdir()
    for name in tool.MODULES:
        shutil.copy2(source / f"{name}.py", copied_source / f"{name}.py")
    source = copied_source
    destination = tmp_path / "output"
    shutil.copytree(SRC, destination, ignore=shutil.ignore_patterns("__pycache__"))
    expected = tool.tree_snapshot(destination)
    for name in ("chunkers.py", "embeddings.py"):
        (destination / name).write_text("synthetic old generated content\n")
    before = tool.tree_snapshot(destination)
    real_generate = tool.regenerate
    original_destination = tool.DEST
    def generate(src, **kwargs):
        if fault == "generation":
            raise SystemExit("synthetic generation failure")
        real_generate(src, **kwargs)
        if fault == "protected":
            (tool.DEST / "acceptance.py").write_text("synthetic protected mutation\n")
        elif fault == "destination":
            (destination / "facts.py").write_bytes(b"synthetic concurrent generated edit")
        elif fault == "source":
            (source / "facts.py").write_text("synthetic concurrent source edit\n")
    monkeypatch.setattr(tool, "regenerate", generate)
    attempts = []
    def publish(src, target):
        attempts.append(str(target))
        pytest.fail("validation attempted shared-file replacement")
    monkeypatch.setattr(tool.os, "replace", publish)
    if fault == "none":
        report = tool.staged_regenerate(source, destination)
        assert report["changed_modules"] == 2
        assert report["published_modules"] == 0
        assert tool.tree_snapshot(destination) == before
    else:
        with pytest.raises(SystemExit) as error:
            tool.staged_regenerate(source, destination)
        if fault == "destination":
            assert tool.tree_snapshot(destination) == before | {"facts.py": b"synthetic concurrent generated edit"}
        else:
            assert tool.tree_snapshot(destination) == before
        diagnostic = json.loads(str(error.value).partition(": ")[2])
        assert Path(diagnostic["stage_path"]).is_dir()
        assert diagnostic["stage_validation"] == ("incomplete" if fault == "generation" else "unvalidated")
        assert diagnostic["phase"] == ("generate" if fault == "generation" else "validate-stage")
    assert attempts == []
    assert tool.DEST == original_destination


@pytest.mark.parametrize("capability", ["publish", "alias", "overlap"])
def test_staged_extraction_refuses_unsafe_capabilities_before_any_read(tmp_path, monkeypatch, capability):
    import importlib.util
    spec = importlib.util.spec_from_file_location("staged_refusal_contract", SRC.parent.parent / "tools" / "extract.py")
    tool = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tool)
    monkeypatch.setattr(tool, "tree_snapshot", lambda root: pytest.fail("unsafe request reached inventory reads"))
    source = tmp_path / "source"
    destination = source if capability == "alias" else source / "nested" if capability == "overlap" else tmp_path / "output"
    with pytest.raises(SystemExit, match="in-place publication is unsafe|must not overlap or alias"):
        tool.staged_regenerate(source, destination, publish=capability == "publish")


def test_staged_extraction_refuses_concurrent_global_redirection(tmp_path, monkeypatch):
    import importlib.util
    spec = importlib.util.spec_from_file_location("generation_lock_contract", SRC.parent.parent / "tools" / "extract.py")
    tool = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tool)
    monkeypatch.setattr(tool, "tree_snapshot", lambda root: pytest.fail("busy generation reached inventory reads"))
    assert tool._GENERATION_LOCK.acquire(blocking=False)
    try:
        with pytest.raises(SystemExit, match="another generation is active"):
            tool.staged_regenerate(tmp_path / "source", tmp_path / "template")
    finally:
        tool._GENERATION_LOCK.release()
    assert not tool._GENERATION_LOCK.locked()


def _extraction_replacement_calls(source):
    return [node.lineno for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and (node.func.value.id, node.func.attr) in {
                ("os", "replace"), ("os", "rename"), ("os", "renames"), ("shutil", "move")}]


def test_extractor_cannot_reintroduce_shared_file_replacement():
    tool = SRC.parent.parent / "tools" / "extract.py"
    assert _extraction_replacement_calls(tool.read_text()) == []
    for expression in ("os.replace(a, b)", "os.rename(a, b)", "os.renames(a, b)", "shutil.move(a, b)"):
        assert _extraction_replacement_calls(expression) == [1]
    assert _extraction_replacement_calls('description = "os.replace(a, b)"') == []


def test_fresh_extraction_cli_reproduces_complete_tree_without_template_writes(tmp_path, monkeypatch, capsys):
    import importlib.util
    import json
    import shutil
    source = SRC.parents[3] / "src" / ("ha" + "os")
    if not source.is_dir():
        pytest.skip("build-only extraction source unavailable in standalone package")
    spec = importlib.util.spec_from_file_location("fresh_cli_contract", SRC.parent.parent / "tools" / "extract.py")
    tool = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tool)
    template = tmp_path / "template"
    shutil.copytree(SRC, template, ignore=shutil.ignore_patterns("__pycache__"))
    expected = tool.tree_snapshot(template)
    (template / "embeddings.py").write_text("synthetic stale generated file\n")
    original = tool.tree_snapshot(template)
    def run(output=None):
        argv = ["extract.py", "--source", str(source), "--template", str(template)]
        if output is not None:
            argv.extend(["--out", str(output)])
        monkeypatch.setattr(sys, "argv", argv)
        result = tool.main()
        captured = capsys.readouterr()
        return result, captured
    code, captured = run()
    assert code == 1
    assert json.loads(captured.out)["status"] == "drift"
    assert tool.tree_snapshot(template) == original
    reports = []
    for name in ("first", "second"):
        output = tmp_path / name
        code, captured = run(output)
        assert code == 0 and captured.err == ""
        report = json.loads(captured.out)
        assert report["status"] == "exported" and report["exported_files"] == len(expected)
        assert tool.tree_snapshot(output) == expected
        assert tool.tree_snapshot(template) == original
        reports.append(report)
    assert reports[0] == reports[1]
    code, captured = run(tmp_path / "first")
    assert code == 2 and "already exists" in captured.err
    assert tool.tree_snapshot(tmp_path / "first") == expected


def test_fresh_export_collision_preserves_other_writer_and_partial_output(tmp_path, monkeypatch):
    import importlib.util
    spec = importlib.util.spec_from_file_location("exclusive_export_contract", SRC.parent.parent / "tools" / "extract.py")
    tool = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tool)
    output = tmp_path / "fresh"
    original_open = tool.os.open
    def collide(path, flags, *args, **kwargs):
        if Path(path) == output / "facts.py":
            (output / "facts.py").write_bytes(b"another writer's bytes")
        return original_open(path, flags, *args, **kwargs)
    monkeypatch.setattr(tool.os, "open", collide)
    with pytest.raises(tool.FreshOutputError) as error:
        tool.export_fresh_tree(tmp_path, output, {"embeddings.py": b"first artifact", "facts.py": b"second artifact"})
    assert error.value.phase == "create-file" and error.value.target == "facts.py"
    assert error.value.error_class == "FileExistsError"
    assert (output / "facts.py").read_bytes() == b"another writer's bytes"
    assert (output / "embeddings.py").read_bytes() == b"first artifact"


def test_fresh_export_same_root_reservation_has_one_winner(tmp_path):
    import concurrent.futures
    import importlib.util
    import threading
    spec = importlib.util.spec_from_file_location("root_reservation_contract", SRC.parent.parent / "tools" / "extract.py")
    tool = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tool)
    output = tmp_path / "fresh"
    expected = {"facts.py": b"synthetic output"}
    gate = threading.Barrier(2)
    def export():
        gate.wait(timeout=5)
        try:
            return tool.export_fresh_tree(tmp_path, output, expected)
        except tool.FreshOutputError as exc:
            return exc.phase, exc.error_class
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: export(), range(2)))
    assert results.count(1) == 1
    assert results.count(("reserve-root", "FileExistsError")) == 1
    assert tool.tree_snapshot(output) == expected


@pytest.mark.parametrize("case", ["source", "template", "ancestor", "linked-parent", "existing", "missing-parent"])
def test_fresh_output_roles_refuse_before_inventory_reads(tmp_path, monkeypatch, case):
    import importlib.util
    spec = importlib.util.spec_from_file_location("fresh_path_contract", SRC.parent.parent / "tools" / "extract.py")
    tool = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tool)
    source, template = tmp_path / "source", tmp_path / "template"
    output = {"source": source / "nested", "template": template, "ancestor": tmp_path,
              "existing": tmp_path / "existing", "missing-parent": tmp_path / "missing" / "new"}.get(case)
    if case == "existing":
        output.mkdir()
        (output / "keep").write_bytes(b"existing bytes")
    elif case == "linked-parent":
        target = tmp_path / "target"
        target.mkdir()
        alias = tmp_path / "alias"
        try:
            alias.symlink_to(target, target_is_directory=True)
        except OSError:
            pytest.skip("native directory symlink creation unavailable")
        output = alias / "new"
    monkeypatch.setattr(tool, "tree_snapshot", lambda root: pytest.fail("invalid output reached template inventory"))
    with pytest.raises(SystemExit, match="overlap|linked path|already exists|parent must already exist"):
        tool.staged_regenerate(source, template, output=output)
    if case == "existing":
        assert (output / "keep").read_bytes() == b"existing bytes"
    assert list(tmp_path.glob(".ledger-extract-*")) == []


@pytest.mark.parametrize("fault", ["fdopen", "write", "close", "short"])
def test_fresh_export_cleanup_preserves_primary(tmp_path, monkeypatch, fault):
    import importlib.util
    spec = importlib.util.spec_from_file_location("fresh_cleanup_contract", SRC.parent.parent / "tools" / "extract.py")
    tool = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tool)
    real_close = tool.os.close
    def close_fd(fd):
        real_close(fd)
        raise OSError("private-cleanup")
    class Stream:
        def __init__(self, fd):
            self.fd = fd
        def write(self, data):
            if fault == "write":
                raise PermissionError("private-primary")
            return 0 if fault == "short" else len(data)
        def close(self):
            close_fd(self.fd)
    def fdopen(fd, mode):
        if fault == "fdopen":
            raise PermissionError("private-primary")
        return Stream(fd)
    monkeypatch.setattr(tool.os, "fdopen", fdopen)
    monkeypatch.setattr(tool.os, "close", close_fd)
    with pytest.raises(tool.FreshOutputError) as error:
        tool.export_fresh_tree(tmp_path, tmp_path / "fresh", {"facts.py": b"data"})
    assert error.value.phase == {"fdopen": "open-stream", "write": "write-file", "close": "close-file", "short": "write-file"}[fault]
    assert error.value.error_class == ("PermissionError" if fault in ("fdopen", "write") else "OSError")
    assert error.value.cleanup_error_class == (None if fault == "close" else "OSError")
    assert error.value.target == "facts.py"
    assert (tmp_path / "fresh" / "facts.py").exists()


def test_fresh_export_failure_retains_stage_and_partial_output_diagnostic(tmp_path, monkeypatch):
    import importlib.util
    import shutil
    source = SRC.parents[3] / "src" / ("ha" + "os")
    if not source.is_dir():
        pytest.skip("build-only extraction source unavailable in standalone package")
    spec = importlib.util.spec_from_file_location("fresh_retention_contract", SRC.parent.parent / "tools" / "extract.py")
    tool = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tool)
    template, output = tmp_path / "template", tmp_path / "fresh"
    shutil.copytree(SRC, template, ignore=shutil.ignore_patterns("__pycache__"))
    before = tool.tree_snapshot(template)
    original_open = tool.os.open
    def fail(path, flags, *args, **kwargs):
        if Path(path) == output / "about_me_recall.py":
            raise PermissionError("private-sentinel")
        return original_open(path, flags, *args, **kwargs)
    monkeypatch.setattr(tool.os, "open", fail)
    with pytest.raises(SystemExit) as error:
        tool.staged_regenerate(source, template, output=output)
    diagnostic = json.loads(str(error.value).partition(": ")[2])
    assert diagnostic["phase"] == "create-file"
    assert diagnostic["target"] == "about_me_recall.py"
    assert diagnostic["error_class"] == "PermissionError"
    assert "private-sentinel" not in str(error.value)
    stage = Path(diagnostic["stage_path"])
    assert tool.tree_snapshot(stage) == before
    assert (output / "__init__.py").read_bytes() == before["__init__.py"]
    assert not (output / "about_me_recall.py").exists()
    assert tool.tree_snapshot(template) == before


@pytest.mark.parametrize("mutation", ["cache-link", "unreadable-cache", "retired-adapter", "migration-fifo"])
def test_extractor_checks_excluded_subtrees_before_mutation(tmp_path, monkeypatch, mutation):
    import importlib.util
    spec = importlib.util.spec_from_file_location("extract_subtree_guard", SRC.parent.parent / "tools" / "extract.py")
    tool = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tool)
    destination = tmp_path / "output"
    destination.mkdir()
    # Full declared non-semantic inventory is observable, not just a one-file proxy.
    for name in set(tool.MODULES) | tool.HANDWRITTEN:
        (destination / f"{name}.py").write_text("synthetic generated input")
    adapters = destination / "adapters"
    adapters.mkdir()
    (adapters / "__init__.py").write_text("")
    cache = adapters / "__pycache__"
    cache.mkdir()
    (cache / "safe.pyc").write_bytes(b"synthetic regular cache")
    tool.preflight_output_tree(destination)
    if mutation == "cache-link":
        try:
            (cache / "alias").symlink_to(destination / "facts.py")
        except OSError:
            pytest.skip("native symlink creation unavailable")
        reason = "linked output"
    elif mutation == "unreadable-cache":
        original = tool.os.scandir
        def scandir(path):
            if str(path) == str(cache):
                raise PermissionError("synthetic-private-detail")
            return original(path)
        monkeypatch.setattr(tool.os, "scandir", scandir)
        reason = "inventory could not be read"
    elif mutation == "migration-fifo":
        if not hasattr(os, "mkfifo"):
            pytest.skip("native FIFO creation unavailable")
        migrations = destination / "migrations"
        migrations.mkdir()
        os.mkfifo(migrations / "synthetic.sql")
        reason = "unsafe migration entry"
    else:
        (adapters / ("lm" + "studio.py")).write_text("synthetic intentionally refused legacy input")
        reason = "retired adapter requires generator migration"
    calls = []
    def forbidden(*args, **kwargs):
        calls.append(True)
        raise AssertionError("preflight reached mutation")
    monkeypatch.setattr(tool, "DEST", destination)
    monkeypatch.setattr(tool.Path, "unlink", forbidden)
    monkeypatch.setattr(tool.shutil, "rmtree", forbidden)
    monkeypatch.setattr(tool.shutil, "copy", forbidden)
    with pytest.raises(SystemExit, match=reason):
        tool.copy_and_rewrite(tmp_path / "absent-source", tool.Edit(destination))
    assert calls == []
    assert (destination / "facts.py").read_text() == "synthetic generated input"


# ---- S6: write echoes go through the door

def test_write_echoes_are_door_reads(home):
    L = Ledger().ensure()

    def reads():
        con = _con(L)
        try:
            return con.execute("select count(*) from read_audit_entries").fetchone()[0]
        finally:
            con.close()
    n = reads()
    f = L.remember("The kiln is on the left", source="user_said")
    assert reads() == n + 1 and f["statement"] == "The kiln is on the left"
    n = reads(); L.dispute(f["id"], reason="maybe right"); assert reads() == n + 1
    n = reads(); L.revise(f["id"], "The kiln is on the right", reason="moved"); assert reads() == n + 1


def test_a_withheld_echo_carries_no_statement(home):
    L = Ledger().ensure()
    f = L.remember("The kiln is on the left", source="user_said")
    con = _con(L); con.execute("update client_devices set tier_ceiling = 2"); con.commit(); con.close()
    echoed = L.get_fact(f["id"])
    assert echoed["statement"] is None and echoed["withheld"] is True


# ---- S7: permissions

@pytest.mark.skipif(os.name == "nt", reason="POSIX modes")
def test_new_directories_are_private_and_existing_ones_are_not_loosened(home, tmp_path, monkeypatch):
    Ledger().ensure()
    for d in (home, home / "backups", home / "inbox"):
        assert (d.stat().st_mode & 0o777) == 0o700, d
    old = tmp_path / "old"; old.mkdir(); old.chmod(0o755)
    monkeypatch.setenv("LEDGER_HOME", str(old))
    Ledger().ensure()
    assert (old.stat().st_mode & 0o777) == 0o755                          # never tightened or loosened
    assert ((old / "backups").stat().st_mode & 0o777) == 0o700


# ---- NIT: shipped text

def test_no_owner_context_in_shipped_files():
    root = SRC.parent.parent
    needles = ("her" "mes", "trading" "zone", "ai-" "projects", "/us" "ers/")
    hits = []
    for f in root.rglob("*"):
        rel = f.relative_to(root)
        if not f.is_file() or set(rel.parts) & {"tools", ".venv", "__pycache__", ".git", "tests"} or rel.name == "BUILD-LOG.md":
            continue
        if f.suffix in {".pyc", ".db", ".zip"} or rel.name == "uv.lock":
            continue
        low = f.read_text(errors="replace").lower()
        hits += [f"{rel}: {n}" for n in needles if n in low]
    assert hits == []


# ---- NIT: nothing in the package can reach a subprocess, a shell or the keychain (AST, not grep)

def test_no_subprocess_shell_or_keychain_in_any_shipped_module():
    offenders = []
    for f in SRC.rglob("*.py"):
        offenders.extend(_process_boundary_offenders(f.read_text(), f.relative_to(SRC).as_posix()))
    assert offenders == []


def test_process_guard_refuses_forbidden_mutations():
    assert _process_boundary_offenders('import subprocess\nsubprocess.run(["command"])', "worker_spawn.py")
    assert _process_boundary_offenders('import subprocess\nsubprocess.Popen([], shell=True)', "worker_spawn.py")
    assert _process_boundary_offenders('import subprocess', "other.py")
    assert _process_boundary_offenders('command = "find-generic-password"', "worker_spawn.py")
    # Review fix: the exemption is the exact path, and the other ways to start a process are refused too.
    assert _process_boundary_offenders('import subprocess\nsubprocess.Popen([], shell=False)', "adapters/worker_spawn.py")
    assert _process_boundary_offenders('import subprocess as sp\nsp.run(["command"])', "worker_spawn.py")
    for call in ('os.execv("/bin/sh", [])', 'os.spawnl(0, "/bin/sh")', 'os.posix_spawn("/bin/sh", [], {})',
                 'os.fork()', 'asyncio.create_subprocess_exec("sh")', 'importlib.import_module("subprocess")'):
        assert _process_boundary_offenders(call, "api.py"), call
    assert _process_boundary_offenders('import multiprocessing', "api.py")
    assert _process_boundary_offenders('import ctypes', "api.py")
    assert not _process_boundary_offenders('import multiprocessing', "embedding_worker.py")
    assert not _process_boundary_offenders('import subprocess\nsubprocess.Popen([], shell=False)', "worker_spawn.py")


def test_detached_launcher_accepts_only_fixed_modules(tmp_path, monkeypatch):
    import importlib
    launcher = importlib.import_module("ledger_memory.worker_spawn")
    calls = []
    def popen(command, **kwargs):
        calls.append((command, kwargs))
        return type("Child", (), {"pid": 123})()
    monkeypatch.setattr(launcher.subprocess, "Popen", popen)
    for forbidden in ("os", "arbitrary.command", "security", "ledger_memory.embedding_worker --other"):
        with pytest.raises(ValueError):
            launcher._launch(forbidden)
    assert calls == []
    assert launcher.spawn_embedding_worker() == 123
    assert launcher.spawn_pending_worker(tmp_path / "records.db", tmp_path / "vectors.db") == 123
    for command, kwargs in calls:
        assert command[:3] == [sys.executable, "-I", "-m"]
        assert command[3] in {"ledger_memory.embedding_worker", "ledger_memory.semantic_pending"}
        assert kwargs["shell"] is False
        assert kwargs["stdin"] == kwargs["stdout"] == kwargs["stderr"] == launcher.subprocess.DEVNULL
        assert kwargs["close_fds"] is True
    assert calls[0][0][4:] == []
    assert calls[1][0][4:] == ["--database", str(tmp_path / "records.db"), "--sidecar", str(tmp_path / "vectors.db")]


# ---- known 0.3.0 follow-ups that are cheap: stale reader temp files

def test_stale_reader_temp_files_are_removed_at_start(home):
    L = Ledger().ensure()
    old = home / ".reader.token.123.abcd.tmp"; old.write_text("x")
    recent = home / ".reader.token.124.abcd.tmp"; recent.write_text("y")
    os.utime(old, (1, 1))
    Ledger().ensure()
    assert not old.exists() and recent.exists()


def _lock_deadline_child(path, started, result):
    from ledger_memory.locking import exclusive
    started.set()
    try:
        with exclusive(path, timeout=0.1):
            result.send("acquired")
    except Exception as exc:
        result.send(type(exc).__name__)
    finally:
        result.close()


def test_lock_timeout_and_thread_ownership(tmp_path):
    from ledger_memory.locking import exclusive
    path = tmp_path / "worker.lock"
    outcome = []
    finished = threading.Event()

    def contender():
        try:
            with exclusive(path, timeout=0.1):
                outcome.append("acquired")
        except Exception as exc:
            outcome.append(type(exc).__name__)
        finally:
            finished.set()

    with exclusive(path):
        with exclusive(path):
            assert path.is_file()
        thread = threading.Thread(target=contender)
        started_at = time.monotonic()
        thread.start()
        finished_while_held = finished.wait(1)
    thread.join(timeout=2)
    assert not thread.is_alive(), "lock contender failed to exit after release"
    assert finished_while_held, "thread lock acquisition ignored its deadline"
    assert outcome == ["TimeoutError"], outcome
    assert time.monotonic() - started_at < 3

    context = multiprocessing.get_context("spawn")
    started = context.Event()
    read_end, write_end = context.Pipe(duplex=False)
    process = context.Process(target=_lock_deadline_child, args=(path, started, write_end))
    try:
        with exclusive(path):
            process.start()
            write_end.close()
            assert started.wait(5), "child did not reach the measured lock operation"
            process.join(timeout=2)
            exited_while_held = not process.is_alive()
    finally:
        if process.pid is not None:
            if process.is_alive():
                process.terminate()
            process.join(timeout=3)
            if process.is_alive():
                process.kill()
                process.join(timeout=3)
    assert exited_while_held, "OS lock acquisition ignored its deadline"
    assert process.exitcode == 0, process.exitcode
    assert read_end.poll(1), "child lock probe returned no diagnostic"
    assert read_end.recv() == "TimeoutError"
    read_end.close()


def test_lock_zero_timeout_exception_cleanup_and_symlink(tmp_path):
    from ledger_memory.locking import exclusive
    path = tmp_path / "lock"
    with pytest.raises(RuntimeError, match="body failed"):
        with exclusive(path, timeout=0):
            raise RuntimeError("body failed")
    with exclusive(path, timeout=0):
        assert path.is_file()
    for invalid in (-1, float("inf"), float("nan"), True):
        with pytest.raises(ValueError):
            with exclusive(path, timeout=invalid):
                pytest.fail("invalid timeout was accepted")
    alias = tmp_path / "alias-lock"
    try:
        alias.symlink_to(path)
    except OSError:
        pytest.skip("OS does not permit a synthetic test symlink")
    with pytest.raises(OSError, match="symlink"):
        with exclusive(alias):
            pytest.fail("lock followed a symlink")
def test_lock_observation_does_not_create_and_tracks_current_owner(tmp_path):
    from ledger_memory.locking import exclusive, is_held
    path = tmp_path / "absent" / "lifecycle.lock"
    assert not is_held(path)
    assert not path.parent.exists()
    with exclusive(path):
        before = path.read_bytes()
        assert is_held(path)
        assert path.read_bytes() == before
    assert not is_held(path)
    assert path.read_bytes() == before


def test_lock_observation_sees_other_thread_and_refuses_symlink(tmp_path):
    import threading
    from ledger_memory.locking import exclusive, is_held
    path = tmp_path / "thread.lock"
    entered, release = threading.Event(), threading.Event()
    def owner():
        with exclusive(path):
            entered.set()
            assert release.wait(5)
    thread = threading.Thread(target=owner)
    thread.start()
    try:
        assert entered.wait(5)
        assert is_held(path)
    finally:
        release.set()
        thread.join(5)
    assert not thread.is_alive()
    assert not is_held(path)
    link = tmp_path / "alias.lock"
    try:
        link.symlink_to(path)
    except OSError:
        pytest.skip("OS does not permit a synthetic test symlink")
    with pytest.raises(OSError):
        is_held(link)


def _observation_lock_owner(path, entered, release):
    from ledger_memory.locking import exclusive
    with exclusive(path):
        entered.set()
        if not release.wait(5):
            raise RuntimeError("synthetic owner release timed out")


def test_lock_observation_sees_process_owner_and_release(tmp_path):
    from ledger_memory.locking import is_held
    path = tmp_path / "process.lock"
    context = multiprocessing.get_context("spawn")
    entered, release = context.Event(), context.Event()
    process = context.Process(target=_observation_lock_owner, args=(path, entered, release))
    process.start()
    try:
        assert entered.wait(5), "synthetic owner never acquired OS lock"
        assert is_held(path)
    finally:
        release.set()
        process.join(5)
        if process.is_alive():
            process.kill()
            process.join(2)
    assert not process.is_alive()
    assert process.exitcode == 0
    process.close()
    assert not is_held(path)
    assert path.read_bytes() == b""


def test_concurrent_idle_lock_observers_do_not_invent_owner(tmp_path, monkeypatch):
    import threading
    from ledger_memory import locking
    if locking.fcntl is None:
        pytest.skip("requires shared OS observation")
    path = tmp_path / "idle-observed.lock"
    path.write_bytes(b"preserve marker")
    entered, release = threading.Event(), threading.Event()
    real_flock = locking.fcntl.flock
    outcomes, errors = [], []
    def paused_flock(fd, operation):
        result = real_flock(fd, operation)
        if operation & locking.fcntl.LOCK_SH and threading.current_thread().name == "paused-observer":
            entered.set()
            if not release.wait(5):
                raise RuntimeError("synthetic observer release timed out")
        return result
    def observe():
        try:
            outcomes.append(locking.is_held(path))
        except BaseException as exc:
            errors.append(exc)
    monkeypatch.setattr(locking.fcntl, "flock", paused_flock)
    thread = threading.Thread(target=observe, name="paused-observer")
    thread.start()
    try:
        assert entered.wait(5)
        assert locking.is_held(path) is False
    finally:
        release.set()
        thread.join(5)
    assert not thread.is_alive()
    assert errors == []
    assert outcomes == [False]
    assert path.read_bytes() == b"preserve marker"


def test_unavailable_shared_lock_observation_is_not_reported_idle(tmp_path, monkeypatch):
    from ledger_memory import locking
    path = tmp_path / "empty.lock"
    path.touch()
    monkeypatch.setattr(locking, "fcntl", None)
    monkeypatch.setattr(locking, "msvcrt", None)
    with pytest.raises(RuntimeError, match="shared lifecycle-lock observation is unavailable"):
        locking.is_held(path)
    assert path.read_bytes() == b""


@pytest.mark.parametrize("locked,unlocked,error,expected", [
    (True, True, 0, False), (False, True, 33, True),
    (False, True, 5, "probe unavailable"), (True, False, 5, "release unavailable"),
])
def test_windows_shared_observation_uses_nonblocking_native_contract(tmp_path, monkeypatch, locked, unlocked, error, expected):
    import ctypes
    from types import SimpleNamespace
    from ledger_memory import locking
    path = tmp_path / "synthetic.lock"
    path.touch()
    calls = []
    descriptors = []
    state = []
    class Function:
        def __init__(self, name, result):
            self.name, self.result = name, result
        def __call__(self, *args):
            calls.append((self.name, args[:-1]))
            overlap = args[-1]._obj
            assert overlap.Offset == overlap.OffsetHigh == 0
            assert overlap.hEvent is None
            assert ctypes.sizeof(overlap) == (32 if ctypes.sizeof(ctypes.c_void_p) == 8 else 20)
            if self.name == "lock":
                state.append(overlap)
            else:
                assert overlap is state[0]
            return self.result
    kernel = SimpleNamespace(LockFileEx=Function("lock", locked), UnlockFileEx=Function("unlock", unlocked))
    def library(name, **kwargs):
        assert name == "kernel32" and kwargs == {"use_last_error": True}
        return kernel
    def handle(fd):
        descriptors.append(fd)
        return 12345
    monkeypatch.setattr(ctypes, "WinDLL", library, raising=False)
    monkeypatch.setattr(ctypes, "get_last_error", lambda: error, raising=False)
    monkeypatch.setattr(locking, "fcntl", None)
    monkeypatch.setattr(locking, "_WINDOWS", True)
    monkeypatch.setattr(locking, "msvcrt", SimpleNamespace(get_osfhandle=handle))
    if isinstance(expected, bool):
        assert locking.is_held(path) is expected
    else:
        with pytest.raises(OSError, match=expected):
            locking.is_held(path)
    assert calls[0] == ("lock", (12345, 1, 0, 1, 0))
    assert len(calls) == (2 if locked else 1)
    if locked:
        assert calls[1] == ("unlock", (12345, 0, 1, 0))
    assert kernel.LockFileEx.argtypes[0] is ctypes.c_void_p
    assert kernel.LockFileEx.restype is ctypes.c_int32
    assert path.read_bytes() == b""
    with pytest.raises(OSError):
        os.fstat(descriptors[0])


def test_windows_exclusive_empty_file_needs_no_initializing_write(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from ledger_memory import locking
    path = tmp_path / "empty-windows.lock"
    calls = []
    def lock(fd, mode, count):
        assert os.lseek(fd, 0, os.SEEK_CUR) == 0
        assert os.fstat(fd).st_size == 0
        calls.append((mode, count))
    def forbidden_write(*args):
        pytest.fail("byte-range locks support empty files without initialization")
    monkeypatch.setattr(locking, "fcntl", None)
    monkeypatch.setattr(locking, "msvcrt", SimpleNamespace(locking=lock, LK_NBLCK=1, LK_UNLCK=2))
    monkeypatch.setattr(locking.os, "write", forbidden_write)
    with locking.exclusive(path, timeout=0):
        assert calls == [(1, 1)]
        with locking.exclusive(path, timeout=0):
            assert calls == [(1, 1)]
    assert calls == [(1, 1), (2, 1)]
    assert path.read_bytes() == b""
    assert locking._ACTIVE_OWNERS == {}


def test_registry_failure_cannot_leave_reentrant_lock_bypass(tmp_path, monkeypatch):
    from ledger_memory import locking
    if locking.fcntl is None:
        pytest.skip("requires POSIX OS lock contention")
    path = tmp_path / "registry-failure.lock"
    real_fstat = locking.os.fstat
    calls = []
    def failed_registration(fd):
        calls.append(fd)
        if len(calls) == 2:
            raise OSError("synthetic registry identity failure")
        return real_fstat(fd)
    monkeypatch.setattr(locking.os, "fstat", failed_registration)
    with pytest.raises(OSError, match="synthetic registry identity failure"):
        with locking.exclusive(path):
            pytest.fail("failed registration entered protected body")
    assert len(calls) == 2
    monkeypatch.setattr(locking.os, "fstat", real_fstat)
    fd = os.open(path, os.O_RDWR)
    try:
        locking.fcntl.flock(fd, locking.fcntl.LOCK_EX | locking.fcntl.LOCK_NB)
        with pytest.raises(TimeoutError):
            with locking.exclusive(path, timeout=0):
                pytest.fail("stale reentrancy bypassed a real OS owner")
    finally:
        locking.fcntl.flock(fd, locking.fcntl.LOCK_UN)
        os.close(fd)
    with locking.exclusive(path, timeout=0):
        assert locking.is_held(path)
