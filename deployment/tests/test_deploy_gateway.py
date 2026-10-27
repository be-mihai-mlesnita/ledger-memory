"""Gateway installer tests: fake executables, synthetic HOME, no models or live data."""
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from test_deploy import box, config, d, put_config, receipt, tree, uninstall


@pytest.fixture
def luna(box, monkeypatch):
    box.args.memory_mode = None
    executable = box.root / "fake bemind"
    executable.write_text("fixture")
    executable.chmod(0o700)
    box.args.bemind_command = str(executable)
    source = d.HERE / "gateway"
    source.mkdir()
    for name in ("memory_gateway.py", "ledger_backend.py", "gateway_backend_contract.py", "worker_guard.mjs"):
        (source / name).write_text("# synthetic only\n")
    (source / "CONTRACT.md").write_text("do not install")
    (source / "test_private.py").write_text("do not install")
    (source / "__pycache__").mkdir()
    (source / "__pycache__/secret.py").write_text("do not install")
    box.source = source
    original = box.fake_run
    def fake(command, cwd, env, **kwargs):
        result = original(command, cwd, env, **kwargs)
        if "sync" in command:
            d.runtime_python(Path(cwd)).write_text("fixture python")
        if "--settings" in command:
            path = Path(command[-1])
            settings = json.loads(path.read_text())
            state = Path(settings["state_dir"])
            if "enable" in command:
                state.mkdir(parents=True, exist_ok=True)
                (state / "receipts").mkdir(exist_ok=True)
            if "enable" in command or "stop" in command:
                settings["enabled"] = "enable" in command
                path.write_text(json.dumps(settings))
        return result
    monkeypatch.setattr(d, "run_process", fake)
    box.fake_run = fake
    return box


def legacy(box, *, environment=True):
    entry = {"type": "local", "command": [str(box.home / "old runtime/bin/ledger"), "serve"], "enabled": True}
    if environment:
        entry["environment"] = {"LEDGER_HOME": str(box.store), "LEDGER_AGENT_NAME": "old-agent"}
    return entry


def migrate(box):
    old_rules = box.home / "old-rules.md"
    old_rules.write_text("old instructions unchanged forever\n")
    agents = box.home / "AGENTS.md"
    agents.write_bytes(b"# Global\r\n\r\n## Prechecks\r\nkeep first\r\n\r\n## Shared long-term memory (all projects)\r\nold ledger and CLI rules\r\n\r\n## Sending\r\nkeep last\r\n")
    text = ('{\n// keep this exact comment\n"theme":"dark",\n"instructions":["first", /* position */ '
            + json.dumps(str(old_rules)) + ', "last"],\n"mcp":{"other":{"enabled":false},"ledger":'
            + json.dumps(legacy(box)) + '},\n"permission":{"*":"allow","ledger_*":"allow","bash":"ask"},\n'
            '"agent":{"plan":{"model":"unchanged","permission":{"bash":"deny"}},"writer":{"description":"unchanged"}}\n}\n')
    put_config(box, text)
    box.args.migrate_existing_ledger = True
    box.args.replace_instruction = [str(old_rules)]
    box.args.agents_file = str(agents)
    box.args.agents_section = "## Shared long-term memory (all projects)"
    return text.encode(), agents, agents.read_bytes(), old_rules


