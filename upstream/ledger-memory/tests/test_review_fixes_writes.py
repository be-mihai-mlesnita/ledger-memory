"""Review fix (0.3.0-rc2): compound writes are
all-or-nothing, a failure after a commit is a warning (not a refusal), `newest_known_at` names a shown fact, restore
holds the store lock, and `brief` says when meaning-based search is degraded."""
from __future__ import annotations

import sqlite3

import pytest

from ledger_memory import briefing, context_packages, search_index
from ledger_memory.api import Ledger, LedgerError


def _count(L, table):
    with sqlite3.connect(L.db_path) as connection:
        return connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def test_duplicate_handover_ids_are_deduplicated(home):
    L = Ledger().ensure()
    f = L.remember("Synthetic handover fact", source="observed")["id"]
    out = L.handover("Synthetic objective", status_summary="synthetic", fact_ids=[f, f, f])
    assert out["handover_id"]
    assert (_count(L, "context_packages"), _count(L, "context_package_items")) == (1, 1)


def test_failed_handover_leaves_nothing(home, monkeypatch):
    L = Ledger().ensure()
    f = L.remember("Synthetic handover fact", source="observed")["id"]
    before = (_count(L, "context_packages"), _count(L, "context_package_items"))
    with pytest.raises(LedgerError):
        L.handover("Synthetic objective", status_summary="synthetic", fact_ids=[f, 999])
    def boom(*args, **kwargs):
        raise RuntimeError("synthetic failure inside the single transaction")
    monkeypatch.setattr(context_packages, "create_context_package", boom)
    with pytest.raises(RuntimeError):
        L.handover("Synthetic objective", status_summary="synthetic", fact_ids=[f])
    assert (_count(L, "context_packages"), _count(L, "context_package_items")) == before


def test_post_commit_refresh_failure_is_a_warning(home, monkeypatch):
    L = Ledger().ensure()
    def boom(*args, **kwargs):
        raise RuntimeError("synthetic index failure")
    monkeypatch.setattr(search_index, "rebuild_search_index", boom)
    out = L.remember("Synthetic saved despite the index", source="observed")
    assert any("search index not updated" in w for w in out["warnings"])
    with sqlite3.connect(L.db_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM facts WHERE id=?", (out["id"],)).fetchone()[0] == 1


def test_post_commit_echo_failure_still_reports_the_saved_fact(home, monkeypatch):
    L = Ledger().ensure()
    def boom(*args, **kwargs):
        raise RuntimeError("synthetic door failure")
    monkeypatch.setattr(Ledger, "_door", boom)
    out = L.remember("Synthetic echo failure", source="observed")
    assert out["id"] and out["echo_unavailable"] is True and out.get("withheld") is None


def test_brief_newest_known_at_matches_a_shown_fact():
    out = {"facts": [{"id": i, "statement": "x" * 160, "known_at": f"2026-01-0{i}", "source": "observed"}
                     for i in range(1, 6)],
           "decisions": [], "contradictions": [], "interpretations": [], "newest_known_at": "2026-01-05",
           "gaps": [], "budget_tokens": 250, "tokens_estimated": 0}
    briefing._fit(out, 250)
    assert 0 < len(out["facts"]) < 5, "the budget must force trimming for this test to mean anything"
    assert out["newest_known_at"] == max(f["known_at"] for f in out["facts"])


def test_trimming_keeps_the_degradation_notice():
    notice = briefing.DEGRADED_PREFIX + " (synthetic reason); facts below were found by keyword"
    out = {"facts": [{"id": i, "statement": "y" * 600, "known_at": "2026-01-01", "source": "observed"}
                     for i in range(1, 4)],
           "decisions": [], "contradictions": [], "interpretations": [], "newest_known_at": "2026-01-01",
           "gaps": [notice, "3 more matching facts left out; `recall_more` can look further"],
           "budget_tokens": 200, "tokens_estimated": 0}
    briefing._fit(out, 200)
    assert out["gaps"][0] == notice


def test_restore_holds_the_store_lock(home, monkeypatch):
    from ledger_memory import locking
    L = Ledger().ensure()
    L.remember("Synthetic restore fact", source="observed")
    L.backup()
    archive = sorted((L.home / "backups").glob("*.db.xz"))[-1]
    seen = []
    real = locking.exclusive
    def spy(path, **kwargs):
        seen.append(str(path))
        raise TimeoutError("synthetic: another process holds the store")
    monkeypatch.setattr(locking, "exclusive", spy)
    with pytest.raises(LedgerError, match="another process"):
        L.restore(str(archive), str(L.home / "copy.db"))
    assert seen and seen[0].endswith(".lock")
    monkeypatch.setattr(locking, "exclusive", real)
    assert L.restore(str(archive), str(L.home / "copy.db"))["replaced"] is True


def test_brief_states_semantic_degradation_reason(home, monkeypatch):
    L = Ledger().ensure()
    L.remember("Synthetic briefing fact", source="observed")
    notice = briefing.DEGRADED_PREFIX + " (synthetic reason); facts below were found by keyword"
    monkeypatch.setattr(briefing, "semantic_degradation", lambda *a: notice)
    assert L.brief("Synthetic briefing")["gaps"][0] == notice
    monkeypatch.setattr(briefing, "semantic_degradation", lambda *a: None)
    assert not any(g.startswith(briefing.DEGRADED_PREFIX) for g in L.brief("Synthetic briefing")["gaps"])


def test_restore_runs_while_the_store_lock_is_held(home, monkeypatch):
    import contextlib
    from ledger_memory import db_packaging, locking
    L = Ledger().ensure()
    L.remember("Synthetic restore fact", source="observed")
    L.backup()
    archive = sorted((L.home / "backups").glob("*.db.xz"))[-1]
    held = []
    real_lock, real_restore = locking.exclusive, db_packaging.restore_database
    @contextlib.contextmanager
    def tracking(path, **kwargs):
        with real_lock(path, **kwargs):
            held.append(True)
            try:
                yield
            finally:
                held.pop()
    def checked(*args, **kwargs):
        assert held, "the restore ran without the store lock"
        return real_restore(*args, **kwargs)
    monkeypatch.setattr(locking, "exclusive", tracking)
    monkeypatch.setattr(db_packaging, "restore_database", checked)
    assert L.restore(str(archive), str(L.home / "copy.db"))["replaced"] is True
