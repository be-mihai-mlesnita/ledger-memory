"""Opt-in real release installation, always under a disposable HOME."""
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

pytestmark = pytest.mark.skipif(
    os.environ.get("BEMIND_DEPLOY_NATIVE_TEST") != "1" or os.name == "nt",
    reason="Opt-in macOS/Linux real-runtime deployment test",
)


def test_real_install_upgrade_uninstall_and_reinstall(tmp_path):
    import anyio
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    import jsonc_edit

    root = tmp_path.resolve() / "native deployment with spaces"
    home = root / "home"
    home.mkdir(parents=True)
    config_dir = home / ".config/bemind"
    config_dir.mkdir(parents=True)
    config = config_dir / "bemind.jsonc"
    config.write_text('{\n// retain this comment\n"model":"fixture/default", "mcp":{},\n}\n')
    app = home / ".local/share/bemind-ledger-memory"
    store = home / ".ledger-memory"
    deploy_dir = Path(__file__).resolve().parents[1]
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(("LEDGER_", "UV_", "XDG_", "PYTHON", "VIRTUAL_ENV"))}
    env.update(HOME=str(home), USERPROFILE=str(home), XDG_CONFIG_HOME=str(home / ".config"),
               XDG_DATA_HOME=str(home / ".local/share"), XDG_CACHE_HOME=str(home / ".cache"),
               BEMIND_DEPLOY_PYTHON=sys.executable)
    assert shutil.which("uv", path=env.get("PATH")), "uv is required for the opt-in test"
    options = ["--config-dir", str(config_dir), "--app-root", str(app), "--store", str(store)]

    def script(name, *flags, expected=0):
        install_flags = ["--memory-mode", "direct"] if name == "install.sh" else []
        result = subprocess.run(["/bin/sh", str(deploy_dir / name), *options, *install_flags, *flags],
                                cwd=root, env=env, capture_output=True, text=True, timeout=300)
        assert result.returncode == expected, result.stdout + result.stderr
        return result

    state = config_dir / "ledger-memory-deployment"
    script("install.sh", "--dry-run")
    assert not state.exists() and not app.exists() and not store.exists()
    script("install.sh")
    first = json.loads((state / "receipt.json").read_text())
    cli = first["entry"]["command"][0]
    stored = subprocess.run([cli, "remember", "Synthetic deployment sentinel survives uninstall.",
                             "--source", "observed"], env={**env, "LEDGER_HOME": str(store)},
                            cwd=root, capture_output=True, text=True, timeout=60)
    assert stored.returncode == 0, stored.stdout + stored.stderr
    script("install.sh")
    upgraded = json.loads((state / "receipt.json").read_text())
    assert len(upgraded["runtimes"]) == 2
    assert all(Path(item).is_dir() for item in upgraded["runtimes"])
    assert "retain this comment" in config.read_text()
    configured = jsonc_edit.loads(config.read_text())
    assert configured["mcp"]["ledger"] == upgraded["entry"]

    async def in_use():
        entry = upgraded["entry"]
        params = StdioServerParameters(command=entry["command"][0], args=entry["command"][1:],
                                       env={**env, **entry["environment"]}, cwd=str(root))
        with (root / "mcp-stderr.log").open("w") as errors:
            async with stdio_client(params, errlog=errors) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    assert len((await session.list_tools()).tools) == 15
                    before = config.read_bytes()
                    refusal = await anyio.to_thread.run_sync(lambda: script("uninstall.sh", expected=1))
                    assert "busy" in refusal.stderr
                    assert config.read_bytes() == before
                    assert all(Path(item).is_dir() for item in upgraded["runtimes"])

    anyio.run(in_use)
    script("uninstall.sh")
    assert all(not Path(item).exists() for item in upgraded["runtimes"])
    assert "ledger" not in jsonc_edit.loads(config.read_text())["mcp"]
    assert "retain this comment" in config.read_text()
    script("uninstall.sh")
    script("install.sh")
    reinstalled = json.loads((state / "receipt.json").read_text())
    assert len(reinstalled["runtimes"]) == 3
    with sqlite3.connect((store / "memory.db").as_uri() + "?mode=ro", uri=True) as database:
        assert database.execute("SELECT count(*) FROM facts WHERE statement=?",
                                ("Synthetic deployment sentinel survives uninstall.",)).fetchone()[0] == 1
    script("uninstall.sh")
    assert (store / "memory.db").is_file()
    assert (state / "backups").is_dir()