def test_default_luna_and_scoped_package(luna):
    assert "succeeded" in d.deploy(luna.args)
    r = receipt(luna)
    assert r["schema"] == 2 and r["memory_mode"] == "luna"
    assert config(luna)["mcp"] == {"memory": r["entry"]}
    assert r["entry"]["command"] == [str(d.runtime_python(Path(r["runtimes"][-1]))),
        str(Path(r["runtimes"][-1]) / "gateway/memory_gateway.py"), "serve", "--settings", r["gateway"]["settings"]]
    assert config(luna)["permission"] == {"ledger_*": "deny", "memory_read": "allow", "memory_write": "ask"}
    assert config(luna)["agent"]["plan"]["permission"]["memory_write"] == "deny"
    installed = Path(r["runtimes"][-1]) / "gateway"
    assert sorted(p.name for p in installed.iterdir()) == ["gateway_backend_contract.py", "ledger_backend.py", "memory_gateway.py", "worker_guard.mjs"]
    settings = json.loads(Path(r["gateway"]["settings"]).read_text())
    assert settings["bemind_command"] == [luna.args.bemind_command]
    assert settings["bemind_data_home"] == os.environ["XDG_DATA_HOME"]
    assert settings["owner"] == r["owner"] and settings["model"] == d.LUNA_MODEL
    assert Path(settings["state_dir"]).parent == luna.state / "gateway"
    assert Path(settings["state_dir"]).name.startswith("state-")
    assert "preflight" in luna.calls[-2][0] and "enable" in luna.calls[-1][0]
    if os.name != "nt":
        assert Path(r["gateway"]["settings"]).stat().st_mode & 0o077 == 0


def test_memory_write_allow_persists_across_upgrade_and_respects_denials(luna):
    luna.args.memory_write = "allow"
    assert "write to allow" in d.deploy(luna.args)
    assert config(luna)["permission"]["memory_write"] == "allow"
    assert config(luna)["agent"]["plan"]["permission"]["memory_write"] == "deny"
    assert receipt(luna)["memory_write_policy"] == "allow"
    luna.args.memory_write = None          # an upgrade without the flag keeps the choice
    d.deploy(luna.args)
    assert config(luna)["permission"]["memory_write"] == "allow"
    luna.args.memory_write = "ask"         # and it can be switched back explicitly
    d.deploy(luna.args)
    assert config(luna)["permission"]["memory_write"] == "ask"
    assert receipt(luna)["memory_write_policy"] == "ask"


def test_memory_write_allow_never_overrides_existing_user_denial(luna):
    put_config(luna, '{"permission":{"memory_*":"deny"}}', "config.json")
    luna.args.memory_write = "allow"
    d.deploy(luna.args)
    assert config(luna)["permission"]["memory_write"] == "deny"


def test_migration_exact_round_trip_and_no_old_runtime_ownership(luna):
    original, agents, agents_before, old_rules = migrate(luna)
    d.deploy(luna.args)
    assert old_rules.read_text() == "old instructions unchanged forever\n"
    assert config(luna)["instructions"] == ["first", str(luna.state / "MEMORY-RULES.md"), "last"]
    assert b"## Prechecks\r\nkeep first\r\n" in agents.read_bytes()
    assert b"## Sending\r\nkeep last\r\n" in agents.read_bytes()
    r = receipt(luna)
    assert all(Path(p).parent == luna.app / "versions" for p in r["runtimes"])
    assert "uninstalled" in uninstall(luna)
    assert (luna.config / "bemind.jsonc").read_bytes() == original
    assert agents.read_bytes() == agents_before
    assert old_rules.read_text() == "old instructions unchanged forever\n"


def test_migration_requires_flag_and_same_store(luna):
    put_config(luna, json.dumps({"mcp": {"ledger": legacy(luna)}}))
    before = (luna.config / "bemind.jsonc").read_bytes()
    with pytest.raises(d.DeployError, match="Unmanaged"):
        d.deploy(luna.args)
    assert not luna.calls and (luna.config / "bemind.jsonc").read_bytes() == before
    luna.args.migrate_existing_ledger = True
    bad = legacy(luna)
    bad["environment"]["LEDGER_HOME"] = str(luna.home / "other store")
    put_config(luna, json.dumps({"mcp": {"ledger": bad}}))
    with pytest.raises(d.DeployError, match="different"):
        d.deploy(luna.args)
    assert not luna.calls


def test_default_ledger_home_migration(luna):
    put_config(luna, json.dumps({"mcp": {"ledger": legacy(luna, environment=False)}}))
    luna.args.migrate_existing_ledger = True
    d.deploy(luna.args)
    uninstall(luna)
    assert "environment" not in config(luna)["mcp"]["ledger"]


