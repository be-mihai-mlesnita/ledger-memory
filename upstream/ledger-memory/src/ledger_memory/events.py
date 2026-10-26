from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from ledger_memory.db import connect


@dataclass(frozen=True)
class Event:
    id: int
    event_type: str
    task_id: int | None
    summary: str
    occurred_at: str


def add_event(
    database_path: Path,
    *,
    event_type: str,
    summary: str,
    task_id: int | None = None,
    actor_id: int | None = None,
    metadata: str | None = None,
) -> Event:
    """Insert one event (event-only, no audit). ``metadata`` is the optional
    machine-readable payload column the baseline schema's always had; callers that want a reader to
    parse something pass JSON here and keep ``summary`` as the human line."""
    with connect(database_path) as connection:
        cursor = connection.execute(
            """
            INSERT INTO events (event_type, task_id, actor_id, summary, metadata)
            VALUES (?, ?, ?, ?, ?)
            """,
            (event_type, task_id, actor_id, summary, metadata),
        )
        event_id = int(cursor.lastrowid)
        row = connection.execute(
            """
            SELECT id, event_type, task_id, summary, occurred_at
            FROM events
            WHERE id = ?
            """,
            (event_id,),
        ).fetchone()

    if row is None:
        raise RuntimeError(f"created event could not be loaded: {event_id}")
    return _row_to_event(row)


def list_events(database_path: Path, *, task_id: int | None = None) -> list[Event]:
    with connect(database_path) as connection:
        if task_id is None:
            rows = connection.execute(
                """
                SELECT id, event_type, task_id, summary, occurred_at
                FROM events
                ORDER BY id
                """
            ).fetchall()
        else:
            rows = connection.execute(
                """
                SELECT id, event_type, task_id, summary, occurred_at
                FROM events
                WHERE task_id = ?
                ORDER BY id
                """,
                (task_id,),
            ).fetchall()
    return [_row_to_event(row) for row in rows]


def _row_to_event(row) -> Event:
    task_id = row["task_id"]
    return Event(
        id=int(row["id"]),
        event_type=str(row["event_type"]),
        task_id=None if task_id is None else int(task_id),
        summary=str(row["summary"]),
        occurred_at=str(row["occurred_at"]),
    )


# ---------------------------------------------------------------------------
# Emit-on-change telemetry
#
# A telemetry row that repeats the previous row byte-for-byte carries no information; the
# 2026-09-02 measurement found 6,512 identical `model.embedding_run` rows and 13,993
# `scheduled_job.executed` rows whose top-6 summaries were all identical all-clear lines. The
# primitives below let a writer record a CHANGE of state, plus one bounded heartbeat row per
# `heartbeat_seconds` while the state holds, and nothing in between. State is derived from the
# newest row already in the table (its `metadata` JSON), never from a new cursor — a config-keyed
# cursor would itself emit a `config.set` row per write and defeat the point.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LatestEvent:
    """The newest row of one event type: its id, stored metadata, and wall-clock age."""

    id: int
    metadata: str | None
    age_seconds: int | None  # None when occurred_at cannot be parsed — treated as "heartbeat due"


def latest_event(connection, *, event_type: str, task_id: int | None = None) -> LatestEvent | None:
    """The newest ``event_type`` row (optionally scoped to ``task_id``) on the caller's open
    connection, with its age computed in SQL against the wall clock: the calendar a reader of
    the events table sees is ``occurred_at``, so the cadence is measured on that column — not on
    a caller-injected "now" that steers something else (the tick's cron evaluation)."""
    where = "event_type = ?"
    params: list = [event_type]
    if task_id is not None:
        where += " AND task_id = ?"
        params.append(task_id)
    row = connection.execute(
        "SELECT id, metadata, "
        "CAST(strftime('%s', 'now') AS INTEGER) - CAST(strftime('%s', occurred_at) AS INTEGER) "
        f"FROM events WHERE {where} ORDER BY id DESC LIMIT 1",
        params,
    ).fetchone()
    if row is None:
        return None
    age = row[2]
    return LatestEvent(id=int(row[0]), metadata=row[1], age_seconds=None if age is None else int(age))


def heartbeat_due(latest: LatestEvent | None, *, heartbeat_seconds: int) -> bool:
    """True when no row exists, its age is unknown, or it is at least ``heartbeat_seconds`` old."""
    return latest is None or latest.age_seconds is None or latest.age_seconds >= heartbeat_seconds


def _emission_reason(latest: LatestEvent | None, *, state: str, heartbeat_seconds: int) -> str | None:
    """``"first"`` / ``"change"`` / ``"heartbeat"`` — or ``None`` when the newest row already says
    exactly this and is younger than the heartbeat. A newest row whose metadata is not a JSON
    object carrying ``state`` FAILS OPEN to ``"change"``: when the recorded state cannot be read,
    recording again is the safe side."""
    if latest is None:
        return "first"
    try:
        stored = json.loads(latest.metadata) if latest.metadata else None
    except ValueError:
        stored = None
    if not isinstance(stored, dict) or stored.get("state") != state:
        return "change"
    if heartbeat_due(latest, heartbeat_seconds=heartbeat_seconds):
        return "heartbeat"
    return None


def add_event_on_change(
    database_path: Path,
    *,
    event_type: str,
    summary: str,
    state: str,
    heartbeat_seconds: int,
    extra: dict | None = None,
    task_id: int | None = None,
    actor_id: int | None = None,
) -> Event | None:
    """Record ``event_type`` only when ``state`` differs from the newest recorded state, or the
    newest row is at least ``heartbeat_seconds`` old, or none exists. Returns the written
    ``Event``, or ``None`` when nothing was written. The row's ``metadata`` is JSON:
    ``{**extra, "state": state, "emit": "first"|"change"|"heartbeat"}`` (reserved keys win).
    The read and the conditional write share one CONNECTION but not one transaction: ``connect()``
    keeps sqlite3's legacy isolation level, so the gate read runs in autocommit and the implicit
    transaction opens only at the INSERT. Two processes can therefore both see "no fresh row" and
    both write — one extra telemetry row, never a lost one, and no worse than the unconditional
    write this replaced. Tighten with ``BEGIN IMMEDIATE`` before the read if that ever matters."""
    with connect(database_path) as connection:
        latest = latest_event(connection, event_type=event_type, task_id=task_id)
        reason = _emission_reason(latest, state=state, heartbeat_seconds=heartbeat_seconds)
        if reason is None:
            return None
        payload = json.dumps({**(extra or {}), "state": state, "emit": reason}, sort_keys=True)
        cursor = connection.execute(
            """
            INSERT INTO events (event_type, task_id, actor_id, summary, metadata)
            VALUES (?, ?, ?, ?, ?)
            """,
            (event_type, task_id, actor_id, summary, payload),
        )
        event_id = int(cursor.lastrowid)
        row = connection.execute(
            "SELECT id, event_type, task_id, summary, occurred_at FROM events WHERE id = ?",
            (event_id,),
        ).fetchone()
    if row is None:
        raise RuntimeError(f"created event could not be loaded: {event_id}")
    return _row_to_event(row)
