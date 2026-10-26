"""The only detached-process launcher; never a general command runner."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys


def _launch(module: str, *, database: Path | None = None, sidecar: Path | None = None) -> int:
    if module == "ledger_memory.embedding_worker":
        if database is not None or sidecar is not None:
            raise ValueError("embedding worker must not receive store paths")
        arguments = []
    elif module == "ledger_memory.semantic_pending":
        if database is None or sidecar is None:
            raise ValueError("pending worker requires database and sidecar paths")
        paths = [str(Path(path).absolute()) for path in (database, sidecar)]
        if any("\0" in path for path in paths):
            raise ValueError("invalid worker path")
        arguments = ["--database", paths[0], "--sidecar", paths[1]]
    else:
        raise ValueError("only the fixed embedding and pending workers may be launched")
    flags = 0
    if os.name == "nt":
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    child = subprocess.Popen(
        [sys.executable, "-I", "-m", module, *arguments],
        shell=False, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, close_fds=True, creationflags=flags,
        start_new_session=os.name != "nt",
    )
    return child.pid


def spawn_embedding_worker() -> int:
    return _launch("ledger_memory.embedding_worker")


def spawn_pending_worker(database: Path, sidecar: Path) -> int:
    return _launch("ledger_memory.semantic_pending", database=database, sidecar=sidecar)
