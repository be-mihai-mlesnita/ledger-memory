"""An in-place restore refuses while a session holds the memory, and a crash part-way is finished or reported on the
next start (review fix, 0.3.0-rc3)."""
from __future__ import annotations

import hashlib
import json

import pytest

from ledger_memory.api import Ledger, LedgerError


def _sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _archive(L):
    return L.backup()["archive_path"]


def _marker(L, stage, identity):
    (L.home / "restore-in-progress.json").write_text(json.dumps(
        {"stage": stage, "target": str(L.db_path), "pre_image_identity": identity,
         "started_at": "2026-01-01T00:00:00+00:00"}))


def _identity(L):
    return Ledger._file_identity(L.db_path)


def test_restore_refused_while_a_session_holds_the_lock(home):
    L1 = Ledger().ensure()
    L1.remember("Synthetic fact", source="observed")
    archive = _archive(L1)
    with L1.session():
        L2 = Ledger().ensure()
        before = _sha(L2.db_path)
        with pytest.raises(LedgerError, match="in use by another session"):
            L2.restore(archive, str(L2.db_path), force=True)
        assert _sha(L2.db_path) == before
    assert not (L1.home / "restore-in-progress.json").exists()


def test_restore_into_a_new_file_ignores_sessions(home):
    L1 = Ledger().ensure()
    archive = _archive(L1)
    with L1.session():
        result = Ledger().ensure().restore(archive, str(home / "copy.db"))
    assert result["replaced"] is True and (home / "copy.db").exists()
    assert not (L1.home / "restore-in-progress.json").exists()


def test_in_place_restore_succeeds_without_sessions_and_leaves_no_marker(home):
    L1 = Ledger().ensure()
    archive = _archive(L1)
    result = L1.restore(archive, str(L1.db_path), force=True)
    assert result["replaced"] is True and "vectors" in result
    assert not (L1.home / "restore-in-progress.json").exists()


def test_recovery_restoring_unchanged(home):
    L1 = Ledger().ensure()
    _marker(L1, "restoring", _identity(L1))
    L2 = Ledger().ensure()
    assert L2.recovery_note == "an interrupted restore changed nothing"
    assert L2.status()["recovery"] == L2.recovery_note
    assert not (L1.home / "restore-in-progress.json").exists()


def test_recovery_restoring_changed_completes(home):
    L1 = Ledger().ensure()
    wal = L1.db_path.with_name(L1.db_path.name + "-wal")
    _marker(L1, "restoring", [0, 0])
    planted = b"SYNTHETIC-OLD-IMAGE-WAL" * 4
    wal.write_bytes(planted)
    note = Ledger()._recover_interrupted_restore()
    assert note == "an interrupted restore was completed"
    # Reading the config afterwards may create a fresh, empty -wal; the old image's bytes must be gone.
    assert not wal.exists() or planted not in wal.read_bytes(), "the old image's -wal must be removed"
    assert not (L1.home / "restore-in-progress.json").exists()


def test_recovery_restoring_unchanged_keeps_the_wal(home):
    L1 = Ledger().ensure()
    wal = L1.db_path.with_name(L1.db_path.name + "-wal")
    _marker(L1, "restoring", _identity(L1))
    planted = b"SYNTHETIC-LIVE-WAL" * 4
    wal.write_bytes(planted)
    assert Ledger()._recover_interrupted_restore() == "an interrupted restore changed nothing"
    assert wal.read_bytes() == planted, "nothing was replaced, so the live -wal belongs to this database"


def test_recovery_replaced_completes(home):
    L1 = Ledger().ensure()
    _marker(L1, "replaced", _identity(L1))
    assert Ledger().ensure().recovery_note == "an interrupted restore was completed"
    assert not (L1.home / "restore-in-progress.json").exists()


@pytest.mark.parametrize("content", ["{not json", "[]", '"x"', "", json.dumps({"stage": "unknown", "target": "x",
                                                                     "pre_image_identity": None, "started_at": "x"})])
def test_malformed_marker_refuses_start(home, content):
    L1 = Ledger().ensure()
    (L1.home / "restore-in-progress.json").write_text(content)
    with pytest.raises(LedgerError, match="unreadable marker"):
        Ledger().ensure()


