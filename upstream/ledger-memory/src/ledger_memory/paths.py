"""Where the memory module keeps its files.

Every path is derived from the database path or an explicit environment variable — NEVER from the
process's current directory. An MCP server is launched by an agent from inside whatever project the
agent is working on; a cwd-relative default would silently scatter memory files into that project.
"""
from __future__ import annotations

import os
from pathlib import Path


def default_vectors_path(database_path: Path | str) -> Path:
    """The semantic-vector sidecar for a database: ``memory.db`` -> ``memory.vectors.db``.

    Override with ``LEDGER_VECTORS_PATH``. One sidecar per database, so two databases in the same
    directory can never share (and corrupt) each other's vectors.
    """
    override = os.environ.get("LEDGER_VECTORS_PATH")
    if override:
        return Path(override).expanduser()
    p = Path(database_path)
    return p.with_name(p.stem + ".vectors.db")
