"""Review fix (0.3.0-rc2): the fact candidate lane reads only through the validated semantic read."""
import pytest
import sqlite3
from unittest.mock import patch

from ledger_memory.candidates import _semantic_lane_fact_ids


def test_candidate_lane_returns_only_current_facts(home, monkeypatch):
    monkeypatch.setenv("LEDGER_EMBEDDING_BACKEND", "fake")
    from ledger_memory.api import Ledger

    L = Ledger().ensure()

    id1 = L.remember("First fact about apples", source="observed")["id"]
    id2 = L.remember("Second fact about oranges", source="observed")["id"]

    L.revise(id1, "Revised fact about apples", reason="correction")
    L.refresh(repair=True)

    ids = _semantic_lane_fact_ids(L.db_path, "apples", limit=10, sidecar_path=L.vectors_path)

    assert len(ids) >= 1
    assert id1 not in ids, "a superseded fact must never be a candidate"
    with sqlite3.connect(L.db_path) as conn:
        conn.row_factory = sqlite3.Row
        current = {row["id"] for row in conn.execute("SELECT id FROM facts WHERE superseded_by_fact_id IS NULL").fetchall()}
    assert set(ids).issubset(current)


def test_candidate_lane_refuses_missing_or_stale_receipts(home, monkeypatch):
    monkeypatch.setenv("LEDGER_EMBEDDING_BACKEND", "fake")
    from ledger_memory.api import Ledger

    L = Ledger().ensure()
    id1 = L.remember("Fact about apples", source="observed")["id"]
    L.refresh(repair=True)

    # Delete receipts → lane should refuse
    with sqlite3.connect(L.vectors_path) as conn:
        conn.execute("DELETE FROM semantic_vector_revisions")
        conn.commit()

    ids = _semantic_lane_fact_ids(L.db_path, "apples", limit=10, sidecar_path=L.vectors_path)
    assert ids == []

    # Separately: monkeypatch semantic_search_outcome to return a receipt mismatch reason
    with patch("ledger_memory.semantic_search.semantic_search_outcome") as mock_outcome:
        mock_outcome.return_value = ([], "record revision receipt mismatch")
        ids = _semantic_lane_fact_ids(L.db_path, "apples", limit=10, sidecar_path=L.vectors_path)
        assert ids == []


def test_candidate_lane_never_raises(home, monkeypatch):
    monkeypatch.setenv("LEDGER_EMBEDDING_BACKEND", "fake")
    from ledger_memory.api import Ledger

    L = Ledger().ensure()
    L.remember("Some fact", source="observed")["id"]
    L.refresh(repair=True)

    with patch("ledger_memory.semantic_search.semantic_search_outcome") as mock_outcome:
        mock_outcome.side_effect = RuntimeError("backend exploded")
        ids = _semantic_lane_fact_ids(L.db_path, "fact", limit=10, sidecar_path=L.vectors_path)
        assert ids == []
