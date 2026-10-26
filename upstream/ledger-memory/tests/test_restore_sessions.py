"""Restore versus live processes: an in-place restore refuses while any session process uses the memory, never loses a
write, and sessions or drains started during a restore wait or step aside (review fix, 0.3.0-rc3).
Children are separate Python processes using the same synthetic memory; every child is killed in `finally`."""
from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import time

import pytest

from ledger_memory import locking, reader
from ledger_memory.api import Ledger, LedgerError
from ledger_memory.semantic_pending import run_pending_worker

pytestmark = pytest.mark.skipif(os.name == "nt", reason="POSIX advisory locks in child processes")


def _child(code):
    return subprocess.Popen([sys.executable, "-c", "import os, sys, time\nfrom ledger_memory.api import Ledger\n" + code],
                            env=dict(os.environ), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def _stop(proc):
    if proc.poll() is None:
        proc.kill()
    proc.wait(timeout=10)


def _facts(L):
    return Ledger(L.home).status()["facts_current"]


def test_idle_session_process_refuses_in_place_restore(home):
    L = Ledger().ensure()
    L.remember("Synthetic fact before backup", source="observed")
    archive = L.backup()["archive_path"]
    child = _child("L = Ledger()\nwith L.session():\n    L.ensure(); L.remember('Synthetic child fact', source='observed')\n"
                   "    print('ready', flush=True); time.sleep(30)\n")
    try:
        assert child.stdout.readline().strip() == "ready"
        before = _facts(L)
        with pytest.raises(LedgerError, match="in use by another session"):
            Ledger().restore(archive, str(L.db_path), force=True)
        assert _facts(L) == before, "a refused restore must not change the memory"
    finally:
        _stop(child)
    assert Ledger().restore(archive, str(L.db_path), force=True)["replaced"] is True


def test_mcp_server_process_refuses_in_place_restore(home):
    """The real agent-facing server holds the lock for its whole life, even while idle between requests."""
    L = Ledger().ensure()
    archive = L.backup()["archive_path"]
    server = subprocess.Popen([sys.executable, "-m", "ledger_memory.mcp_server"], env=dict(os.environ),
                              stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        deadline = time.monotonic() + 20
        def held():
            try:
                with locking.exclusive(locking.session_lock_path(L.db_path), timeout=0):
                    return False
            except TimeoutError:
                return True
        while not held():   # `is_held` sees only exclusive owners; an exclusive attempt also sees shared holders
            assert server.poll() is None and time.monotonic() < deadline, "server did not take the session lock"
            time.sleep(0.05)
        with pytest.raises(LedgerError, match="in use by another session"):
            Ledger().restore(archive, str(L.db_path), force=True)
    finally:
        _stop(server)


def test_looping_writer_refuses_restore_and_keeps_its_writes(home):
    L = Ledger().ensure()
    archive = L.backup()["archive_path"]          # the empty memory: a successful restore would erase the loop's facts
    done = L.home / "writer-done"
    child = _child(f"L = Ledger()\nfor i in range(60):\n    with L.session():\n        L.ensure(); "
                   f"L.remember(f'Synthetic loop {{i}}', source='observed')\n"
                   f"with L.session():\n    open({str(done)!r}, 'w').close(); time.sleep(1.5)\n")
    refused = succeeded = 0
    try:
        while not done.exists():
            assert child.poll() is None, child.stderr.read()
            try:
                Ledger().restore(archive, str(L.db_path), force=True)
                succeeded += 1                    # only possible between two of the writer's sessions
            except LedgerError as exc:
                assert "in use by another session" in str(exc) or "starting or upgrading" in str(exc), str(exc)
                refused += 1
            time.sleep(0.01)
        # The writer still holds a session: from here on every restore must refuse.
        with pytest.raises(LedgerError, match="in use by another session"):
            Ledger().restore(archive, str(L.db_path), force=True)
        child.wait(timeout=30)
    finally:
        _stop(child)
    assert child.returncode == 0, child.stderr.read()
    assert refused > 0, "the test never overlapped the writer"
    con = sqlite3.connect(L.db_path)
    try:
        assert con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        stored = {row[0] for row in con.execute("SELECT statement FROM facts")}
    finally:
        con.close()
    assert "Synthetic loop 59" in stored, "the last write, made after any possible restore, must survive"

def test_session_starting_during_restore_waits_and_sees_restored_store(home):
    L = Ledger().ensure()
    L.remember("Synthetic restored fact", source="observed")
    with locking.exclusive(locking.session_lock_path(L.db_path)), locking.exclusive(reader.lock_path(L.db_path)):
        child = _child("L = Ledger()\nwith L.session():\n    print(L.ensure().status()['facts_current'], flush=True)\n")
        try:
            time.sleep(0.7)
            assert child.poll() is None, "a session must wait while a restore holds the memory"
        except BaseException:
            _stop(child)
            raise
    try:
        out, err = child.communicate(timeout=30)
    finally:
        _stop(child)
    assert child.returncode == 0, err
    assert out.strip() == "1"


def test_drain_during_restore_returns_maintenance_and_leaves_work(home):
    L = Ledger().ensure()
    with locking.exclusive(locking.session_lock_path(L.db_path)):
        assert run_pending_worker(L.db_path, L.vectors_path)["status"] == "maintenance"


def test_two_sessions_write_concurrently(home):
    L = Ledger().ensure()
    before = _facts(L)
    code = "L = Ledger()\nwith L.session():\n    L.ensure()\n    for i in range(20): L.remember(f'Synthetic {sys.argv[-1] if len(sys.argv) > 1 else os.getpid()} {i}', source='observed')\n"
    children = [_child(code), _child(code)]
    try:
        for c in children:
            c.wait(timeout=60)
            assert c.returncode == 0, c.stderr.read()
    finally:
        for c in children:
            _stop(c)
    assert _facts(L) == before + 40