@pytest.mark.parametrize("name", ["ledger-mcp", "ledger-mcp.exe"])
def test_single_executable_migration_restores_exact_entry_and_old_runtime(luna, name):
    _, agents, agents_before, old_rules = migrate(luna)
    executable = luna.home / ".local/share/ledger-memory/.venv/bin" / name
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"synthetic manual runtime: never adopted\n")
    entry = {"type": "local", "command": [str(executable)], "enabled": True,
             "environment": {"LEDGER_HOME": str(luna.store), "LEDGER_AGENT_NAME": "bemind"},
             "timeout": 180000}
    path = luna.config / "bemind.jsonc"
    original = d.jsonc_edit.set_value(path.read_text(), ["mcp", "ledger"], entry).encode()
    path.write_bytes(original)
    luna.args.migrate_existing_ledger = False
    with pytest.raises(d.DeployError, match="Unmanaged"):
        d.deploy(luna.args)
    assert not luna.calls and path.read_bytes() == original
    luna.args.migrate_existing_ledger = True
    d.deploy(luna.args)
    assert "ledger" not in config(luna)["mcp"]
    assert all(not executable.is_relative_to(Path(p)) for p in receipt(luna)["runtimes"])
    uninstall(luna)
    assert path.read_bytes() == original and config(luna)["mcp"]["ledger"] == entry
    assert agents.read_bytes() == agents_before
    assert old_rules.read_text() == "old instructions unchanged forever\n"
    assert executable.read_bytes() == b"synthetic manual runtime: never adopted\n"


@pytest.mark.parametrize("name", ["ledger-mcp", "ledger-mcp.exe"])
def test_single_executable_migration_keeps_store_verification(luna, name):
    entry = {**legacy(luna), "command": [str(luna.home / "bin" / name)]}
    entry["environment"]["LEDGER_HOME"] = str(luna.home / "other store")
    put_config(luna, json.dumps({"mcp": {"ledger": entry}}))
    luna.args.migrate_existing_ledger = True
    with pytest.raises(d.DeployError, match="different"):
        d.deploy(luna.args)
    assert not luna.calls
    del entry["environment"]["LEDGER_HOME"]
    d.validate_legacy_store(entry, luna.store)


@pytest.mark.parametrize("command", [
    ["ledger-mcp"], ["ledger-mcp.exe"], ["relative/ledger-mcp"],
    ["/synthetic/ledger-mcp", "serve"], ["/synthetic/ledger-mcp.exe", "--store", "/other"],
    ["/synthetic/ledger"], ["/synthetic/ledger", "serve", "--other"],
    ["/synthetic/sh", "-c", "ledger-mcp"], ["/synthetic/arbitrary-server"],
])
def test_single_executable_migration_does_not_adopt_other_commands(luna, command):
    entry = {**legacy(luna), "command": command}
    put_config(luna, json.dumps({"mcp": {"ledger": entry}}))
    before = (luna.config / "bemind.jsonc").read_bytes()
    luna.args.migrate_existing_ledger = True
    with pytest.raises(d.DeployError, match="Cannot verify"):
        d.deploy(luna.args)
    assert not luna.calls and (luna.config / "bemind.jsonc").read_bytes() == before


@pytest.mark.parametrize("permission,read,write", [
    ({"*": "deny"}, "deny", "deny"), ({"memory_*": "deny"}, "deny", "deny"),
    ({"memory_write": "deny"}, "allow", "deny"), ({"memory_read": "ask"}, "ask", "ask"),
    ("deny", "deny", "deny"), ({"memory_write": {"*": "deny"}}, "allow", "deny"),
])
def test_existing_denials_not_weakened(luna, permission, read, write):
    original = json.dumps({"permission": permission})
    put_config(luna, original)
    d.deploy(luna.args)
    assert config(luna)["permission"]["memory_read"] == read
    assert config(luna)["permission"]["memory_write"] == write
    assert config(luna)["agent"]["plan"]["permission"]["memory_write"] == "deny"
    uninstall(luna)
    assert (luna.config / "bemind.jsonc").read_text() == original


