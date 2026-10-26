"""0.2.0: reads go through the one door under the local reader; `brief` and `recall_more`; the upgrade from 0.1.0.

Synthetic data only. Nothing here contacts a service: semantic search is off and the network is patched shut."""
from __future__ import annotations

import json
import logging
import os
import socket
import sqlite3
import stat
import subprocess
import sys
from importlib import resources
from pathlib import Path

import pytest

from ledger_memory import api, client_auth, config, db, facts, reader, selftest
from ledger_memory.api import Ledger, LedgerError
from ledger_memory.token_budget import _estimate_text_tokens


@pytest.fixture(autouse=True)
def no_network_no_keychain(monkeypatch):
    """Every test in this file runs with sockets and subprocesses shut: a read that tried either would fail loudly."""
    def refuse(*a, **k):
        raise AssertionError("a network connection or subprocess was attempted")
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(subprocess, "run", refuse)
    monkeypatch.setattr(subprocess, "Popen", refuse)


def _con(L):
    return sqlite3.connect(L.db_path)


def _read_log(L):
    con = _con(L)
    try:
        return con.execute("select surface, record_count from read_audit_entries order by id").fetchall()
    finally:
        con.close()


def _token(L) -> str:
    return reader.read_reader_token(L.db_path)


# ------------------------------------------------------------------ 1. the upgrade from 0.1.0

def _make_0_1_0_memory(home: Path) -> Path:
    """A memory exactly as 0.1.0 left it: only the baseline migration applied, no reader, no read-path tables."""
    home.mkdir(parents=True)
    path = home / "memory.db"
    con = sqlite3.connect(path)
    con.executescript((resources.files("ledger_memory.migrations") / "001_baseline.sql").read_text())
    con.execute("create table if not exists schema_migrations (version text primary key, applied_at text not null "
                "default current_timestamp)")
    con.execute("insert into schema_migrations (version) values ('001_baseline.sql')")
    con.commit()
    con.close()
    with db.connect(path) as c:
        db._seed_owner_actor(c)
        db._seed_system_actor(c)
    return path


def test_a_0_1_0_memory_upgrades_in_place_after_a_backup(home):
    path = _make_0_1_0_memory(home)
    a = facts.add_fact(path, statement="The printer is on floor 2", fact_type="location", confidence=0.9, actor_id=1,
                       authority_class="system_observed")
    b = facts.add_fact(path, statement="Stand-ups should stay short", fact_type="note", confidence=None, actor_id=1,
                       authority_class="model_inferred")
    from ledger_memory import decisions
    decisions.add_decision(path, title="Printer stays", summary="Agreed", rationale="closer", status="accepted",
                           fact_id=a.id, actor_id=1)
    before = sqlite3.connect(path)
    rows_before = before.execute("select id, statement, sensitivity_tier from facts order by id").fetchall()
    before.close()
    assert db.migration_status(path)[0] == "behind" or not db.database_is_initialized(path)

    L = Ledger().ensure()

    assert L.last_upgrade and L.last_upgrade["applied"] == ["002_read_path.sql"]
    archive = Path(L.last_upgrade["backup"])
    assert archive.exists() and Path(f"{archive}.sha256").exists()            # the backup exists and is verifiable
    assert "pre-upgrade" in archive.name
    assert db.migration_status(path) == ("current", [])
    con = _con(L)
    assert con.execute("select id, statement, sensitivity_tier from facts order by id").fetchall() == rows_before
    assert {r[0] for r in con.execute("select sensitivity_tier from facts")} == {1}            # no record's tier changed
    assert con.execute("select count(*) from decisions").fetchone()[0] == 1
    con.close()
    assert {x["id"] for x in L.current_beliefs()} == {a.id, b.id}              # all still readable, through the reader
    assert L.recall("printer floor")["results"][0]["id"] == a.id
    restored = L.restore(str(archive), str(home / "from-backup.db"))           # and the backup restores
    assert restored["sha256_verified"] is True
    again = Ledger().ensure()
    assert again.last_upgrade is None                                          # nothing to upgrade the second time


