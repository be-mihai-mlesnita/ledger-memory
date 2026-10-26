"""Restoring over the live memory must leave the vectors file describing the RESTORED state, never the replaced one.
Uses the deterministic test-only embedding backend, so it runs without LM Studio."""
from __future__ import annotations

from ledger_memory import config
from ledger_memory.api import Ledger
from ledger_memory.semantic_search import semantic_lane_blocked


def test_in_place_restore_with_semantic_on_rebuilds_vectors(home):
    L = Ledger().ensure()
    config.set_config(L.db_path, key="embedding.backend", value="fake", actor_id=L.owner_id)
    L.remember("Invoices are sent as PDF attachments", source="user_said")
    archive = L.backup()["archive_path"]
    L.remember("The rotation schedule is monthly", source="user_said")      # not in the backup
    r = L.restore(archive, str(L.db_path), force=True)
    assert r["vectors"]["repaired"]["search_ready"] is True
    assert semantic_lane_blocked(L.db_path, L.vectors_path) is None
    statements = [x.get("statement") for x in L.recall("rotation schedule")["results"]]
    assert "The rotation schedule is monthly" not in statements
    assert L.recall("invoices")["warnings"] == []
    assert list(L.home.glob("memory.vectors.db.pre-replacement-*"))          # the replaced vectors are kept, not deleted


def test_in_place_restore_with_semantic_off_moves_stale_vectors_aside(home):
    L = Ledger().ensure()
    L.remember("Invoices are sent as PDF attachments", source="user_said")
    archive = L.backup()["archive_path"]                                    # taken while semantic search is off
    config.set_config(L.db_path, key="embedding.backend", value="fake", actor_id=L.owner_id)
    L.remember("The rotation schedule is monthly", source="user_said")      # embedded into the vectors file
    assert L.vectors_path.exists()
    r = L.restore(archive, str(L.db_path), force=True)
    assert "moved aside" in r["vectors"] and not L.vectors_path.exists()
    assert list(L.home.glob("memory.vectors.db.before-restore-*"))
    config.set_config(L.db_path, key="embedding.backend", value="fake", actor_id=L.owner_id)
    L.refresh(repair=True)                                                  # what `ledger semantic on` does
    assert semantic_lane_blocked(L.db_path, L.vectors_path) is None
    assert L.recall("invoices")["warnings"] == []
