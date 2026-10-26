"""Behaviour the agent relies on, through the same API the MCP server and CLI use."""
from __future__ import annotations

import sqlite3

import pytest

from ledger_memory.api import KNOWLEDGE_TYPES, Ledger, LedgerError
from ledger_memory import config


def test_store_is_created_under_home_only(home, tmp_path, monkeypatch):
    elsewhere = tmp_path / "cwd"; elsewhere.mkdir(); monkeypatch.chdir(elsewhere)
    L = Ledger().ensure()
    L.remember("x is y", source="inferred")
    L.backup()
    assert L.db_path == home / "memory.db" and L.db_path.exists()
    assert list(elsewhere.iterdir()) == []                      # nothing ever lands in the working directory
    assert config.get_config(L.db_path, "search.index_types") == KNOWLEDGE_TYPES


def test_nothing_is_deleted_by_a_revision(home):
    L = Ledger().ensure()
    a = L.remember("The deploy window is Friday", source="user_said")
    b = L.revise(a["id"], "The deploy window is Thursday", reason="moved by the release manager")
    con = sqlite3.connect(L.db_path)
    rows = con.execute("select id, statement from facts order by id").fetchall()
    assert rows == [(a["id"], "The deploy window is Friday"), (b["now"]["id"], "The deploy window is Thursday")]
    assert [x["id"] for x in L.current_beliefs()] == [b["now"]["id"]]
    assert L.history(b["now"]["id"])["versions"][0]["changes"]


def test_inference_cannot_be_laundered_into_user_said(home):
    L = Ledger().ensure()
    a = L.remember("The user probably prefers tabs", source="inferred")
    assert L.revise(a["id"], "The user prefers tabs", reason="more context")["now"]["source"] == "inferred"


@pytest.mark.parametrize("call, message", [
    (lambda L: L.remember("x", source="gossip"), "source must be one of"),
    (lambda L: L.remember(" ", source="inferred"), "statement is required"),
    (lambda L: L.remember("x", source="inferred", confidence=2), "confidence must be between 0 and 1"),
    (lambda L: L.revise(999, "x", reason="r"), "no fact 999"),
    (lambda L: L.dispute(1, reason=" "), "reason is required"),
    (lambda L: L.recall("  "), "query is empty"),
])
def test_refusals_explain_themselves(home, call, message):
    L = Ledger().ensure()
    L.remember("seed", source="inferred")
    with pytest.raises(LedgerError, match=message):
        call(L)


def test_dispute_is_derived_from_events_and_counted(home):
    L = Ledger().ensure()
    a = L.remember("The VPN is in rack 3", source="observed")
    d = L.dispute(a["id"], reason="the floor plan says rack 4")
    assert d["fact"]["disputed"] is True and d["fact"]["current"] is True
    assert d["fact"]["disputes"][0]["reason"] == "the floor plan says rack 4"
    assert L.status()["facts_disputed"] == 1


def test_recall_indexes_knowledge_and_stays_fresh(home):
    L = Ledger().ensure()
    L.remember("Invoices go to the shared finance mailbox", source="user_said")
    L.record_decision("Use the finance mailbox", summary="All invoices", rationale="one place to look")
    r = L.recall("finance mailbox")
    assert {x["type"] for x in r["results"]} == {"fact", "decision"}
    assert r["index"]["fulltext_fresh"] is True and r["warnings"] == []


# ---------------------------------------------------------------- validation happens BEFORE any write

def _counts(L):
    con = sqlite3.connect(L.db_path)
    try:
        return {t: con.execute(f"select count(*) from {t}").fetchone()[0]
                for t in ("facts", "decisions", "relationships", "context_packages", "context_package_items", "documents")}
    finally:
        con.close()


BAD_WRITES = {
    "link to a missing record": lambda L, f, d: L.link("fact", f, "fact", 999, "depends_on"),
    "link with an unknown type": lambda L, f, d: L.link("planet", 1, "fact", f, "orbits"),
    "link without a relationship": lambda L, f, d: L.link("fact", f, "decision", d, " "),
    "link to itself": lambda L, f, d: L.link("fact", f, "fact", f, "same_as"),
    "handover citing a missing fact": lambda L, f, d: L.handover("o", status_summary="s", fact_ids=[f, 999]),
    "handover citing a missing decision": lambda L, f, d: L.handover("o", status_summary="s", decision_ids=[999]),
    "handover without an objective": lambda L, f, d: L.handover(" ", status_summary="s"),
    "decision with an unknown status": lambda L, f, d: L.record_decision("t", summary="s", rationale="r", status="maybe"),
    "decision without a rationale": lambda L, f, d: L.record_decision("t", summary="s", rationale=""),
    "revision to an empty statement": lambda L, f, d: L.revise(f, " ", reason="r"),
    "revision with a bad confidence": lambda L, f, d: L.revise(f, "x", reason="r", confidence=7),
    "a fact longer than a fact": lambda L, f, d: L.remember("x" * 5000, source="inferred"),
    "ingest a directory": lambda L, f, d: L.ingest(path=str(L.home)),
}


@pytest.mark.parametrize("name", sorted(BAD_WRITES))
def test_bad_writes_are_refused_and_write_nothing(home, name):
    L = Ledger().ensure()
    f = L.remember("seed fact", source="inferred")["id"]
    d = L.record_decision("seed", summary="s", rationale="r")["decision"]["id"]
    before = _counts(L)
    with pytest.raises(LedgerError):
        BAD_WRITES[name](L, f, d)
    assert _counts(L) == before                                  # append-only: a refusal must leave no trace


def test_as_of_parses_and_normalises_time(home):
    L = Ledger().ensure()
    L.remember("seed", source="inferred")
    with pytest.raises(LedgerError, match="not an ISO-8601"):
        L.as_of("yesterday-ish")                                 # text comparison would have returned everything
    assert L.as_of("2020-01-01")["as_of"] == "2020-01-01T00:00:00" and L.as_of("2020-01-01")["beliefs"] == []
    assert L.as_of("2020-01-01T03:00:00+03:00")["as_of"] == "2020-01-01T00:00:00"
    assert len(L.as_of("2999-01-01T00:00:00Z")["beliefs"]) == 1


def test_limits_are_clamped(home):
    L = Ledger().ensure()
    L.remember("seed one", source="inferred"); L.remember("seed two", source="inferred")
    assert len(L.current_beliefs(limit=-1)) == 1 and len(L.recall("seed", limit=0)["results"]) == 1


def test_restore_refusals_are_clean(home, tmp_path):
    L = Ledger().ensure()
    archive = L.backup()["archive_path"]
    out = tmp_path / "restored.db"
    assert L.restore(archive, str(out))["sha256_verified"] is True
    with pytest.raises(LedgerError, match="--force"):
        L.restore(archive, str(out))
    (tmp_path / "bad.db.xz").write_text("not an archive")
    with pytest.raises(LedgerError, match="no .sha256"):
        L.restore(str(tmp_path / "bad.db.xz"), str(tmp_path / "x.db"))
    with pytest.raises(LedgerError):
        L.restore(str(tmp_path / "missing.db.xz"), str(tmp_path / "y.db"))