# ------------------------------------------------------------------ 2. the local reader

def _revoked_reader_fixture(connection, label, *, name="local-reader", revoked="synthetic-revoked", local=True):
    device_id = connection.execute(
        "INSERT INTO client_devices(name,token_hash,revoked_at,capabilities,tier_ceiling) VALUES(?,?,?,?,1)",
        (name, f"synthetic-hash-{label}", revoked, '["read.status"]')).lastrowid
    reader.emit_audit_and_event(connection, action_type="client_device.paired", target_type="client_device",
        target_id=device_id, actor_id=None, event_type="client_device.paired",
        summary=reader._reader_creation_summary(device_id) if local else f"client device paired: {name}")
    return device_id


def test_revoked_reader_cleanup_preserves_referenced_rows_and_history(home):
    ledger = Ledger().ensure()
    with db.connect(ledger.db_path) as connection:
        removable = _revoked_reader_fixture(connection, "removable")
        read_reference = _revoked_reader_fixture(connection, "read-reference")
        custom_reference = _revoked_reader_fixture(connection, "custom-reference")
        other = _revoked_reader_fixture(connection, "other", name="other-device")
        lookalike = _revoked_reader_fixture(connection, "lookalike")
        connection.execute("UPDATE client_devices SET device_class='headless' WHERE id=?", (lookalike,))
        remote_lookalike = _revoked_reader_fixture(connection, "remote-lookalike", local=False)
        tier_lookalike = _revoked_reader_fixture(connection, "tier-lookalike")
        connection.execute("UPDATE client_devices SET tier_ceiling=3 WHERE id=?", (tier_lookalike,))
        active = _revoked_reader_fixture(connection, "active", revoked=None)
        connection.execute("INSERT INTO read_audit_entries(device_id,record_count,effective_ceiling) VALUES(?,1,1)", (read_reference,))
        connection.execute('CREATE TABLE "custom reader refs"("device id" TEXT REFERENCES client_devices(id) ON DELETE CASCADE)')
        connection.execute('INSERT INTO "custom reader refs" VALUES(?)', (f"{custom_reference:04d}",))
        connection.execute('INSERT INTO "custom reader refs" VALUES(NULL)')
        connection.execute("""CREATE TRIGGER prove_archive_before_delete BEFORE DELETE ON client_devices
            BEGIN SELECT CASE WHEN NOT EXISTS (
                SELECT 1 FROM audit_entries a JOIN events e ON e.id=a.event_id
                WHERE a.action_type='client_device.pruned' AND a.target_id=OLD.id AND e.metadata IS NOT NULL
            ) THEN RAISE(ABORT,'missing pre-delete archive') END; END""")
        history = {table: [tuple(row) for row in connection.execute(f"SELECT * FROM {table} ORDER BY id")]
                   for table in ("audit_entries", "events", "read_audit_entries")}
    assert reader.prune_revoked_readers(ledger.db_path, owner_actor_id=ledger.owner_id) == 1
    with db.connect(ledger.db_path) as connection:
        remaining = {row["id"] for row in connection.execute("SELECT id FROM client_devices")}
        assert removable not in remaining
        assert {read_reference, custom_reference, other, lookalike, remote_lookalike, tier_lookalike, active} <= remaining
        assert connection.execute('SELECT COUNT(*) FROM "custom reader refs"').fetchone()[0] == 2
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        for table, original in history.items():
            ids = {row[0] for row in original}
            assert [tuple(row) for row in connection.execute(f"SELECT * FROM {table} ORDER BY id") if row[0] in ids] == original
        metadata = [json.loads(row[0]) for row in connection.execute("SELECT metadata FROM events WHERE event_type='client_device.pruned'")]
        assert len(metadata) == 1
        assert metadata[0] == {"id": removable, "name": "local-reader",
                               "created_at": metadata[0]["created_at"], "revoked_at": "synthetic-revoked"}
        assert "synthetic-hash" not in json.dumps(metadata)
    assert reader.prune_revoked_readers(ledger.db_path, owner_actor_id=ledger.owner_id) == 0


