"""Synthetic-only deployment tests. Never use the real HOME or a real uv."""
import json
import os
from pathlib import Path
import sqlite3
import stat
import sys
from types import SimpleNamespace
import zipfile

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import bemind_deploy as d


def archive(path, version="0.3.0rc3", extra=()):
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("ledger-memory/pyproject.toml", f'[project]\nname="ledger-memory"\nversion="{version}"\n')
        z.writestr("ledger-memory/uv.lock", "version = 1\n")
        z.writestr("ledger-memory/src/ledger_memory/locking.py", "# fixture only\n")
        for name, data in extra:
            z.writestr(name, data)
    path.with_suffix(".zip.sha256").write_text(d.digest(path.read_bytes()) + "  " + path.name + "\n")


@pytest.fixture
def box(tmp_path, monkeypatch):
    # resolve() avoids macOS's /var -> /private/var system alias.
    root = tmp_path.resolve() / "other machine with spaces \u00e9"
    root.mkdir()
    home = root / "home"
    home.mkdir()
    for key in list(os.environ):
        if key.startswith(("LEDGER_", "UV_", "XDG_")):
            monkeypatch.delenv(key)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(home / "data"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(home / "cache"))
    monkeypatch.setenv("LOCALAPPDATA", str(home / "local"))
    bundle = root / "bundle"
    bundle.mkdir()
    (bundle / "MEMORY-RULES.md").write_text("Store {{STORE}}\nCLI {{CLI}}\n")
    release = bundle / d.ARCHIVE_NAME
    archive(release)
    monkeypatch.setattr(d, "HERE", bundle)
    monkeypatch.setattr(d.shutil, "which", lambda name: str(root / "fake uv"))
    calls = []

    def fake_run(command, cwd, env, **kwargs):
        calls.append((command, cwd, dict(env)))
        assert Path(env["LEDGER_HOME"]).is_relative_to(root)
        assert Path(cwd).is_relative_to(root)
        if "sync" in command:
            executable = Path(env["UV_PROJECT_ENVIRONMENT"]) / ("Scripts/ledger.exe" if os.name == "nt" else "bin/ledger")
            executable.parent.mkdir(parents=True)
            executable.write_text("fixture executable")
        elif command[-1] == "init":
            store = Path(env["LEDGER_HOME"])
            store.mkdir(parents=True, exist_ok=True)
            with sqlite3.connect(store / "memory.db") as db:
                db.execute("CREATE TABLE facts (value TEXT)")
                db.execute("INSERT INTO facts VALUES ('precious memory')")
            (store / "memory.db").chmod(0o600)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(d, "run_process", fake_run)
    args = d.parser().parse_args(["install", "--memory-mode", "direct"])
    config = Path(args.config_dir)
    app = Path(args.app_root)
    store = Path(args.store)
    state = config / "ledger-memory-deployment"
    return SimpleNamespace(root=root, home=home, release=release, config=config, app=app,
                           store=store, state=state, args=args, calls=calls, fake_run=fake_run)


def receipt(box):
    return json.loads((box.state / "receipt.json").read_text())


def config(box):
    return d.parse_config((box.config / "bemind.jsonc").read_bytes())


def put_config(box, text, name="bemind.jsonc"):
    box.config.mkdir(parents=True, exist_ok=True)
    (box.config / name).write_text(text)


def uninstall(box, *flags):
    return d.deploy(d.parser().parse_args(["uninstall", *flags]))


def tree(root):
    return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()}


def test_fresh_install(box):
    assert "succeeded" in d.deploy(box.args)
    r = receipt(box)
    assert r["entry"] == config(box)["mcp"]["ledger"]
    assert r["entry"]["environment"] == {"LEDGER_HOME": str(box.store), "LEDGER_AGENT_NAME": "bemind"}
    assert r["entry"]["command"][-1] == "serve"
    assert Path(r["runtimes"][0]).parent == box.app / "versions"
    assert config(box)["permission"]["ledger_*"] == "allow"
    assert len(box.calls) == 3
    assert box.calls[1][0][-2:] == ["selftest", "--offline"]
    assert "--extra" in box.calls[0][0] and "semantic" in box.calls[0][0]
    assert "--python" in box.calls[0][0] and "3.13" in box.calls[0][0]
    assert "{{" not in (box.state / "MEMORY-RULES.md").read_text()


