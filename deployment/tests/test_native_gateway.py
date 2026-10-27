"""Opt-in native gateway test. Only synthetic memory/configuration is modified.

Optional BEMIND_GATEWAY_SEED_PYTHON seeds through an older installed interpreter;
BEMIND_GATEWAY_PREVIOUS_KIT selects a checksummed prior deployment ZIP for the first
installation. Upgrade/uninstall always use this checkout.
"""
import hashlib
import io
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import subprocess
import sys
import unicodedata
import zipfile

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


def extract_previous_kit(path, destination):
    sidecar = path.with_suffix(path.suffix + ".sha256")
    assert path.is_absolute() and path.is_file() and not path.is_symlink()
    assert sidecar.is_file() and not sidecar.is_symlink()
    checksum = sidecar.read_text("ascii").strip()
    assert re.fullmatch(r"[0-9a-fA-F]{64}(?:[ \t]+\*?[^\r\n]+)?", checksum), "Malformed kit checksum"
    assert path.stat().st_size <= 256 * 1024 * 1024, "Kit exceeds extraction limit"
    data = path.read_bytes()
    assert hashlib.sha256(data).hexdigest() == checksum[:64].lower(), "Kit checksum mismatch"
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        infos = archive.infolist()
        assert len(infos) <= 10000 and sum(info.file_size for info in infos) <= 256 * 1024 * 1024
        names, components = set(), {}
        for info in infos:
            name = info.filename
            parts = name.rstrip("/").split("/")
            kind = stat.S_IFMT(info.external_attr >> 16)
            assert (info.orig_filename == name and "\\" not in name
                    and all(part not in ("", ".", "..") and not part.endswith((" ", "."))
                            and not any(c in part for c in ':*?<>|"')
                            and not any(ord(c) < 32 for c in part) for part in parts)
                    and kind in (0, stat.S_IFREG, stat.S_IFDIR)
                    and not (kind == stat.S_IFREG and info.is_dir())
                    and not (kind == stat.S_IFDIR and not info.is_dir())), "Unsafe kit member"
            key = unicodedata.normalize("NFC", name.rstrip("/")).casefold()
            assert key not in names, "Duplicate kit member"
            names.add(key)
            for n in range(1, len(parts) + 1):
                prefix = "/".join(parts[:n])
                canonical = unicodedata.normalize("NFC", prefix).casefold()
                assert components.get(canonical, prefix) == prefix, "Aliased kit path"
                components[canonical] = prefix
        files = {info.filename for info in infos if not info.is_dir()}
        assert {"install.sh", "run.sh", "bemind_deploy.py"} <= files, "Missing kit installer"
        assert not any("/".join(info.filename.split("/")[:n]) in files
                       for info in infos for n in range(1, len(info.filename.rstrip("/").split("/"))))
        destination.mkdir(mode=0o700)
        for info in infos:
            target = destination / info.filename
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True, mode=0o700)
            else:
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                with target.open("xb") as stream:
                    stream.write(archive.read(info))
                target.chmod(0o700 if info.external_attr >> 16 & 0o111 else 0o600)
    return destination