def test_other_layer_deny_preserved_and_manual_entry_restored(luna):
    original = json.dumps({"mcp": {"ledger": legacy(luna)}, "permission": {"*": "deny"}})
    put_config(luna, original, "config.json")
    put_config(luna, '{"model":"untouched"}')
    luna.args.migrate_existing_ledger = True
    d.deploy(luna.args)
    assert "ledger" not in d.parse_config((luna.config / "config.json").read_bytes())["mcp"]
    assert config(luna)["permission"]["memory_write"] == "deny"
    uninstall(luna)
    assert (luna.config / "config.json").read_text() == original
    assert (luna.config / "bemind.jsonc").read_text() == '{"model":"untouched"}'


@pytest.mark.parametrize("changed", ["entry", "permission", "plan", "rules", "agents", "settings", "instruction"])
def test_changed_managed_fields_stop_before_commands(luna, changed):
    migrate(luna)
    d.deploy(luna.args)
    r = receipt(luna)
    path = luna.config / "bemind.jsonc"
    keys = {"entry": ["mcp", "memory", "enabled"], "permission": ["permission", "memory_write"],
            "plan": ["agent", "plan", "permission", "memory_write"], "instruction": ["instructions", 1]}
    if changed in keys:
        path.write_text(d.jsonc_edit.set_value(path.read_text(), keys[changed], "edited"))
    else:
        path = Path({"rules": r["instruction"], "agents": r["agents"]["path"], "settings": r["gateway"]["settings"]}[changed])
        path.write_bytes(path.read_bytes().replace(b"Shared memory rules", b"Edited memory rules") if changed == "agents" else path.read_bytes() + b" ")
    before = path.read_bytes()
    calls = len(luna.calls)
    for action in (lambda: d.deploy(luna.args), lambda: uninstall(luna)):
        with pytest.raises(d.DeployError):
            action()
    assert path.read_bytes() == before and len(luna.calls) == calls


def test_unrelated_edits_and_added_instructions_survive(luna):
    original, agents, agents_before, _ = migrate(luna)
    d.deploy(luna.args)
    path = luna.config / "bemind.jsonc"
    text = d.jsonc_edit.set_value(path.read_text(), ["theme"], "light")
    text = d.jsonc_edit.set_value(text, ["agent", "writer", "description"], "later value")
    text = d.jsonc_edit.set_value(text, ["instructions", 3], "later-rule.md")
    path.write_text(text)
    agents.write_bytes(agents.read_bytes().replace(b"keep last", b"new unrelated last"))
    uninstall(luna)
    expected = d.jsonc_edit.set_value(original.decode(), ["theme"], "light")
    expected = d.jsonc_edit.set_value(expected, ["agent", "writer", "description"], "later value")
    expected = d.jsonc_edit.set_value(expected, ["instructions", 3], "later-rule.md")
    assert path.read_bytes() == expected.encode()
    assert agents.read_bytes() == agents_before.replace(b"keep last", b"new unrelated last")