def test_unverified_process_cleanup_retains_runtime_and_blocks_retry(box, monkeypatch):
    def fail(*args, **kwargs):
        raise d.ProcessCleanupError(12345)
    monkeypatch.setattr(d, "run_process", fail)
    with pytest.raises(d.ProcessCleanupError):
        d.deploy(box.args)
    assert len(list((box.app / "versions").iterdir())) == 1
    assert (box.state / "pending.json").exists()
    assert not (box.config / "bemind.jsonc").exists()
    with pytest.raises(d.DeployError, match="Interrupted"):
        d.deploy(box.args)


def test_duplicate_managed_rules_refuse_teardown(box):
    d.deploy(box.args)
    current = config(box)
    current["instructions"].append(receipt(box)["instruction"])
    put_config(box, json.dumps(current))
    before = (box.config / "bemind.jsonc").read_bytes()
    with pytest.raises(d.DeployError, match="duplicated"):
        uninstall(box)
    assert (box.config / "bemind.jsonc").read_bytes() == before
    assert Path(receipt(box)["runtimes"][0]).is_dir()


@pytest.mark.parametrize("name", ['bad?name', 'bad*name', 'bad|name', 'bad<name', 'bad"name'])
def test_windows_unsafe_archive_characters(box, name):
    archive(box.release, extra=[("ledger-memory/" + name, "synthetic")])
    with pytest.raises(d.DeployError, match="Unsafe"):
        d.verified_archive(box.release)


def test_repeat_and_upgrade_preserve_store_and_owner(box):
    d.deploy(box.args)
    original = receipt(box)
    database = (box.store / "memory.db").read_bytes()
    d.deploy(box.args)
    archive(box.release, version="0.4.0")
    d.deploy(box.args)
    updated = receipt(box)
    assert updated["owner"] == original["owner"]
    assert len(set(updated["runtimes"])) == 3
    assert Path(updated["runtimes"][-1]).name.startswith("0.4.0-")
    assert (box.store / "memory.db").read_bytes() == database
    assert sum(c[0][-1] == "init" for c in box.calls) == 1
    assert len(config(box)["instructions"]) == 1


def test_jsonc_comments_and_unrelated_contents_preserved(box):
    original = '{\n// secret comment\n"theme":"dark",\n"instructions":["existing.md", /* retain here */],\n"mcp":{"other":{"enabled":false}},\n}\n'
    put_config(box, original)
    d.deploy(box.args)
    saved = (box.config / "bemind.jsonc").read_text()
    assert '// secret comment' in saved and '/* retain here */' in saved
    assert '"theme":"dark"' in saved
    assert config(box)["mcp"]["other"] == {"enabled": False}
    uninstall(box)
    assert config(box)["instructions"] == ["existing.md"]
    assert "/* retain here */" in (box.config / "bemind.jsonc").read_text()
    assert any(p.read_bytes() == original.encode() for p in (box.state / "backups").rglob("*") if p.is_file())


@pytest.mark.parametrize("layer", ["config.json", "bemind.json", "bemind.jsonc"])
def test_unmanaged_conflict_in_any_layer(box, layer):
    put_config(box, '{"mcp":{"ledger":{"command":["other"]}}}', layer)
    original = (box.config / layer).read_bytes()
    with pytest.raises(d.DeployError, match="Unmanaged"):
        d.deploy(box.args)
    assert (box.config / layer).read_bytes() == original
    assert not box.calls