@pytest.mark.parametrize("kind", ["hash", "composite"])
def test_revoked_reader_cleanup_discovers_non_id_and_composite_references(home, kind):
    ledger = Ledger().ensure()
    with db.connect(ledger.db_path) as connection:
        referenced = _revoked_reader_fixture(connection, kind)
        removable = _revoked_reader_fixture(connection, "unreferenced")
        if kind == "hash":
            connection.execute("CREATE TABLE custom_refs(value TEXT REFERENCES client_devices(token_hash) ON DELETE CASCADE)")
            connection.execute("INSERT INTO custom_refs VALUES(?)", (f"synthetic-hash-{kind}",))
        else:
            connection.execute("CREATE UNIQUE INDEX custom_parent_key ON client_devices(id,name)")
            connection.execute("CREATE TABLE custom_refs(value TEXT,name TEXT, FOREIGN KEY(value,name) REFERENCES client_devices(id,name) ON DELETE CASCADE)")
            connection.execute("INSERT INTO custom_refs VALUES(?,?)", (f"{referenced:04d}", "local-reader"))
    assert reader.prune_revoked_readers(ledger.db_path) == 1
    with db.connect(ledger.db_path) as connection:
        assert connection.execute("SELECT 1 FROM client_devices WHERE id=?", (referenced,)).fetchone()
        assert connection.execute("SELECT 1 FROM client_devices WHERE id=?", (removable,)).fetchone() is None
        assert connection.execute("SELECT COUNT(*) FROM custom_refs").fetchone()[0] == 1
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_revoked_reader_cleanup_rolls_back_if_archive_fails_and_runs_at_start(home, monkeypatch):
    ledger = Ledger().ensure()
    with db.connect(ledger.db_path) as connection:
        removable = _revoked_reader_fixture(connection, "rollback")
    original = reader.emit_audit_and_event
    def fail(*args, **kwargs):
        raise RuntimeError("synthetic-private-detail")
    monkeypatch.setattr(reader, "emit_audit_and_event", fail)
    with pytest.raises(reader.ReaderError) as caught:
        reader.prune_revoked_readers(ledger.db_path)
    assert "synthetic-private-detail" not in str(caught.value)
    with db.connect(ledger.db_path) as connection:
        assert connection.execute("SELECT 1 FROM client_devices WHERE id=?", (removable,)).fetchone()
        assert connection.execute("SELECT COUNT(*) FROM events WHERE event_type='client_device.pruned'").fetchone()[0] == 0
    monkeypatch.setattr(reader, "emit_audit_and_event", original)
    Ledger().ensure()
    with db.connect(ledger.db_path) as connection:
        assert connection.execute("SELECT 1 FROM client_devices WHERE id=?", (removable,)).fetchone() is None

@pytest.mark.parametrize("code", [32, 33])
def test_reader_token_retries_windows_sharing_violations(home, monkeypatch, code):
    home.mkdir()
    path = home / "reader.token"
    path.write_text("synthetic-old\n")
    original = reader.os.replace
    calls = []
    sleeps = []
    def replace(source, destination):
        calls.append(source)
        if len(calls) <= 2:
            exc = PermissionError("synthetic-private-detail")
            exc.winerror = code
            raise exc
        original(source, destination)
    monkeypatch.setattr(reader.os, "replace", replace)
    monkeypatch.setattr(reader.time, "sleep", sleeps.append)
    reader._write_private(path, "synthetic-new")
    assert path.read_text() == "synthetic-new\n"
    assert len(calls) == 3
    assert sleeps == [0.02, 0.04]
    assert len(set(calls)) == 1
    assert list(home.glob(".reader.token.*.tmp")) == []


