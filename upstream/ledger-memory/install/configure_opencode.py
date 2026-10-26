"""Add (or remove) the ledger memory in opencode's GLOBAL config, merging into what is already there.

* Backs up an existing config before writing (opencode.json.bak-<UTC stamp>), writes atomically, re-reads to verify.
* Touches only two things it owns: the `mcp.ledger` entry and its own rules file in `instructions`.
* Refuses (exit 2, nothing written) if the existing config is not plain JSON, e.g. JSONC with comments;
  it then prints the snippet to merge by hand.

    python configure_opencode.py --app <install dir> --store <memory dir>     # add / update
    python configure_opencode.py --app <install dir> --remove                 # undo
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import shutil
import sys
from pathlib import Path

SERVER_KEY = "ledger"


def config_path() -> Path:
    base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "opencode" / "opencode.json"


def entry(app: Path, store: Path) -> dict:
    return {"type": "local", "command": [str(app / ".venv" / "bin" / "ledger-mcp")], "enabled": True,
            "environment": {"LEDGER_HOME": str(store), "LEDGER_AGENT_NAME": "opencode"}, "timeout": 15000}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--app", required=True, type=Path)
    ap.add_argument("--store", type=Path, default=Path(os.environ.get("LEDGER_HOME") or Path.home() / ".ledger-memory"))
    ap.add_argument("--remove", action="store_true")
    a = ap.parse_args()
    app, store = a.app.expanduser().resolve(), a.store.expanduser().resolve()
    rules = str(app / "MEMORY-RULES.md")
    cfg = config_path()
    snippet = {"mcp": {SERVER_KEY: entry(app, store)}, "instructions": [rules]}

    if cfg.with_suffix(".jsonc").exists() and not cfg.exists():
        print(f"refused: your opencode config is {cfg.with_suffix('.jsonc')} (JSONC); not editing it automatically.\n"
              f"Merge this into it by hand:\n{json.dumps(snippet, indent=2)}")
        return 2
    data: dict = {}
    if cfg.exists():
        try:
            data = json.loads(cfg.read_text() or "{}")
        except json.JSONDecodeError as exc:
            print(f"refused: {cfg} is not plain JSON ({exc}); not editing it automatically.\n"
                  f"Merge this into it by hand:\n{json.dumps(snippet, indent=2)}")
            return 2
        if not isinstance(data, dict):
            print(f"refused: {cfg} does not contain a JSON object")
            return 2

    before = json.dumps(data, sort_keys=True)
    if a.remove:
        (data.get("mcp") or {}).pop(SERVER_KEY, None)
        if isinstance(data.get("instructions"), list):
            data["instructions"] = [i for i in data["instructions"] if i != rules]
    else:
        data.setdefault("$schema", "https://opencode.ai/config.json")
        if not isinstance(data.setdefault("mcp", {}), dict):
            print("refused: existing `mcp` key is not an object"); return 2
        data["mcp"][SERVER_KEY] = entry(app, store)
        instr = data.setdefault("instructions", [])
        if not isinstance(instr, list):
            print("refused: existing `instructions` key is not a list"); return 2
        if rules not in instr:
            instr.append(rules)

    if json.dumps(data, sort_keys=True) == before:
        print(f"opencode config already up to date: {cfg}")
        return 0
    cfg.parent.mkdir(parents=True, exist_ok=True)
    backup = None
    if cfg.exists():
        backup = cfg.with_name(f"opencode.json.bak-{dt.datetime.now(dt.timezone.utc).strftime('%Y%m%d%H%M%S')}")
        shutil.copy2(cfg, backup)
    tmp = cfg.with_name(cfg.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    os.replace(tmp, cfg)
    json.loads(cfg.read_text())                     # verify what landed parses
    print(f"{'removed ledger from' if a.remove else 'updated'} {cfg}" + (f" (backup: {backup.name})" if backup else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
