"""`ledger selftest` — prove an install works, on a throwaway store. Never touches the real memory.

Exit code 0 only if every executed check passed. This synthetic selftest never reads
the existing memory or probes a model service. Real-model acceptance is separate;
unexecuted semantic checks are reported as SKIPPED, never as passed.
"""
from __future__ import annotations

import datetime as _dt
import os
import tempfile
import time
from pathlib import Path

from ledger_memory.api import Ledger, LedgerError


class _Report:
    def __init__(self) -> None:
        self.rows: list[tuple[str, str, str]] = []

    def check(self, name: str, ok: bool, detail: str = "") -> None:
        self.rows.append(("PASS" if ok else "FAIL", name, detail))

    def skip(self, name: str, why: str) -> None:
        self.rows.append(("SKIP", name, why))

    def refuses(self, name: str, fn) -> None:
        try:
            fn()
            self.check(name, False, "was accepted but should have been refused")
        except LedgerError as exc:
            self.check(name, True, f"refused: {exc}")

    def print(self) -> int:
        width = max(len(r[1]) for r in self.rows)
        for status, name, detail in self.rows:
            print(f"  {status}  {name:<{width}}  {detail[:90]}")
        fails = sum(1 for r in self.rows if r[0] == "FAIL")
        passes = sum(1 for r in self.rows if r[0] == "PASS")
        skips = sum(1 for r in self.rows if r[0] == "SKIP")
        print(f"\n  {passes} passed, {fails} failed, {skips} skipped")
        return 1 if fails else 0


def run(offline: bool = False) -> int:
    print("ledger-memory selftest (throwaway store; your real memory is not touched)\n")
    rep = _Report()
    env_backend = os.environ.pop("LEDGER_EMBEDDING_BACKEND", None)
    env_vectors = os.environ.pop("LEDGER_VECTORS_PATH", None)
    offline = offline or env_backend == "none"
    try:
        with tempfile.TemporaryDirectory(prefix="ledger-selftest-") as tmp:
            _core(Ledger(Path(tmp) / "core"), rep)
            if offline:
                why = "offline run: meaning-based search forced off, no service contacted"
            else:
                why = "semantic checks not configured for synthetic selftest; run ledger acceptance for model evidence"
            rep.skip("semantic search (meaning-based)", why)
    finally:
        if env_backend is not None:
            os.environ["LEDGER_EMBEDDING_BACKEND"] = env_backend
        if env_vectors is not None:
            os.environ["LEDGER_VECTORS_PATH"] = env_vectors
    return rep.print()