def test_marker_for_another_target_is_removed(home):
    L1 = Ledger().ensure()
    (L1.home / "restore-in-progress.json").write_text(json.dumps(
        {"stage": "replaced", "target": str(home / "other.db"), "pre_image_identity": None, "started_at": "x"}))
    assert Ledger().ensure().recovery_note is None
    assert not (L1.home / "restore-in-progress.json").exists()


def test_a_reused_ledger_works_after_a_restore_between_its_sessions(home):
    """Found by the looping-writer test: cached actor ids from before a restore made the next write fail."""
    writer = Ledger().ensure()
    archive = _archive(Ledger().ensure())            # taken before the agent actor exists
    with writer.session():
        writer.remember("Synthetic before restore", source="observed")
    Ledger().ensure().restore(archive, str(writer.db_path), force=True)
    with writer.session():
        writer.ensure()
        assert writer.remember("Synthetic after restore", source="observed")["id"]


def test_ordinary_writes_after_a_stale_marker_never_delete_the_live_wal(home):
    """Review fix: a content hash changes with every write, the file identity only with a swap."""
    L1 = Ledger().ensure()
    _marker(L1, "restoring", _identity(L1))
    L1.remember("Synthetic write after the marker", source="observed")   # same file, new content
    wal = L1.db_path.with_name(L1.db_path.name + "-wal")
    planted = b"SYNTHETIC-LIVE-WAL" * 4
    wal.write_bytes(planted)
    assert Ledger()._recover_interrupted_restore() == "an interrupted restore changed nothing"
    assert wal.read_bytes() == planted


def test_failure_before_the_swap_removes_the_marker_and_keeps_the_error(home, monkeypatch):
    from ledger_memory import db_packaging
    L1 = Ledger().ensure()
    archive = _archive(L1)
    def boom(*args, **kwargs):
        raise OSError(28, "synthetic: no space left")
    monkeypatch.setattr(db_packaging, "restore_database", boom)
    before = _sha(L1.db_path)
    with pytest.raises(OSError, match="no space left"):   # not a refusal: the original error is kept
        L1.restore(archive, str(L1.db_path), force=True)
    assert _sha(L1.db_path) == before
    assert not (L1.home / "restore-in-progress.json").exists()
    def interrupt(*args, **kwargs):
        raise KeyboardInterrupt
    monkeypatch.setattr(db_packaging, "restore_database", interrupt)
    with pytest.raises(KeyboardInterrupt):
        L1.restore(archive, str(L1.db_path), force=True)
    assert not (L1.home / "restore-in-progress.json").exists()


def test_failure_after_the_swap_keeps_the_marker_and_the_next_start_finishes(home, monkeypatch):
    import os
    from ledger_memory import db_packaging
    L1 = Ledger().ensure()
    archive = _archive(L1)
    real = db_packaging.restore_database
    def swap_then_fail(*args, **kwargs):
        real(*args, **kwargs)
        raise PermissionError("synthetic failure after the swap")
    monkeypatch.setattr(db_packaging, "restore_database", swap_then_fail)
    with pytest.raises(LedgerError, match="next start of the memory finishes it"):
        L1.restore(archive, str(L1.db_path), force=True)
    assert (L1.home / "restore-in-progress.json").exists()
    monkeypatch.setattr(db_packaging, "restore_database", real)
    assert Ledger().ensure().recovery_note.startswith("an interrupted restore was completed")
    assert not (L1.home / "restore-in-progress.json").exists()


def test_recovery_survives_a_failing_vector_step(home, monkeypatch):
    L1 = Ledger().ensure()
    _marker(L1, "replaced", _identity(L1))
    def boom(self):
        raise RuntimeError("synthetic index failure")
    monkeypatch.setattr(Ledger, "_vector_step", boom)
    note = Ledger().ensure().recovery_note
    assert note.startswith("an interrupted restore was completed") and "ledger reindex" in note
    assert not (L1.home / "restore-in-progress.json").exists()


def test_recovery_removes_a_crashed_staging_copy(home):
    L1 = Ledger().ensure()
    staging = L1.db_path.with_name(L1.db_path.name + ".restoring")
    staging.write_bytes(b"synthetic partial copy")
    _marker(L1, "restoring", _identity(L1))
    Ledger().ensure()
    assert not staging.exists()