def test_upgrade_preserves_mode_owner_receipts_and_baseline(luna):
    original, agents, agents_before, _ = migrate(luna)
    d.deploy(luna.args)
    r = receipt(luna)
    durable = Path(r["gateway"]["state_dir"]) / "receipts" / (d.digest(b"write") + ".json")
    durable_data = d.encoded({"schema": 1, "request_id": "write", "state": "committed", "operation": "remember",
                              "payload_hash": d.digest(b"synthetic payload")})
    durable.write_bytes(durable_data)
    luna.args.memory_mode = None
    luna.args.migrate_existing_ledger = False
    luna.args.replace_instruction = []
    luna.args.agents_file = luna.args.agents_section = None
    luna.calls.clear()
    d.deploy(luna.args)
    updated = receipt(luna)
    assert updated["owner"] == r["owner"] and updated["memory_mode"] == "luna"
    assert len(updated["runtimes"]) == 2 and all(Path(p).exists() for p in updated["runtimes"])
    assert updated["gateway"]["state_dir"] != r["gateway"]["state_dir"]
    assert (Path(updated["gateway"]["state_dir"]) / "receipts" / durable.name).read_bytes() == durable_data
    assert json.loads(Path(r["gateway"]["settings"]).read_text())["enabled"] is False
    commands = [call[0] for call in luna.calls]
    assert next(i for i, c in enumerate(commands) if "stop" in c) < next(i for i, c in enumerate(commands) if "preflight" in c)
    uninstall(luna)
    assert (luna.config / "bemind.jsonc").read_bytes() == original
    assert agents.read_bytes() == agents_before and durable.read_bytes() == durable_data
    d.deploy(luna.args)
    assert receipt(luna)["memory_mode"] == "luna"
    assert durable.read_bytes() == durable_data


def test_schema_one_direct_upgrade_default_and_explicit_switch(luna):
    luna.args.memory_mode = "direct"
    d.deploy(luna.args)
    r = receipt(luna)
    r.pop("memory_mode")
    (luna.state / "receipt.json").write_text(json.dumps(r))
    luna.args.memory_mode = None
    d.deploy(luna.args)
    assert receipt(luna)["memory_mode"] == "direct" and "ledger" in config(luna)["mcp"]
    luna.args.memory_mode = "luna"
    d.deploy(luna.args)
    assert receipt(luna)["memory_mode"] == "luna" and "ledger" not in config(luna)["mcp"]
    uninstall(luna)
    assert "ledger" not in config(luna).get("mcp", {})


def test_explicit_luna_to_direct_retains_manual_baseline(luna):
    original, _, _, _ = migrate(luna)
    d.deploy(luna.args)
    luna.args.memory_mode = "direct"
    d.deploy(luna.args)
    assert receipt(luna)["memory_mode"] == "direct" and "memory" not in config(luna)["mcp"]
    assert config(luna)["permission"]["ledger_*"] == "allow"
    uninstall(luna)
    assert (luna.config / "bemind.jsonc").read_bytes() == original


def test_worker_stop_failure_deletes_nothing(luna, monkeypatch):
    d.deploy(luna.args)
    before = tree(luna.root)
    def fail(command, **kwargs):
        if "stop" in command:
            return SimpleNamespace(returncode=7)
        return luna.fake_run(command, **kwargs)
    monkeypatch.setattr(d, "run_process", fail)
    with pytest.raises(d.DeployError, match="failed"):
        uninstall(luna)
    for name, data in before.items():
        if not name.endswith(".log"):
            assert (luna.root / name).read_bytes() == data
    assert not (luna.state / "pending.json").exists()


def test_preflight_failure_rolls_back_and_reenables_previous(luna, monkeypatch):
    d.deploy(luna.args)
    r = receipt(luna)
    config_before = (luna.config / "bemind.jsonc").read_bytes()
    rules_before = Path(r["instruction"]).read_bytes()
    def fail(command, **kwargs):
        if "preflight" in command:
            return SimpleNamespace(returncode=8)
        return luna.fake_run(command, **kwargs)
    monkeypatch.setattr(d, "run_process", fail)
    with pytest.raises(d.DeployError, match="failed"):
        d.deploy(luna.args)
    assert receipt(luna) == r
    assert (luna.config / "bemind.jsonc").read_bytes() == config_before
    assert Path(r["instruction"]).read_bytes() == rules_before
    assert luna.calls[-1][0][-3:] == ["enable", "--settings", r["gateway"]["settings"]]
    assert not (luna.state / "pending.json").exists()


def test_failed_reenable_leaves_pending(luna, monkeypatch):
    d.deploy(luna.args)
    def fail(command, **kwargs):
        if "preflight" in command or "enable" in command:
            return SimpleNamespace(returncode=8)
        return luna.fake_run(command, **kwargs)
    monkeypatch.setattr(d, "run_process", fail)
    with pytest.raises(d.DeployError):
        d.deploy(luna.args)
    assert (luna.state / "pending.json").exists()