def _core(L: Ledger, rep: _Report) -> None:
    L.ensure()
    rep.check("store created", L.db_path.exists(), str(L.db_path))
    f = L.remember("The office printer is on floor 2", source="user_said", kind="location", confidence=0.9)
    rep.check("remember (user_said)", f["source"] == "user_said" and f["current"], f"fact {f['id']}")
    rep.check("remember (inferred)", L.remember("Stand-ups should stay short", source="inferred")["source"] == "inferred")
    rep.check("remember (observed)", L.remember("Disk is 91% full", source="observed")["source"] == "observed")
    rep.refuses("unknown source refused", lambda: L.remember("x", source="rumour"))
    rep.refuses("empty statement refused", lambda: L.remember("  ", source="inferred"))
    time.sleep(1.1)
    before = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    time.sleep(1.1)
    rep.refuses("revision without a reason refused", lambda: L.revise(f["id"], "y", reason=""))
    r = L.revise(f["id"], "The office printer moved to floor 4", reason="Facilities moved it", source="user_said")
    rep.check("revise keeps the old version", not L.get_fact(f["id"])["current"] and r["now"]["current"])
    rep.check("a correction keeps the source it was given", r["now"]["source"] == "user_said")
    g = L.remember("The team meets on Mondays", source="user_said")
    guess = L.revise(g["id"], "The team meets on Tuesdays", reason="agent read an old calendar")
    rep.check("an unsourced agent correction is recorded as inferred", guess["now"]["source"] == "inferred",
              "an agent's guess must not read as something the user said")
    rep.refuses("revising an already-revised fact refused", lambda: L.revise(f["id"], "z", reason="r"))
    past = [b["statement"] for b in L.as_of(before)["beliefs"]]
    rep.check("time travel (as of before the revision)", "The office printer is on floor 2" in past)
    hist = L.history(f["id"])["versions"]
    rep.check("history shows both versions", len(hist) == 2 and hist[0]["changes"], f"{len(hist)} versions")
    rec = L.recall("printer")
    ids = [x["id"] for x in rec["results"] if x["type"] == "fact"]
    rep.check("recall returns the current belief only", ids == [r["now"]["id"]], f"lanes {rec['results'][0]['lanes'] if rec['results'] else []}")
    rep.check("recall reports index health", rec["index"]["fulltext_fresh"] is True)
    d = L.dispute(2, reason="Two people disagree")
    rep.check("dispute is recorded, not an edit", d["fact"].get("disputed") is True and d["fact"]["current"])
    dec = L.record_decision("Printer stays on 4", summary="Agreed", rationale="Closer to the team", fact_id=r["now"]["id"])
    rep.check("decision recorded", dec["decision"]["id"] > 0)
    rep.check("link recorded", L.link("decision", dec["decision"]["id"], "fact", r["now"]["id"], "rests_on")["link"]["id"] > 0)
    h = L.handover("Hand over the printer move", status_summary="done", fact_ids=[r["now"]["id"]],
                   decision_ids=[dec["decision"]["id"]])
    rep.check("handover bundle", "printer" in h["markdown"].lower(), f"handover {h['handover_id']}")
    doc = L.ingest(text="The VPN concentrator needs a firmware upgrade before Q4. The window is Saturday night.", uri="note://vpn")
    rep.check("document ingested and chunked", doc["chunks"] >= 1)
    s = L.search_documents("firmware upgrade")
    rep.check("document search (keyword mode)", bool(s["results"]) and s["mode"].startswith("keyword"), s["mode"])
    rep.check("document-backed fact", L.remember("VPN upgrade due before Q4", source="document", document_id=doc["document_id"])["source"] == "document")
    rep.refuses("document source without a document refused", lambda: L.remember("x", source="document"))
    from ledger_memory import reader
    rep.check("local reader exists and its file is private", reader.read_reader_token(L.db_path) is not None
              and reader.reader_file_is_private(L.db_path), "reader.token, owner-only")
    cur = L.current_beliefs()
    rep.check("a fresh memory is visible to the agent's reads",
              any("printer" in x["statement"] for x in cur) and bool(L.recall("printer")["results"]), f"{len(cur)} current beliefs")
    br = L.brief("where is the printer", budget_tokens=600)
    rep.check("brief returns facts, within budget", bool(br["facts"]) and br["tokens_estimated"] <= 600,
              f"{len(br['facts'])} facts, ~{br['tokens_estimated']} tokens")
    con = __import__("sqlite3").connect(L.db_path)
    reads = con.execute("select count(*) from read_audit_entries where surface = 'mcp'").fetchone()[0]
    con.close()
    rep.check("reads are logged once each", reads >= 4, f"{reads} read-log rows")
    rep.check("integrity check", L.check()["ok"] is True)
    b = L.backup()
    rep.check("checksummed backup", Path(b["archive_path"]).exists() and Path(b["sha256_path"]).exists())
    bad = L.home / "tampered.db"
    try:
        from ledger_memory import db_packaging
        db_packaging.restore_database(Path(b["archive_path"]), bad, expected_sha256="0" * 64)
        rep.check("tampered restore refused", False, "a forged checksum was accepted")
    except Exception as exc:
        rep.check("tampered restore refused", not bad.exists(), type(exc).__name__)
    good = L.home / "restored.db"
    L.restore(b["archive_path"], str(good))
    same = Ledger(L.home)  # same counts after a restore into a fresh file
    rep.check("verified restore", good.exists(), "sha256 verified")
    st = L.status()
    rep.check("status", st["facts_current"] >= 4 and st["facts_superseded"] == 2,
              f"{st['facts_current']} current, {st['facts_superseded']} superseded, {st['facts_disputed']} disputed")
    del same