def test_real_gateway_migration_read_write_upgrade_and_uninstall(tmp_path, monkeypatch):
    import anyio
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    root = tmp_path.resolve() / "gateway native with spaces"
    root.mkdir(mode=0o700)
    home, config_dir = root / "home", root / "home/.config/bemind"
    config_dir.mkdir(parents=True)
    store, app = home / ".ledger-memory", home / ".local/share/bemind-ledger-memory"
    store.mkdir(mode=0o700)
    monkeypatch.setenv("LEDGER_EMBEDDING_BACKEND", "none")
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
    seed_python = os.environ.get("BEMIND_GATEWAY_SEED_PYTHON")
    previous_kit = os.environ.get("BEMIND_GATEWAY_PREVIOUS_KIT")
    if seed_python:
        assert Path(seed_python).is_absolute() and Path(seed_python).is_file()
        result = subprocess.run([seed_python, "-I", "-B", "-c", """
import os
os.umask(0o077)
import json
import sys
from importlib.metadata import version
from pathlib import Path
from ledger_memory.api import Ledger

store = Path(sys.argv[1])
assert store.is_absolute()
assert os.environ["LEDGER_HOME"] == str(store)
assert os.environ["LEDGER_EMBEDDING_BACKEND"] == "none"
memory = Ledger(store, agent_name="native-gateway-fixture").ensure()
seed = memory.remember("The synthetic gateway validation color is cobalt.", source="observed", kind="system")
memory.ingest(text="Synthetic Juniper archive: October 20 proposal only; no deployment approval was given.",
              uri="synthetic://Juniper/release", media_type="text/plain")
print(json.dumps({"id": seed["id"], "version": version("ledger-memory")}))
""", str(store)], cwd=root, env={**environment, "LEDGER_HOME": str(store),
                                "LEDGER_EMBEDDING_BACKEND": "none", "XDG_DATA_HOME": str(home / ".local/share")},
                                text=True, capture_output=True, timeout=60)
        assert result.returncode == 0, result.stdout + result.stderr
        seed = json.loads(result.stdout)
        assert type(seed["id"]) is int and seed["id"] > 0
        assert seed["version"] != "0.3.0rc5", seed
    else:
        from ledger_memory.api import Ledger

        previous_umask = os.umask(0o077)
        try:
            memory = Ledger(store, agent_name="native-gateway-fixture").ensure()
            seed = memory.remember("The synthetic gateway validation color is cobalt.", source="observed", kind="system")
            memory.ingest(text="Synthetic Juniper archive: October 20 proposal only; no deployment approval was given.",
                          uri="synthetic://Juniper/release", media_type="text/plain")
        finally:
            os.umask(previous_umask)
    initial_deployment = extract_previous_kit(Path(previous_kit), root / "old-kit") if previous_kit else deployment
    base = ["--config-dir", str(config_dir), "--app-root", str(app), "--store", str(store)]
    install_options = ["--memory-mode", "luna", "--bemind-command", str(command)]
    migration = ["--migrate-existing-ledger", "--replace-instruction", str(old_rules),
                 "--agents-file", str(agents), "--agents-section", "## Shared long-term memory (all projects)"]

    def run(script, *extra, kit=deployment):
        install_flags = install_options if script == "install.sh" else []
        result = subprocess.run(["/bin/sh", str(kit / script), *base, *install_flags, *extra], cwd=root,
                                env=environment, text=True, capture_output=True, timeout=300)
        assert result.returncode == 0, result.stdout + result.stderr
        return result

    def runtime_version(entry):
        runtime_python = Path(entry["command"][0])
        assert runtime_python.is_relative_to(app / "versions") and runtime_python.is_file()
        result = subprocess.run([str(runtime_python), "-I", "-B", "-c",
                                 "import json; from importlib.metadata import version; "
                                 "print(json.dumps(version('ledger-memory')))"],
                                cwd=root, env=environment, text=True, capture_output=True, timeout=30)
        assert result.returncode == 0, result.stdout + result.stderr
        return json.loads(result.stdout)

    run("install.sh", *migration, "--dry-run", kit=initial_deployment)
    assert config.read_bytes() == original.encode() and agents.read_text() == agents_before
    run("install.sh", *migration, kit=initial_deployment)
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
    assert next(item for item in owned["manifest"] if item["path"] == str(config))["before"] == original
    assert any(path.read_bytes() == original.encode() for path in (receipt_path.parent / "backups").rglob("*")
               if path.is_file())
    if previous_kit:
        assert runtime_version(configured["mcp"]["memory"]) == seed["version"]
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
                    evidence = payload(await session.call_tool("read", {
                        "question": "What is the synthetic gateway validation color?", "project": "global", "purpose": "facts"}))
                    assert "error" not in evidence, evidence
                    assert any(item.get("id") == seed["id"] and "cobalt" in json.dumps(item)
                               for item in evidence["evidence"]), evidence
                    if first:
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
                    return committed["receipt"]

    first_receipt = anyio.run(use, configured["mcp"]["memory"], True)
    with sqlite3.connect((store / "memory.db").as_uri() + "?mode=ro", uri=True) as database:
        assert database.execute("SELECT count(*) FROM facts WHERE statement=?", (update["statement"],)).fetchone()[0] == 1
    run("install.sh")
    upgraded = jsonc_edit.loads(config.read_text())
    assert upgraded["mcp"]["memory"]["command"] != configured["mcp"]["memory"]["command"]
    if seed_python or previous_kit:
        assert runtime_version(upgraded["mcp"]["memory"]) == "0.3.0rc5"
    upgraded_owned = json.loads(receipt_path.read_text())
    assert next(item for item in upgraded_owned["manifest"] if item["path"] == str(config))["before"] == original
    assert anyio.run(use, upgraded["mcp"]["memory"], False) == first_receipt
    with sqlite3.connect((store / "memory.db").as_uri() + "?mode=ro", uri=True) as database:
        assert database.execute("SELECT count(*) FROM facts WHERE statement=?", (update["statement"],)).fetchone()[0] == 1
    run("uninstall.sh")
    assert config.read_bytes() == original.encode()
    assert agents.read_text() == agents_before
    assert (store / "memory.db").is_file()
    assert all(not Path(runtime).exists() for runtime in upgraded_owned["runtimes"])
    with sqlite3.connect((store / "memory.db").as_uri() + "?mode=ro", uri=True) as database:
        assert database.execute("SELECT count(*) FROM facts WHERE statement=?", (update["statement"],)).fetchone()[0] == 1
    run("uninstall.sh")