@pytest.mark.parametrize("permission", ['"deny"', '{"*":"deny"}', '{"ledger_*":"ask"}'])
def test_explicit_permissions_never_overridden(box, permission):
    put_config(box, '{"permission":' + permission + '}')
    d.deploy(box.args)
    assert config(box)["permission"] == json.loads(permission)
    uninstall(box)
    assert config(box)["permission"] == json.loads(permission)


@pytest.mark.parametrize("sidecar", ["", "not a hash", "0" * 64])
def test_bad_checksum(box, sidecar):
    box.release.with_suffix(".zip.sha256").write_text(sidecar)
    with pytest.raises(d.DeployError, match="checksum"):
        d.deploy(box.args)
    assert not box.calls and not (box.config / "bemind.jsonc").exists()


@pytest.mark.parametrize("name", ["ledger-memory/../escape", "ledger-memory/C:/bad", "ledger-memory\\bad", "/ledger-memory/bad"])
def test_unsafe_archive_names(box, name):
    archive(box.release, extra=[(name, "bad")])
    with pytest.raises(d.DeployError, match="Unsafe"):
        d.deploy(box.args)
    assert not box.calls


def test_archive_symlink_alias_and_bounds(box, monkeypatch):
    link = zipfile.ZipInfo("ledger-memory/link")
    link.create_system = 3
    link.external_attr = (stat.S_IFLNK | 0o777) << 16
    archive(box.release, extra=[(link, "/outside")])
    with pytest.raises(d.DeployError, match="Unsafe"):
        d.verified_archive(box.release)
    archive(box.release, extra=[("ledger-memory/Foo/a", "a"), ("ledger-memory/foo/b", "b")])
    with pytest.raises(d.DeployError, match="alias"):
        d.verified_archive(box.release)
    archive(box.release)
    monkeypatch.setattr(d, "MAX_BYTES", 1)
    with pytest.raises(d.DeployError, match="limits"):
        d.verified_archive(box.release)


@pytest.mark.parametrize("phase", ["sync", "selftest"])
def test_dependency_and_selftest_failure_do_not_activate(box, monkeypatch, phase):
    put_config(box, '{"theme":"unchanged"}')
    before = (box.config / "bemind.jsonc").read_bytes()
    def fail(command, **kwargs):
        if phase in command:
            return SimpleNamespace(returncode=19)
        return box.fake_run(command, **kwargs)
    monkeypatch.setattr(d, "run_process", fail)
    with pytest.raises(d.DeployError, match="failed"):
        d.deploy(box.args)
    assert (box.config / "bemind.jsonc").read_bytes() == before
    assert not (box.state / "receipt.json").exists()
    assert list((box.app / "versions").iterdir()) == []


def test_concurrent_other_layer_edit_not_overwritten(box, monkeypatch):
    def edit(command, **kwargs):
        result = box.fake_run(command, **kwargs)
        if "selftest" in command:
            put_config(box, '{"user":"later edit"}', "config.json")
        return result
    monkeypatch.setattr(d, "run_process", edit)
    with pytest.raises(d.DeployError, match="Concurrent"):
        d.deploy(box.args)
    assert not (box.config / "bemind.jsonc").exists()
    assert json.loads((box.config / "config.json").read_text())["user"] == "later edit"


def test_atomic_cas_and_guarded_rollback(box, monkeypatch):
    put_config(box, '{"original":true}')
    real_atomic = d.atomic
    def edit(path, data, expected):
        if path == box.config / "bemind.jsonc":
            path.write_text('{"concurrent":true}')
        return real_atomic(path, data, expected)
    monkeypatch.setattr(d, "atomic", edit)
    with pytest.raises(d.DeployError, match="Concurrent"):
        d.deploy(box.args)
    assert config(box) == {"concurrent": True}
    assert not (box.state / "MEMORY-RULES.md").exists()
    assert (box.state / "pending.json").exists()


