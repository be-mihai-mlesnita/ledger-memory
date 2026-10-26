"""Every process that opens the memory holds the shared session lock (review fix, 0.3.0-rc3)."""
import pytest
from pathlib import Path
from ledger_memory import cli, locking, semantic_pending
from ledger_memory.api import Ledger
from ledger_memory.api import LedgerError


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("LEDGER_HOME", str(tmp_path))
    return tmp_path


def test_cli_command_holds_the_session_lock(home, monkeypatch):
    original_dispatch = cli._dispatch

    def mock_dispatch(L, args):
        session_lock = locking.session_lock_path(L.db_path)
        with pytest.raises(TimeoutError):
            with locking.exclusive(session_lock, timeout=0):
                pass
        return {}

    monkeypatch.setattr(cli, "_dispatch", mock_dispatch)
    assert cli.main(["status"]) == 0


def test_cli_restore_does_not_hold_the_session_lock(home, monkeypatch):
    original_dispatch = cli._dispatch

    def mock_dispatch(L, args):
        session_lock = locking.session_lock_path(L.db_path)
        with locking.exclusive(session_lock, timeout=0):
            pass
        return {}

    monkeypatch.setattr(cli, "_dispatch", mock_dispatch)
    restore_file = home / "x.db.xz"
    out_file = home / "o.db"
    restore_file.write_bytes(b"dummy")
    assert cli.main(["restore", str(restore_file), "--out", str(out_file)]) == 0


def test_cli_refuses_during_restore(home, monkeypatch):
    monkeypatch.setattr(cli, "SESSION_WAIT_SECONDS", 0)

    L = Ledger(home).ensure()
    session_lock = locking.session_lock_path(L.db_path)
    exclusive_ctx = locking.exclusive(session_lock)
    exclusive_ctx.__enter__()

    try:
        assert cli.main(["status"]) == 2
    finally:
        exclusive_ctx.__exit__(None, None, None)


def test_drain_returns_maintenance_during_restore(home):
    L = Ledger(home).ensure()
    session_lock = locking.session_lock_path(L.db_path)
    exclusive_ctx = locking.exclusive(session_lock)
    exclusive_ctx.__enter__()

    try:
        result = semantic_pending.run_pending_worker(L.db_path, L.vectors_path)
        assert result["status"] == "maintenance"
        assert result["published"] == 0
        assert result["failed"] == 0
        assert result["discarded"] == 0
    finally:
        exclusive_ctx.__exit__(None, None, None)