def test_concurrent_agents_edit_is_not_overwritten(luna, monkeypatch):
    original, agents, _, _ = migrate(luna)
    def edit(command, **kwargs):
        result = luna.fake_run(command, **kwargs)
        if "selftest" in command:
            agents.write_bytes(agents.read_bytes().replace(b"keep last", b"concurrent edit"))
        return result
    monkeypatch.setattr(d, "run_process", edit)
    with pytest.raises(d.DeployError, match="Concurrent"):
        d.deploy(luna.args)
    assert (luna.config / "bemind.jsonc").read_bytes() == original
    assert b"concurrent edit" in agents.read_bytes()


@pytest.mark.parametrize("field,value", [("gateway", {"settings": "/outside"}), ("manifest", []),
    ("instruction_owned", "yes"), ("runtimes", [123]), ("memory_mode", "invalid"), ("schema", True)])
def test_receipt_validation_before_actions(luna, field, value):
    d.deploy(luna.args)
    r = receipt(luna)
    r[field] = value
    (luna.state / "receipt.json").write_text(json.dumps(r))
    calls = len(luna.calls)
    with pytest.raises(d.DeployError, match="receipt"):
        uninstall(luna)
    assert len(luna.calls) == calls


def test_fresh_uninstall_and_dry_run_preserve_runtime_state(luna):
    luna.args.dry_run = True
    before = tree(luna.root)
    d.deploy(luna.args)
    assert tree(luna.root) == before and not luna.calls
    luna.args.dry_run = False
    d.deploy(luna.args)
    state = Path(receipt(luna)["gateway"]["state_dir"])
    for name in ("history.db", "auth.json", "worker.json"):
        (state / name).write_text("synthetic preserved state")
    main_auth = luna.home / "data/bemind/auth.json"
    main_auth.parent.mkdir(parents=True, exist_ok=True)
    main_auth.write_text("synthetic main auth must never be touched")
    before = tree(state)
    calls = len(luna.calls)
    uninstall(luna, "--dry-run")
    assert len(luna.calls) == calls
    uninstall(luna)
    assert tree(state) == before and main_auth.read_text() == "synthetic main auth must never be touched"
    assert not (luna.config / "bemind.jsonc").exists()
    assert "uninstalled" in uninstall(luna)


@pytest.mark.skipif(os.name != "posix", reason="Gateway runtime currently fails closed outside POSIX")
def test_real_gateway_control_commands_with_synthetic_settings_only(luna, monkeypatch):
    source = Path(__file__).resolve().parents[1] / "gateway/memory_gateway.py"
    if not source.exists():
        pytest.skip("Gateway runtime is developed independently; source not available yet")
    (luna.source / "memory_gateway.py").write_bytes(source.read_bytes())
    (luna.source / "gateway_backend_contract.py").write_text("\n".join(
        "def " + name + "(*a, **k): pass" for name in
        ("validate_write", "write_hash", "request_artifact", "receipt_path", "knowledge_fingerprint", "store_session", "store_identity", "check_stat")))
    def native_controls(command, cwd, env, **kwargs):
        if "--settings" not in command:
            return luna.fake_run(command, cwd, env, **kwargs)
        assert command[2] in ("preflight", "enable", "stop")
        assert Path(command[-1]).is_relative_to(luna.root)
        luna.calls.append((command, cwd, dict(env)))
        return subprocess.run([sys.executable, *command[1:]], cwd=cwd, env=env,
                              stdout=kwargs.get("stdout"), stderr=kwargs.get("stderr"), timeout=15)
    monkeypatch.setattr(d, "run_process", native_controls)
    d.deploy(luna.args)
    first = receipt(luna)
    d.deploy(luna.args)
    second = receipt(luna)
    assert json.loads(Path(first["gateway"]["settings"]).read_text())["enabled"] is False
    assert json.loads(Path(second["gateway"]["settings"]).read_text())["enabled"] is True
    uninstall(luna)
    assert json.loads(Path(second["gateway"]["settings"]).read_text())["enabled"] is False