def test_interrupted_activation_refuses_retry_with_backups(box, monkeypatch):
    put_config(box, '{"original":true}')
    real_atomic = d.atomic
    def interrupt(path, data, expected):
        if path == box.config / "bemind.jsonc":
            raise KeyboardInterrupt()
        return real_atomic(path, data, expected)
    monkeypatch.setattr(d, "atomic", interrupt)
    with pytest.raises(KeyboardInterrupt):
        d.deploy(box.args)
    assert (box.state / "pending.json").exists()
    assert any(p.read_bytes() == b'{"original":true}' for p in (box.state / "backups").rglob("*") if p.is_file())
    with pytest.raises(d.DeployError, match="Interrupted"):
        d.deploy(box.args)


def test_uninstall_preserves_data_models_backups_and_is_idempotent(box):
    d.deploy(box.args)
    d.deploy(box.args)
    history = box.store / "imports" / "history.txt"
    history.parent.mkdir()
    history.write_text("irreplaceable history")
    cache = d.worker_root()
    cache.mkdir(parents=True)
    model = cache / "model.onnx"
    model.write_text("shared model")
    before = tree(box.store)
    backups = tree(box.state / "backups")
    runtimes = receipt(box)["runtimes"]
    assert "uninstalled" in uninstall(box)
    for name, data in before.items():
        assert (box.store / name).read_bytes() == data
    for name, data in backups.items():
        assert (box.state / "backups" / name).read_bytes() == data
    assert model.read_text() == "shared model"
    assert all(not Path(p).exists() for p in runtimes)
    assert receipt(box)["status"] == "uninstalled"
    assert "uninstalled" in uninstall(box)


def test_edited_entry_stops_upgrade_and_uninstall(box):
    d.deploy(box.args)
    path = box.config / "bemind.jsonc"
    path.write_text(d.jsonc_edit.set_value(path.read_text(), ["mcp", "ledger", "enabled"], False))
    before = path.read_bytes()
    for action in (lambda: d.deploy(box.args), lambda: uninstall(box)):
        with pytest.raises(d.DeployError, match="edited"):
            action()
    assert path.read_bytes() == before
    assert all(Path(p).exists() for p in receipt(box)["runtimes"])


def test_manually_removed_config_still_cleans_owned_runtime(box):
    d.deploy(box.args)
    (box.config / "bemind.jsonc").unlink()
    assert "uninstalled" in uninstall(box)
    assert not (box.config / "bemind.jsonc").exists()


def test_keep_runtime_deregisters_even_if_session_busy(box):
    d.deploy(box.args)
    with d.file_lock(box.store / ".ledger.sessions.lock"):
        assert "retained" in uninstall(box, "--keep-runtime")
    assert all(Path(p).exists() for p in receipt(box)["runtimes"])
    assert "ledger" not in config(box)["mcp"]
    assert "uninstalled" in uninstall(box)


