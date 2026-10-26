"""Opt-in native gateway test. Only synthetic memory/configuration is modified."""
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import jsonc_edit

pytestmark = pytest.mark.skipif(os.environ.get("BEMIND_GATEWAY_NATIVE_TEST") != "1" or os.name != "posix",
                                reason="Opt-in native Luna gateway validation")


def payload(result):
    assert not result.is_error
    if result.structured_content is not None:
        value = result.structured_content
        return value["result"] if isinstance(value, dict) and set(value) == {"result"} else value
    return json.loads(result.content[0].text)


def test_real_gateway_migration_read_write_upgrade_and_uninstall(tmp_path, monkeypatch):
    import anyio
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    from ledger_memory.api import Ledger

    root = tmp_path.resolve() / "gateway native with spaces"
    home, config_dir = root / "home", root / "home/.config/bemind"
    config_dir.mkdir(parents=True)
    store, app = home / ".ledger-memory", home / ".local/share/bemind-ledger-memory"
    store.mkdir(mode=0o700)
    monkeypatch.setenv("LEDGER_EMBEDDING_BACKEND", "none")
    previous_umask = os.umask(0o077)
    try:
        memory = Ledger(store, agent_name="native-gateway-fixture").ensure()
        seed = memory.remember("The synthetic gateway validation color is cobalt.", source="observed", kind="system")
        memory.ingest(text="Synthetic Juniper archive: October 20 proposal only; no deployment approval was given.",
                      uri="synthetic://Juniper/release", media_type="text/plain")
    finally:
        os.umask(previous_umask)

    auth_data = Path(os.environ["BEMIND_GATEWAY_AUTH_DATA_HOME"])
    command = Path(os.environ["BEMIND_GATEWAY_BEMIND"])
    legacy_command = Path(os.environ["BEMIND_GATEWAY_LEGACY_MCP"])
    assert auth_data.is_absolute() and auth_data.is_dir() and command.is_file() and legacy_command.is_file()
    old_rules = config_dir / "old-memory-rules.md"
    old_rules.write_text("Synthetic prior rules. Use ledger_brief.\n")
    agents = config_dir / "AGENTS.md"
    agents_before = ("# Test global rules\n\nKeep every unrelated rule.\n\n"
                     "## Shared long-term memory (all projects)\n\nUse ledger_brief and its CLI fallback.\n\n"
                     "## Other policy\n\nNever send messages without permission.\n")
    agents.write_text(agents_before)
    legacy = {"type": "local", "command": [str(legacy_command)], "enabled": True,
              "environment": {"LEDGER_HOME": str(store)}, "timeout": 180000}
    config = config_dir / "bemind.jsonc"
    original = "// preserve this exact unrelated comment\n" + json.dumps({
        "model": "bemind-proxy/gpt-6-sol", "permission": {"ledger_*": "allow"},
        "instructions": [str(old_rules)], "mcp": {"ledger": legacy},
        "agent": {"custom": {"description": "Unrelated fixture agent", "permission": {"task": "deny"}}},
    }, indent=2) + "\n"
    config.write_text(original)
    deployment = Path(__file__).resolve().parents[1]
    environment = {k: v for k, v in os.environ.items()
                   if not k.startswith(("LEDGER_", "OPENCODE_", "XDG_", "UV_", "PYTHON", "VIRTUAL_ENV"))}
    environment.update(HOME=str(home), USERPROFILE=str(home), XDG_CONFIG_HOME=str(home / ".config"),
                       XDG_CACHE_HOME=str(home / ".cache"), XDG_DATA_HOME=str(auth_data),
                       BEMIND_DEPLOY_PYTHON=sys.executable)
    base = ["--config-dir", str(config_dir), "--app-root", str(app), "--store", str(store)]
    install_options = ["--memory-mode", "luna", "--bemind-command", str(command)]
    migration = ["--migrate-existing-ledger", "--replace-instruction", str(old_rules),
                 "--agents-file", str(agents), "--agents-section", "## Shared long-term memory (all projects)"]

    def run(script, *extra):
        install_flags = install_options if script == "install.sh" else []
        result = subprocess.run(["/bin/sh", str(deployment / script), *base, *install_flags, *extra], cwd=root,
                                env=environment, text=True, capture_output=True, timeout=300)
        assert result.returncode == 0, result.stdout + result.stderr
        return result

    run("install.sh", *migration, "--dry-run")
    assert config.read_text() == original and agents.read_text() == agents_before
    run("install.sh", *migration)
    configured = jsonc_edit.loads(config.read_text())
    assert "ledger" not in configured["mcp"] and "memory" in configured["mcp"]
    assert configured["permission"]["ledger_*"] == "deny"
    assert configured["permission"]["memory_write"] == "ask"
    assert configured["agent"]["plan"]["permission"]["memory_write"] == "deny"
    assert configured["model"] == "bemind-proxy/gpt-6-sol"
    assert old_rules.read_text() == "Synthetic prior rules. Use ledger_brief.\n"
    assert "Keep every unrelated rule." in agents.read_text()
    assert "Never send messages without permission." in agents.read_text()
    receipt_path = config_dir / "ledger-memory-deployment/receipt.json"
    owned = json.loads(receipt_path.read_text())
    write_id = "native-gateway-write-001"
    update = {"statement": "A synthetic gateway write was tested once.", "source": "observed", "kind": "note"}

    async def use(entry, first):
        params = StdioServerParameters(command=entry["command"][0], args=entry["command"][1:],
                                       env={**environment, **entry.get("environment", {})}, cwd=str(root))
        with (root / "public-mcp-stderr.log").open("a") as errors:
            async with stdio_client(params, errlog=errors) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    assert {tool.name for tool in (await session.list_tools()).tools} == {"read", "write"}
                    if first:
                        evidence = payload(await session.call_tool("read", {
                            "question": "What is the synthetic gateway validation color?", "project": "global", "purpose": "facts"}))
                        assert "error" not in evidence, evidence
                        assert any(item.get("id") == seed["id"] and "cobalt" in json.dumps(item)
                                   for item in evidence["evidence"]), evidence
                        history = payload(await session.call_tool("read", {
                            "question": "What does the Juniper release archive say about deployment approval?", "project": "Juniper", "purpose": "history"}))
                        assert "error" not in history and history["evidence"], history
                        assert history["warnings"]
                    committed = payload(await session.call_tool("write", {
                        "operation": "remember", "payload": update, "request_id": write_id}))
                    assert committed.get("receipt", {}).get("state") == "committed", committed
                    duplicate = payload(await session.call_tool("write", {
                        "operation": "remember", "payload": update, "request_id": write_id}))
                    assert duplicate["receipt"] == committed["receipt"]

    anyio.run(use, configured["mcp"]["memory"], True)
    with sqlite3.connect((store / "memory.db").as_uri() + "?mode=ro", uri=True) as database:
        assert database.execute("SELECT count(*) FROM facts WHERE statement=?", (update["statement"],)).fetchone()[0] == 1
    run("install.sh")
    upgraded = jsonc_edit.loads(config.read_text())
    assert upgraded["mcp"]["memory"]["command"] != configured["mcp"]["memory"]["command"]
    anyio.run(use, upgraded["mcp"]["memory"], False)
    run("uninstall.sh")
    assert config.read_text() == original
    assert agents.read_text() == agents_before
    assert (store / "memory.db").is_file()
    assert not Path(owned["runtimes"][0]).exists()
    with sqlite3.connect((store / "memory.db").as_uri() + "?mode=ro", uri=True) as database:
        assert database.execute("SELECT count(*) FROM facts WHERE statement=?", (update["statement"],)).fetchone()[0] == 1
    run("uninstall.sh")