@pytest.mark.parametrize("code,expected_attempts", [(32, 6), (33, 6), (5, 1), (None, 1)])
def test_windows_token_replace_retry_preserves_existing_credential(home, monkeypatch, code, expected_attempts):
    ledger = Ledger().ensure()
    before = reader.token_path(ledger.db_path).read_bytes()
    raw = reader.read_reader_token(ledger.db_path)
    calls = []
    sleeps = []
    def replace(*args):
        calls.append(True)
        exc = PermissionError("synthetic-private-detail")
        if code is not None:
            exc.winerror = code
        raise exc
    monkeypatch.setattr(reader.os, "replace", replace)
    monkeypatch.setattr(reader.time, "sleep", sleeps.append)
    with pytest.raises(reader.ReaderError) as caught:
        reader._create_reader(ledger.db_path, ledger.owner_id)
    assert "existing credential retained" in str(caught.value)
    assert "synthetic-private-detail" not in str(caught.value)
    assert raw not in str(caught.value)
    assert len(calls) == expected_attempts
    assert len(sleeps) == expected_attempts - 1
    assert sum(sleeps) <= 1.0
    assert reader.token_path(ledger.db_path).read_bytes() == before
    assert client_auth.verify_device_token(ledger.db_path, raw) is not None
    assert list(home.glob(".reader.token.*.tmp")) == []
    with _con(ledger) as connection:
        assert connection.execute("SELECT COUNT(*) FROM client_devices WHERE revoked_at IS NULL").fetchone()[0] == 1


def test_reader_replacement_deadline_stops_retry(home, monkeypatch):
    home.mkdir()
    path = home / "reader.token"
    path.write_text("synthetic-old\n")
    clock = iter([10.0, 12.0])
    monkeypatch.setattr(reader.time, "monotonic", lambda: next(clock))
    calls = []
    def replace(*args):
        calls.append(True)
        exc = PermissionError("synthetic-private-detail")
        exc.winerror = 32
        raise exc
    monkeypatch.setattr(reader.os, "replace", replace)
    with pytest.raises(reader.ReaderError, match="existing credential retained"):
        reader._write_private(path, "synthetic-new")
    assert calls == [True]
    assert path.read_text() == "synthetic-old\n"
    assert list(home.glob(".reader.token.*.tmp")) == []


