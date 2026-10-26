"""Offline, synthetic acceptance evidence; never certifies unexecuted native checks."""
from __future__ import annotations

import json
import multiprocessing
import os
import platform
import shutil
import tempfile
import time
from importlib.metadata import version
from pathlib import Path

from ledger_memory.api import Ledger, LedgerError
from ledger_memory.selftest import _Report, _core


def run(output: Path | str) -> dict[str, str]:
    output = Path(output)
    # Reserve before any checks. O_EXCL also refuses dangling symlinks.
    try:
        fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except OSError:
        raise LedgerError("acceptance report unavailable; choose a new writable output path") from None
    try:
        stream = os.fdopen(fd, "w", encoding="utf-8")
    except BaseException as exc:
        os.close(fd)
        if not isinstance(exc, Exception):
            raise
        raise LedgerError("acceptance report could not be opened; preserve it as incomplete evidence") from None
    try:
        with stream:
            payload, status = _collect_report()
            json.dump(payload, stream, indent=2, allow_nan=False)
            stream.write("\n")
    except LedgerError:
        raise
    except (OSError, ValueError, ImportError):
        raise LedgerError("acceptance report could not be completed; preserve it as incomplete evidence") from None
    return {"report_path": str(output), "status": status}


def _synthetic_checks(scratch: str) -> list[dict]:
    for name in ("LEDGER_EMBEDDING_BACKEND", "LEDGER_VECTORS_PATH"):
        os.environ.pop(name, None)
    report = _Report()
    try:
        _core(Ledger(Path(scratch) / "synthetic"), report)
    except Exception:
        report.check("synthetic execution", False)
    return [{"name": name, "status": {"PASS": "passed", "SKIP": "unverified"}.get(status, "failed"),
             "reason": (None if status == "PASS" else "synthetic check not executed"
                        if status == "SKIP" else "synthetic assertion failed")}
            for status, name, _detail in report.rows]


def _synthetic_entry(scratch: str, buffer) -> None:
    data = json.dumps(_synthetic_checks(scratch), allow_nan=False).encode("utf-8")
    if len(data) > len(buffer) - 4:
        raise ValueError("synthetic report exceeds fixed buffer")
    buffer[4:4 + len(data)] = data
    buffer[:4] = len(data).to_bytes(4, "big")


def _bounded_checks(*, timeout: float = 120) -> list[dict]:
    if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or not 0 < timeout <= 120:
        raise ValueError("invalid acceptance deadline")
    deadline = time.monotonic() + timeout
    context = multiprocessing.get_context("spawn")
    buffer = context.RawArray("B", 65536)
    scratch = tempfile.mkdtemp(prefix="ledger-acceptance-")
    process = None
    try:
        process = context.Process(target=_synthetic_entry, args=(scratch, buffer))
        process.start()
        process.join(max(0, deadline - time.monotonic()))
        if process.is_alive() or process.exitcode != 0:
            return [{"name": "synthetic execution", "status": "failed",
                     "reason": "synthetic child unavailable or deadline exceeded"}]
        size = int.from_bytes(bytes(buffer[:4]), "big")
        if not 0 < size <= len(buffer) - 4:
            raise ValueError("invalid synthetic report size")
        checks = json.loads(bytes(buffer[4:4 + size]).decode("utf-8"))
        if not isinstance(checks, list) or not checks or len(checks) > 200:
            raise ValueError("invalid synthetic report checks")
        for row in checks:
            if (not isinstance(row, dict) or set(row) != {"name", "status", "reason"}
                    or not isinstance(row["name"], str) or not 0 < len(row["name"]) <= 200
                    or row["status"] not in ("passed", "failed", "unverified")
                    or row["reason"] not in (None, "synthetic assertion failed", "synthetic check not executed")):
                raise ValueError("invalid synthetic report row")
        return checks
    finally:
        if process is not None and process.pid is not None:
            if process.is_alive():
                process.terminate()
                process.join(1)
            if process.is_alive():
                process.kill()
                process.join(2)
            if process.is_alive():
                raise LedgerError("acceptance child could not be reaped; scratch retained")
            process.close()
        shutil.rmtree(scratch)


def _collect_report() -> tuple[dict, str]:
    try:
        checks = _bounded_checks()
    except LedgerError:
        raise
    except (OSError, ValueError, RuntimeError):
        checks = [{"name": "synthetic execution", "status": "failed",
                   "reason": "synthetic checks unavailable"}]
    if not checks:
        checks = [{"name": "synthetic execution", "status": "failed",
                   "reason": "synthetic checks unavailable"}]
    unverified = ["native_windows", "corporate_proxy", "defender_antivirus", "official_model_inference"]
    checks.extend({"name": name, "status": "unverified",
                   "reason": "requires separate explicit environment evidence"} for name in unverified)
    unverified = [row["name"] for row in checks if row["status"] == "unverified"]
    status = "failed" if any(row["status"] == "failed" for row in checks) else "partial"
    payload = {"package_version": version("ledger-memory"), "platform": platform.system(),
               "python_version": platform.python_version(), "checks": checks,
               "native_windows_verified": False, "unverified_checks": unverified}
    return payload, status