@pytest.mark.skipif(os.name == "nt", reason="POSIX shared lock test; not a Windows claim")
def test_shared_session_blocks_uninstall_before_config_change(box):
    import fcntl
    d.deploy(box.args)
    before = (box.config / "bemind.jsonc").read_bytes()
    with (box.store / ".ledger.sessions.lock").open("wb") as lock:
        fcntl.flock(lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
        with pytest.raises(d.DeployError, match="busy"):
            uninstall(box)
    assert (box.config / "bemind.jsonc").read_bytes() == before


def test_all_worker_and_startup_locks_block_cleanup(box):
    d.deploy(box.args)
    cache = d.worker_root()
    cache.mkdir(parents=True)
    before = (box.config / "bemind.jsonc").read_bytes()
    for path in [box.store / ".ledger.lock", *(cache / n for n in ("launch.lock", "service.lock", "model.lock"))]:
        with d.file_lock(path):
            with pytest.raises(d.DeployError, match="busy"):
                uninstall(box)
        assert (box.config / "bemind.jsonc").read_bytes() == before


def test_environment_isolation_and_tls_preserved(box, monkeypatch):
    for key in ("UV_PROJECT", "UV_PROJECT_ENVIRONMENT", "UV_PYTHON", "VIRTUAL_ENV", "PYTHONPATH", "PYTHONHOME", "LEDGER_EMBEDDING_BACKEND"):
        monkeypatch.setenv(key, "/do/not/touch")
    monkeypatch.setenv("HTTPS_PROXY", "https://proxy.invalid")
    monkeypatch.setenv("SSL_CERT_FILE", "/certs.pem")
    ca = box.root / "company ca.pem"
    ca.write_text("fixture")
    box.args.ca_bundle = str(ca)
    d.deploy(box.args)
    env = box.calls[0][2]
    assert "UV_PROJECT" not in env and "VIRTUAL_ENV" not in env and "PYTHONPATH" not in env
    assert "LEDGER_EMBEDDING_BACKEND" not in env
    assert env["SSL_CERT_FILE"] == str(ca) and env["HTTPS_PROXY"] == "https://proxy.invalid"
    assert env["LEDGER_CA_BUNDLE"] == str(ca)
    assert env["UV_PROJECT_ENVIRONMENT"].startswith(str(box.app))
    assert config(box)["mcp"]["ledger"]["environment"]["LEDGER_CA_BUNDLE"] == str(ca)


def test_dry_runs_are_nonmutating(box):
    before = tree(box.root)
    box.args.dry_run = True
    assert "preflight" in d.deploy(box.args)
    assert tree(box.root) == before and not box.calls
    assert not box.config.exists() and not box.app.exists() and not box.store.exists()
    box.args.dry_run = False
    d.deploy(box.args)
    before = tree(box.root)
    assert "preflight" in uninstall(box, "--dry-run")
    assert tree(box.root) == before


def test_paths_and_marker_guard(box):
    box.args.app_root = str(box.store / "runtime")
    with pytest.raises(d.DeployError, match="overlap"):
        d.deploy(box.args)
    box.args.app_root = str(box.app)
    d.deploy(box.args)
    runtime = Path(receipt(box)["runtimes"][0])
    (runtime / d.MARKER).write_text("not owned")
    before = (box.config / "bemind.jsonc").read_bytes()
    with pytest.raises(d.DeployError, match="marker"):
        uninstall(box)
    assert (box.config / "bemind.jsonc").read_bytes() == before


def test_symlink_destination_refused(box):
    target = box.root / "outside"
    target.mkdir()
    box.app.parent.mkdir(parents=True)
    box.app.symlink_to(target, target_is_directory=True)
    with pytest.raises(d.DeployError, match="symlinks"):
        d.deploy(box.args)
    assert list(target.iterdir()) == []


def test_runtime_partial_cleanup_retries(box, monkeypatch):
    d.deploy(box.args)
    real_rmtree = d.shutil.rmtree
    monkeypatch.setattr(d.shutil, "rmtree", lambda *a, **k: (_ for _ in ()).throw(OSError("failure")))
    assert "cleanup failed" in uninstall(box)
    assert receipt(box)["status"] == "uninstalled"
    assert (Path(receipt(box)["runtimes"][0]) / d.MARKER).exists()
    monkeypatch.setattr(d.shutil, "rmtree", real_rmtree)
    assert "uninstalled" in uninstall(box)


def test_semantic_failure_reports_base_install_exists(box, monkeypatch):
    model = box.root / "local model"
    model.mkdir()
    (model / "model.onnx").write_text("preserve source")
    def fail(command, **kwargs):
        if "import" in command:
            assert receipt(box)["status"] == "installed"
        if "enable" in command:
            return SimpleNamespace(returncode=8)
        return box.fake_run(command, **kwargs)
    monkeypatch.setattr(d, "run_process", fail)
    box.args.semantic = True
    box.args.model_dir = str(model)
    result = d.deploy(box.args)
    assert "Base installation succeeded" in result and "Semantic setup failed" in result
    assert receipt(box)["status"] == "installed"
    assert any(c[0][-3:] == ["semantic", "import", str(model)] for c in box.calls)
    uninstall(box)
    assert (model / "model.onnx").read_text() == "preserve source"


def test_existing_sqlite_backup_is_private_and_source_not_migrated(box):
    box.store.mkdir()
    with sqlite3.connect(box.store / "memory.db") as db:
        db.execute("CREATE TABLE old_schema (value TEXT)")
        db.execute("INSERT INTO old_schema VALUES ('preserve me')")
    original = (box.store / "memory.db").read_bytes()
    d.deploy(box.args)
    assert (box.store / "memory.db").read_bytes() == original
    assert all(call[0][-1] != "init" for call in box.calls)
    backup = next((box.state / "backups").glob("memory-*.db"))
    with sqlite3.connect(backup) as db:
        assert db.execute("SELECT value FROM old_schema").fetchone() == ("preserve me",)
    if os.name != "nt":
        assert backup.stat().st_mode & 0o077 == 0


def test_missing_uv_and_safe_error_output(box, monkeypatch, capsys):
    monkeypatch.setattr(d.shutil, "which", lambda _: None)
    assert d.main(["install"]) == 1
    assert "Install uv separately" in capsys.readouterr().err
    put_config(box, '{"credential":"TOP-SECRET", "duplicate":1,"duplicate":2}')
    assert d.main(["install"]) == 1
    assert "TOP-SECRET" not in capsys.readouterr().err


def test_json_config_selected_and_user_permission_change_preserved(box):
    put_config(box, '{"instructions":["owned-by-user"]}', "bemind.json")
    d.deploy(box.args)
    path = box.config / "bemind.json"
    assert receipt(box)["config"] == str(path)
    path.write_text(d.jsonc_edit.set_value(path.read_text(), ["permission", "ledger_*"], "deny"))
    uninstall(box)
    assert d.parse_config(path.read_bytes())["permission"]["ledger_*"] == "deny"
    assert not (box.config / "bemind.jsonc").exists()


def test_malformed_inputs_rejected_without_activation(box):
    box.release.with_suffix(".zip.sha256").unlink()
    with pytest.raises(d.DeployError):
        d.deploy(box.args)
    box.release.write_bytes(b"not a ZIP")
    box.release.with_suffix(".zip.sha256").write_text(d.digest(box.release.read_bytes()))
    with pytest.raises(d.DeployError, match="malformed"):
        d.deploy(box.args)
    with pytest.warns(UserWarning, match="Duplicate"):
        archive(box.release, extra=[("ledger-memory/uv.lock", "duplicate")])
    with pytest.raises(d.DeployError, match="Duplicate"):
        d.deploy(box.args)
    archive(box.release)
    put_config(box, "")
    with pytest.raises(d.DeployError, match="Invalid"):
        d.deploy(box.args)
    assert not box.calls


def test_unmanaged_noop_exclusive_installer_lock_and_confined_receipt(box):
    before = tree(box.root)
    assert "No deployment receipt" in uninstall(box)
    assert tree(box.root) == before and not box.config.exists()
    box.state.mkdir(parents=True)
    with d.file_lock(box.state / "installer.lock"):
        with pytest.raises(d.DeployError, match="busy"):
            d.deploy(box.args)
    d.deploy(box.args)
    r = receipt(box)
    r["runtimes"] = [str(box.store)]
    (box.state / "receipt.json").write_text(json.dumps(r))
    before = (box.config / "bemind.jsonc").read_bytes()
    with pytest.raises(d.DeployError, match="outside"):
        uninstall(box)
    assert (box.config / "bemind.jsonc").read_bytes() == before


def test_supplied_rc3_archive_passes_read_only_validation(box):
    release = Path(__file__).resolve().parents[2] / "builds" / d.ARCHIVE_NAME
    if not release.exists():
        pytest.skip("Optional supplied release not present in distributable test folder")
    before = tree(box.root)
    data, sha, version = d.verified_archive(release)
    assert version == "0.3.0rc3" and d.digest(data) == sha
    assert not box.calls and tree(box.root) == before


@pytest.fixture
def windows_platform(monkeypatch):
    # Replace only the installer's os view; pathlib/pytest retain the real OS.
    monkeypatch.setattr(d, "os", SimpleNamespace(**{**vars(os), "name": "nt"}))


@pytest.mark.parametrize("mode", [None, "luna"])
@pytest.mark.parametrize("dry_run", [False, True])
def test_windows_luna_refuses_before_state_or_dependencies(box, windows_platform, monkeypatch, mode, dry_run):
    box.args.memory_mode, box.args.dry_run = mode, dry_run
    before = tree(box.root)
    monkeypatch.setattr(d, "verified_archive", lambda *a: pytest.fail("Platform refusal must precede archive work"))
    monkeypatch.setattr(d.shutil, "which", lambda *a: pytest.fail("Platform refusal must precede dependency lookup"))
    monkeypatch.setattr(d, "make_private", lambda *a: pytest.fail("Platform refusal must precede state protection"))
    with pytest.raises(d.DeployError, match=r"Windows.*--memory-mode direct"):
        d.deploy(box.args)
    assert tree(box.root) == before and not box.calls
    assert not box.state.exists() and not box.config.exists()
    assert not box.app.exists() and not box.store.exists()


def test_windows_direct_preflight_remains_available(box, windows_platform):
    box.args.dry_run = True
    before = tree(box.root)
    assert "preflight passed" in d.deploy(box.args)
    assert tree(box.root) == before and not box.calls


@pytest.mark.parametrize("saved_mode", [None, "direct", "luna"])
def test_windows_platform_check_preserves_receipt_mode(box, windows_platform, saved_mode):
    box.args.memory_mode = None
    previous = {"schema": 1} if saved_mode is None else {"schema": 2, "memory_mode": saved_mode}
    if saved_mode == "luna":
        with pytest.raises(d.DeployError, match="--memory-mode direct"):
            d.deployment_mode(box.args, previous)
    else:
        assert d.deployment_mode(box.args, previous) == "direct"
    assert not box.calls and not box.state.exists()


@pytest.mark.skipif(os.name != "posix", reason="POSIX mode-bit requirements")
@pytest.mark.parametrize("existing_database", [False, True])
def test_existing_store_permissions_are_never_automatically_hardened(box, monkeypatch, existing_database):
    box.store.mkdir(mode=0o755)
    box.store.chmod(0o755)
    checked = [box.store]
    if existing_database:
        database = box.store / "memory.db"
        with sqlite3.connect(database) as connection:
            connection.execute("CREATE TABLE synthetic (value TEXT)")
        lock = box.store / ".ledger.sessions.lock"
        lock.write_bytes(b"")
        for path in (database, lock):
            path.chmod(0o644)
            checked.append(path)
    before = {path: stat.S_IMODE(path.stat().st_mode) for path in checked}
    protect = d.make_private
    def protect_owned_only(path):
        assert path not in checked, "Existing memory permissions require operator approval"
        protect(path)
    monkeypatch.setattr(d, "make_private", protect_owned_only)
    d.deploy(box.args)
    assert {path: stat.S_IMODE(path.stat().st_mode) for path in checked} == before


@pytest.mark.skipif(os.name != "posix", reason="POSIX umask requirements")
@pytest.mark.parametrize("outcome,code", [("success", 0), ("refusal", 1), ("interrupt", 130), ("help", 0)])
def test_cli_private_umask_is_restored_on_every_exit(box, monkeypatch, outcome, code):
    def deploy_stub(args):
        assert os.umask(0o077) == 0o077
        if outcome == "refusal":
            raise d.DeployError("synthetic refusal")
        if outcome == "interrupt":
            raise KeyboardInterrupt()
        return "Installation succeeded"
    monkeypatch.setattr(d, "deploy", deploy_stub)
    original = os.umask(0o027)
    try:
        if outcome == "help":
            with pytest.raises(SystemExit) as result:
                d.main(["install", "--help"])
            assert result.value.code == code
        else:
            assert d.main(["install", "--memory-mode", "direct"]) == code
        assert os.umask(0o027) == 0o027
    finally:
        os.umask(original)