@pytest.mark.parametrize("after_sleep", [11.0, 11.5])
def test_reader_replacement_does_not_start_retry_after_backoff_expiry(home, monkeypatch, after_sleep):
    home.mkdir()
    path = home / "reader.token"
    path.write_text("synthetic-old\n")
    clock = [10.0]
    monkeypatch.setattr(reader.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(reader.time, "sleep", lambda seconds: clock.__setitem__(0, after_sleep))
    calls = []
    def replace(*args):
        calls.append(True)
        clock[0] = 10.99
        exc = PermissionError("synthetic-private-detail")
        exc.winerror = 32
        raise exc
    monkeypatch.setattr(reader.os, "replace", replace)
    with pytest.raises(reader.ReaderError, match="existing credential retained"):
        reader._write_private(path, "synthetic-new")
    assert calls == [True]
    assert path.read_text() == "synthetic-old\n"
    assert list(home.glob(".reader.token.*.tmp")) == []


def test_reader_cleanup_failure_is_explicit_and_keeps_previous_authentication(home, monkeypatch):
    ledger = Ledger().ensure()
    path = reader.token_path(ledger.db_path)
    before = path.read_bytes()
    raw = reader.read_reader_token(ledger.db_path)
    original_emit = reader.emit_audit_and_event
    def emit(*args, **kwargs):
        if kwargs["action_type"] == "client_device.revoked":
            raise RuntimeError("synthetic-private-cleanup-detail")
        return original_emit(*args, **kwargs)
    def replace(*args):
        exc = PermissionError("synthetic-private-publication-detail")
        exc.winerror = 32
        raise exc
    monkeypatch.setattr(reader, "emit_audit_and_event", emit)
    monkeypatch.setattr(reader.os, "replace", replace)
    monkeypatch.setattr(reader.time, "sleep", lambda seconds: None)
    with pytest.raises(reader.ReaderError, match="revocation unavailable") as caught:
        reader._create_reader(ledger.db_path, ledger.owner_id)
    assert caught.value.__suppress_context__ is True
    assert "synthetic-private" not in str(caught.value)
    assert path.read_bytes() == before
    assert client_auth.verify_device_token(ledger.db_path, raw) is not None
    assert list(home.glob(".reader.token.*.tmp")) == []
    with _con(ledger) as connection:
        # Failed cleanup is reported, not misrepresented as successful revocation.
        assert connection.execute("SELECT COUNT(*) FROM client_devices WHERE revoked_at IS NULL").fetchone()[0] == 2


def test_cli_reader_publication_failure_is_a_clear_refusal(home, monkeypatch, capsys):
    from ledger_memory import cli
    ledger = Ledger().ensure()
    path = reader.token_path(ledger.db_path)
    before = path.read_bytes()
    monkeypatch.setattr(reader, "read_reader_token", lambda database: None)
    monkeypatch.setattr(reader.time, "sleep", lambda seconds: None)
    def replace(*args):
        exc = PermissionError("synthetic-private-detail")
        exc.winerror = 32
        raise exc
    monkeypatch.setattr(reader.os, "replace", replace)
    assert cli.main(["init"]) == 2
    output = capsys.readouterr()
    assert output.out == ""
    assert "refused: reader credential replacement unavailable" in output.err
    assert "synthetic-private-detail" not in output.err
    assert path.read_bytes() == before

def test_the_reader_exists_after_first_start_is_private_and_never_leaks(home, capsys, caplog):
    caplog.set_level(logging.DEBUG)
    from ledger_memory import cli
    assert cli.main(["init"]) == 0
    L = Ledger()
    token = _token(L)
    assert token and len(token) >= 32
    path = reader.token_path(L.db_path)
    if os.name != "nt":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert reader.reader_file_is_private(L.db_path)
    con = _con(L)
    rows = con.execute("select name, tier_ceiling, revoked_at, token_hash from client_devices").fetchall()
    con.close()
    assert [(r[0], r[1], r[2]) for r in rows] == [("local-reader", 1, None)]
    assert token not in rows[0][3]                                              # only a hash is stored

    for argv in (["remember", "The NAS lives in the hall closet"], ["recall", "NAS"], ["beliefs"], ["brief", "where is the NAS"],
                 ["recall-more", "NAS"], ["status"], ["history", "1"], ["as-of", "2999-01-01"], ["init"]):
        cli.main(argv)
    out = capsys.readouterr()
    assert token not in out.out and token not in out.err
    assert token not in caplog.text
    for r in L.recall("NAS")["results"] + L.brief("NAS")["facts"]:
        assert token not in json.dumps(r)
    # the token is never in the database, the logs or any tool result; check every file the program wrote
    for f in L.home.rglob("*"):
        if f.is_file() and f.name != "reader.token" and f.suffix not in {".xz"}:
            assert token.encode() not in f.read_bytes(), f


def test_the_reader_is_recreated_when_its_file_is_lost(home):
    L = Ledger().ensure()
    L.remember("The wifi password rotates monthly", source="user_said")
    old = _token(L)
    reader.token_path(L.db_path).unlink()
    assert any("wifi" in x["statement"] for x in L.current_beliefs())        # a read re-creates it
    assert _token(L) and _token(L) != old
    con = _con(L)
    active = con.execute("select count(*) from client_devices where name = 'local-reader' and revoked_at is null").fetchone()[0]
    con.close()
    assert active == 1


# ------------------------------------------------------------------ 3. every read tool, one audited read each

def test_every_read_tool_returns_a_fresh_t1_memory_and_logs_one_mcp_read(home):
    L = Ledger().ensure()
    f = L.remember("The staging database lives on host db-2", source="observed", kind="system")
    L.ingest(text="The staging database backup runs nightly at two.", uri="note://backup")
    con = _con(L)
    assert con.execute("select sensitivity_tier from facts where id = ?", (f["id"],)).fetchone() == (1,)
    con.close()

    calls = {
        "recall": lambda: any(r["id"] == f["id"] for r in L.recall("staging database host")["results"]),
        "current_beliefs": lambda: any(r["id"] == f["id"] for r in L.current_beliefs()),
        "history": lambda: L.history(f["id"])["versions"][0]["statement"].startswith("The staging"),
        "beliefs_as_of": lambda: any(r["id"] == f["id"] for r in L.as_of("2999-01-01")["beliefs"]),
        "search_documents": lambda: bool(L.search_documents("staging backup")["results"]),
        "brief": lambda: any(r["id"] == f["id"] for r in L.brief("where is the staging database")["facts"]),
    }
    for name, call in calls.items():
        before = len(_read_log(L))
        assert call() is True, name
        new = _read_log(L)[before:]
        assert [s for s, _ in new] == ["mcp"] or name == "brief", (name, new)       # exactly one mcp read
        if name == "brief":
            assert new and all(s == "mcp" for s, _ in new), new
    before = len(_read_log(L))
    assert "staging" in L.recall_more("staging database host")["result"]
    assert all(s.startswith("mcp") for s, _ in _read_log(L)[before:]) and len(_read_log(L)) > before


def test_without_the_presence_refresh_tier_1_would_be_hidden(home, monkeypatch):
    """Revert check: the refresh is what makes the reader's tier-1 records visible."""
    L = Ledger().ensure()
    L.remember("The vault code is in the red folder", source="user_said")
    assert L.current_beliefs()
    monkeypatch.setattr(reader, "record_activity", lambda *a, **k: 0)         # as if the refresh were removed
    con = _con(L)
    con.execute("update client_devices set last_activity_at = null")
    con.commit(); con.close()
    assert L.current_beliefs() == [] and L.recall("vault code")["results"] == []


# ------------------------------------------------------------------ 4. brief

def _sum_cost(b: dict) -> int:
    total = sum(_estimate_text_tokens(f["statement"]) + 1 for f in b["facts"])
    total += sum(_estimate_text_tokens(d["decision"]) + 1 for d in b["decisions"])
    total += sum(_estimate_text_tokens(f"{c['fact_ids'][0]} {c['fact_ids'][1]} {c['kind']}") + 1 for c in b["contradictions"])
    total += sum(_estimate_text_tokens(i["text"] + " " + i["basis"]) + 1 for i in b["interpretations"])
    return total


def test_brief_returns_facts_a_linked_decision_a_contradiction_and_the_newest_date(home):
    L = Ledger().ensure()
    a = L.remember("The backup window is Friday night", source="user_said", kind="schedule")
    b = L.remember("The backup window is Sunday morning", source="observed", kind="schedule")
    L.remember("Lunch is at noon", source="inferred")
    L.dispute(a["id"], reason="two sources disagree", conflicts_with=b["id"])
    L.record_decision("Keep the backup window", summary="Use the agreed window", rationale="SECRET-RATIONALE-TEXT",
                      fact_id=a["id"])
    out = L.brief("when is the backup window", budget_tokens=600)
    assert {f["id"] for f in out["facts"]} >= {a["id"], b["id"]}
    assert all(f["id"] != 3 for f in out["facts"])                               # unrelated fact not padded in
    assert [d["decision"] for d in out["decisions"]] and "Keep the backup window" in out["decisions"][0]["decision"]
    assert "SECRET-RATIONALE-TEXT" not in json.dumps(out)                        # decisions ship without rationale
    assert any(set(c["fact_ids"]) == {a["id"], b["id"]} for c in out["contradictions"])
    assert out["newest_known_at"] and out["newest_known_at"] == max(f["known_at"] for f in out["facts"])
    assert set(out) >= {"facts", "decisions", "contradictions", "interpretations", "newest_known_at", "gaps"}
    assert _sum_cost(out) <= out["tokens_estimated"] <= out["budget_tokens"] == 600       # whole-payload estimate


def test_brief_stays_inside_a_tight_budget_and_says_what_it_left_out(home):
    L = Ledger().ensure()
    for i in range(30):
        L.remember(f"The deployment checklist step {i} requires a signed ticket and a second reviewer", source="user_said")
    out = L.brief("deployment checklist ticket reviewer", budget_tokens=200)
    assert out["tokens_estimated"] <= 200 and len(out["facts"]) < 30
    assert any("left out" in g for g in out["gaps"])
    with pytest.raises(LedgerError, match="question is empty"):
        L.brief("  ")


# ------------------------------------------------------------------ 5. recall_more

def test_recall_more_three_reads_then_limit_and_brief_resets_the_window(home):
    L = Ledger().ensure()
    L.remember("The ledger rotation happens on the first Monday", source="user_said")
    for n in range(3):
        r = L.recall_more("rotation first Monday")
        assert "read limit reached" not in r["result"] and r["reads_used"] == n + 1
    limited = L.recall_more("rotation first Monday")
    assert "read limit reached" in limited["result"]
    assert limited["reads_used"] == 3                                            # a refused call is not a read
    L.brief("when is the rotation")                                              # a new question: a new window
    again = L.recall_more("rotation first Monday")
    assert "[r" in again["result"] and "read limit reached" not in again["result"]


def test_recall_more_numbers_its_lines_and_refuses_a_malformed_query_without_a_read(home):
    L = Ledger().ensure()
    L.remember("The rotation is on the first Monday", source="user_said")
    for bad in ("", "   "):
        r = L.recall_more(bad)
        assert "malformed" in r["result"] and r["reads_used"] == 0
    assert len(_read_log(L)) == 1                                                # only remember's own echo was read
    r = L.recall_more("rotation Monday")
    assert "[r0]" in r["result"] and r["entries"][0]["type"] == "fact"
    r2 = L.recall_more("rotation Monday")
    assert "[r1]" in r2["result"]


def test_recall_more_never_allows_more_than_five_reads(home):
    L = Ledger().ensure()
    L.remember("The rotation is on the first Monday", source="user_said")
    config.set_config(L.db_path, key="context.tool_reads_max", value="9", actor_id=L.owner_id)
    L.reset_window()
    results = [L.recall_more("rotation Monday")["result"] for _ in range(7)]
    assert sum("read limit reached" not in r for r in results) == 5
    config.set_config(L.db_path, key="context.tool_reads_max", value="2", actor_id=L.owner_id)
    L.reset_window()
    assert sum("read limit reached" not in L.recall_more("rotation Monday")["result"] for _ in range(4)) == 2


def test_the_window_is_not_reset_by_recall_more_itself(home):
    """Revert check on the reset: only `brief` (and a new server) opens a window."""
    L = Ledger().ensure()
    L.remember("The rotation is on the first Monday", source="user_said")
    for _ in range(3):
        L.recall_more("rotation Monday")
    L.recall("rotation")                                                         # other reads do not reset it
    assert "read limit reached" in L.recall_more("rotation Monday")["result"]


# ------------------------------------------------------------------ 6. natural-question keyword search

def test_a_natural_question_finds_the_memory_with_semantic_search_off(home):
    L = Ledger().ensure()
    assert L.semantic_enabled is False
    f = L.remember("Changed the NAS admin password", source="user_said")
    L.remember("Bought new office chairs", source="user_said")
    assert [r["id"] for r in L.recall("NAS password")["results"]][:1] == [f["id"]]
    assert f["id"] in [x["id"] for x in L.brief("NAS password")["facts"]]
    assert "Changed the NAS admin password" in L.recall_more("NAS password")["result"]


# ------------------------------------------------------------------ 7. tiers are present, not used

def _mint_allowance(L, request_id="req-1"):
    from ledger_memory import interaction_authorization as ia
    fingerprint = __import__("ledger_memory.brain_identity", fromlist=["x"]).get_brain_identity(L.db_path).fingerprint
    nonce = ia.mint_tool_allowance(issuer=fingerprint, subject=ia.OWNER_SUBJECT,
                                   evidence_kind=ia.EVIDENCE_KIND_DEVICE_REQUEST, surface="mcp", request_id=request_id)
    return ia.ProofHandle(nonce=nonce, request_id=request_id)


def _authorize(L, query="vault code"):
    from ledger_memory.about_me_recall import tool_read_authorizer
    from ledger_memory.context_tool import ToolReadRequest
    return tool_read_authorizer(L.db_path, surface="mcp").authorize(ToolReadRequest(query, "mcp", "r", 1))


def test_allowance_path_with_a_lowered_or_stale_reader(home):
    from ledger_memory import interaction_authorization as ia
    L = Ledger().ensure()
    L.remember("The vault code is in the red folder", source="user_said")
    config.set_config(L.db_path, key="context.tool_reads_t1", value="on", actor_id=L.owner_id)
    L._ctx()                                                                      # presence refreshed: the normal case
    assert _authorize(L).lines                                                    # T1 reader, present: visible, no allowance

    con = _con(L)                                                                 # the reader stops being present
    con.execute("update client_devices set last_activity_at = null"); con.commit(); con.close()
    withheld = _authorize(L)
    assert withheld.lines == () and withheld.withheld_private is True            # a T1 record is withheld ...
    handle = _mint_allowance(L)
    token = ia.bind_tool_allowance(handle)
    try:
        assert _authorize(L).lines                                                # ... unless an allowance is presented
    finally:
        ia.clear_tool_allowance(token)

    con = _con(L)                                                                 # the reader deliberately lowered to T2
    con.execute("update client_devices set tier_ceiling = 2"); con.commit(); con.close()
    L._ctx()
    assert _authorize(L).lines == ()                                              # T1 is out of reach for a T2 reader
    handle = _mint_allowance(L, "req-2")
    token = ia.bind_tool_allowance(handle)
    try:
        assert _authorize(L).lines == ()                                          # an allowance never lifts a T2 credential
    finally:
        ia.clear_tool_allowance(token)
    assert L.current_beliefs() == []                                              # and the door hides it from every tool


# ------------------------------------------------------------------ 8. nothing leaves the machine

def test_the_offline_selftest_makes_no_connection(capsys):
    assert selftest.run(offline=True) == 0                                        # sockets and subprocesses are shut
    assert "SKIP  semantic search" in capsys.readouterr().out


def test_no_package_code_can_reach_the_keychain():
    from conftest import process_boundary_offenders
    root = Path(api.__file__).parent
    offenders = []
    for f in root.rglob("*.py"):
        offenders.extend(process_boundary_offenders(f.read_text(), f.relative_to(root).as_posix()))
    assert offenders == []
    for name in ("_token_from_keychain", "store_runtime_device_token", "probe_runtime_token_slot", "resolve_runtime_device_token"):
        assert not hasattr(client_auth, name)


# ------------------------------------------------------------------ presence refresh is throttled

def _presence_rows(L) -> int:
    con = _con(L)
    try:
        return con.execute("select count(*) from events where event_type = 'client_device.presence_refreshed'").fetchone()[0]
    finally:
        con.close()


def test_twenty_quick_reads_add_at_most_one_presence_row(home):
    L = Ledger().ensure()
    L.remember("The compressor is serviced in March", source="user_said")
    before = _presence_rows(L)
    for _ in range(20):
        assert L.current_beliefs()
    assert _presence_rows(L) - before <= 1


def test_a_read_past_half_the_window_refreshes_first_and_still_sees_tier_1(home):
    from ledger_memory.presence import DEFAULT_IDLE_SECONDS
    L = Ledger().ensure()
    L.remember("The compressor is serviced in March", source="user_said")
    assert L.current_beliefs()
    before = _presence_rows(L)
    assert L.current_beliefs()
    assert _presence_rows(L) == before                                            # still fresh: no refresh
    stale = (__import__("datetime").datetime.now(__import__("datetime").timezone.utc)
             - __import__("datetime").timedelta(seconds=DEFAULT_IDLE_SECONDS // 2 + 5)).strftime("%Y-%m-%d %H:%M:%S")
    con = _con(L)
    con.execute("update client_devices set last_activity_at = ?", (stale,)); con.commit(); con.close()
    assert L.current_beliefs()                                                    # refreshed first, T1 visible
    assert _presence_rows(L) == before + 1