def test_busy_ledger_session_rolls_back_stop_and_detachment(luna):
    d.deploy(luna.args)
    path = luna.config / "bemind.jsonc"
    before = path.read_bytes()
    r = receipt(luna)
    settings_before = Path(r["gateway"]["settings"]).read_bytes()
    with d.file_lock(luna.store / ".ledger.sessions.lock"):
        with pytest.raises(d.DeployError, match="busy"):
            uninstall(luna)
    assert path.read_bytes() == before and receipt(luna) == r
    assert Path(r["gateway"]["settings"]).read_bytes() == settings_before
    assert all(Path(p).is_dir() for p in r["runtimes"])


def test_migration_multiple_instruction_positions_and_comments(luna):
    original = '{"instructions":["first", /* A */ "old1", "middle", /* B */ "old2", "last"]}'
    put_config(luna, original)
    luna.args.replace_instruction = ["old1", "old2"]
    d.deploy(luna.args)
    assert config(luna)["instructions"] == ["first", str(luna.state / "MEMORY-RULES.md"), "middle", "last"]
    uninstall(luna)
    assert (luna.config / "bemind.jsonc").read_text() == original


def test_uninstall_receipt_write_failure_restores_every_managed_file(luna, monkeypatch):
    migrate(luna)
    d.deploy(luna.args)
    r = receipt(luna)
    paths = [luna.config / "bemind.jsonc", luna.state / "receipt.json", Path(r["gateway"]["settings"]),
             Path(r["agents"]["path"]), Path(r["instruction"])]
    before = {path: path.read_bytes() for path in paths}
    real_atomic = d.atomic
    def fail(path, data, expected):
        if path == luna.state / "receipt.json" and json.loads(data)["status"] == "uninstalled":
            raise OSError("synthetic receipt failure")
        return real_atomic(path, data, expected)
    monkeypatch.setattr(d, "atomic", fail)
    with pytest.raises(OSError):
        uninstall(luna)
    assert all(path.read_bytes() == data for path, data in before.items())
    assert all(Path(p).is_dir() for p in r["runtimes"])
    assert not (luna.state / "pending.json").exists()


def test_unrelated_additions_inside_created_parent_objects_survive(luna):
    put_config(luna, '{"theme":"old"}')
    d.deploy(luna.args)
    path = luna.config / "bemind.jsonc"
    text = path.read_text()
    for keys, value in ((["mcp", "unrelated"], {"command": ["keep"]}),
                        (["agent", "plan", "model"], "keep-model"),
                        (["permission", "*"], "deny"), (["instructions", 1], "keep.md")):
        text = d.jsonc_edit.set_value(text, keys, value)
    path.write_text(text)
    uninstall(luna)
    assert config(luna) == {"theme": "old", "mcp": {"unrelated": {"command": ["keep"]}},
                           "agent": {"plan": {"model": "keep-model"}}, "permission": {"*": "deny"},
                           "instructions": ["keep.md"]}


def test_explicit_missing_instruction_and_fenced_agents_heading(luna):
    luna.args.replace_instruction = ["missing-reference"]
    with pytest.raises(d.DeployError, match="not found"):
        d.deploy(luna.args)
    assert not luna.calls
    luna.args.replace_instruction = []
    agents = luna.home / "AGENTS.md"
    original = b"## Memory\n```md\n## Example\nnot a real section\n```\nlegacy rules\n## Other\nkeep\n"
    agents.write_bytes(original)
    luna.args.agents_file = str(agents)
    luna.args.agents_section = "## Memory"
    d.deploy(luna.args)
    assert b"legacy rules" not in agents.read_bytes() and b"## Other\nkeep\n" in agents.read_bytes()
    uninstall(luna)
    assert agents.read_bytes() == original
