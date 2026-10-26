"""Shared fixtures. Every test runs against a throwaway LEDGER_HOME; nothing touches a real memory."""
from __future__ import annotations

import ast

import pytest
from pathlib import Path


# `filename` is the path relative to the package source root (POSIX), so a same-named file elsewhere is not exempt.
LAUNCHER = "worker_spawn.py"
MULTIPROCESSING_ALLOWED = {"embedding_worker.py", "model_artifacts.py", "acceptance.py"}
CTYPES_ALLOWED = {"locking.py"}  # the Windows byte-range lock
OS_PROCESS_CALLS = {"system", "popen", "fork", "forkpty", "posix_spawn", "posix_spawnp"}


def process_boundary_offenders(source, filename):
    """Keep the sole fixed launcher exception identical in every process guard."""
    offenders = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module or ""]
            roots = {name.split(".")[0] for name in names}
            if "multiprocessing" in roots and filename not in MULTIPROCESSING_ALLOWED:
                offenders.append((filename, node.lineno, "multiprocessing outside its allowed modules"))
            if "ctypes" in roots and filename not in CTYPES_ALLOWED:
                offenders.append((filename, node.lineno, "ctypes outside the Windows lock"))
            if isinstance(node, ast.Import) and any(a.name.split(".")[0] == "subprocess" and a.asname for a in node.names):
                offenders.append((filename, node.lineno, "aliased subprocess import"))
        if isinstance(node, ast.Import) and any(a.name.split(".")[0] == "subprocess" for a in node.names):
            if filename != LAUNCHER:
                offenders.append((filename, node.lineno, "import subprocess"))
        elif isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] == "subprocess":
            offenders.append((filename, node.lineno, "from subprocess"))
        elif isinstance(node, ast.Call):
            fn = node.func
            if isinstance(fn, ast.Attribute) and isinstance(fn.value, ast.Name) and fn.value.id == "subprocess":
                if filename != LAUNCHER or fn.attr != "Popen":
                    offenders.append((filename, node.lineno, "unapproved process call"))
                else:
                    shell = next((kw.value for kw in node.keywords if kw.arg == "shell"), None)
                    if not isinstance(shell, ast.Constant) or shell.value is not False:
                        offenders.append((filename, node.lineno, "shell must be literal False"))
            if isinstance(fn, ast.Attribute) and isinstance(fn.value, ast.Name) and fn.value.id == "os" and (
                    fn.attr in OS_PROCESS_CALLS or fn.attr.startswith(("exec", "spawn"))):
                offenders.append((filename, node.lineno, "os process call"))
            if isinstance(fn, ast.Attribute) and fn.attr.startswith("create_subprocess"):
                offenders.append((filename, node.lineno, "asyncio subprocess"))
            if isinstance(fn, ast.Attribute) and fn.attr == "import_module" and node.args \
                    and isinstance(node.args[0], ast.Constant) \
                    and str(node.args[0].value).split(".")[0] in {"subprocess", "ctypes"}:
                offenders.append((filename, node.lineno, "import_module"))
            if isinstance(fn, ast.Name) and fn.id == "__import__" and node.args and isinstance(node.args[0], ast.Constant) \
                    and str(node.args[0].value).split(".")[0] in {"subprocess", "ctypes"}:
                offenders.append((filename, node.lineno, "__import__"))
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            value = node.value
            if "find-generic-password" in value or "add-generic-password" in value or value.strip().endswith("/bin/security") or value == "security":
                offenders.append((filename, node.lineno, "keychain command"))
    return offenders


EXTRACTOR = Path(__file__).resolve().parent.parent / "tools" / "extract.py"


def pytest_collection_modifyitems(config, items):
    """The extraction tool is build-only and left out of every release archive (.gitattributes export-ignore).
    Tests that load it are skipped, with that reason, when the package runs from a release archive."""
    if EXTRACTOR.exists():
        return
    import inspect
    skip = pytest.mark.skip(reason="build-only extraction tool is not part of a release archive")
    for item in items:
        function = getattr(item, "function", None)
        try:
            source = inspect.getsource(function) if function is not None else ""
        except (OSError, TypeError):
            continue
        if '"extract.py"' in source or "_extraction_replacement_calls" in source:
            item.add_marker(skip)


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    monkeypatch.setenv("LEDGER_HOME", str(h))
    monkeypatch.delenv("LEDGER_EMBEDDING_BACKEND", raising=False)
    monkeypatch.delenv("LEDGER_VECTORS_PATH", raising=False)
    return h
