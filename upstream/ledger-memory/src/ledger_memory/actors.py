from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import sqlite3

from ledger_memory.db import connect
from ledger_memory.emission import emit_audit_and_event


ALLOWED_ACTOR_TYPES = ("human", "model", "script", "system", "agent")


@dataclass(frozen=True)
class Actor:
    id: int
    name: str
    actor_type: str
    external_ref: str | None
    notes: str | None
    created_at: str


def add_actor(
    database_path: Path,
    *,
    name: str,
    actor_type: str,
    external_ref: str | None = None,
    notes: str | None = None,
    actor_id: int | None = None,
) -> Actor:
    if actor_type not in ALLOWED_ACTOR_TYPES:
        raise ValueError(f"invalid actor_type: {actor_type}")

    with connect(database_path) as connection:
        cursor = connection.execute(
            """
            INSERT INTO actors (name, actor_type, external_ref, notes)
            VALUES (?, ?, ?, ?)
            """,
            (name, actor_type, external_ref, notes),
        )
        new_actor_id = int(cursor.lastrowid)
        emit_audit_and_event(
            connection,
            action_type="actor.created",
            target_type="actor",
            target_id=new_actor_id,
            actor_id=actor_id,
            event_type="actor.created",
            summary=f"Actor created: {name}",
        )
        row = _fetch_actor_row(connection, new_actor_id)

    if row is None:
        raise RuntimeError(f"created actor could not be loaded: {new_actor_id}")
    return _row_to_actor(row)


def list_actors(database_path: Path) -> list[Actor]:
    with connect(database_path) as connection:
        rows = connection.execute(
            """
            SELECT id, name, actor_type, external_ref, notes, created_at
            FROM actors
            ORDER BY id
            """
        ).fetchall()
    return [_row_to_actor(row) for row in rows]


def get_actor(database_path: Path, actor_id: int) -> Actor | None:
    with connect(database_path) as connection:
        row = _fetch_actor_row(connection, actor_id)
    if row is None:
        return None
    return _row_to_actor(row)


SYSTEM_ACTOR_NAME = "ledger-system"


class SystemActorUnavailableError(RuntimeError):
    """Raised when the ``ledger-system`` actor is missing, or registered under a non-'system' actor_type."""


def require_system_actor_id(database_path: Path) -> int:
    """Resolve the ``ledger-system`` actor's id BY NAME — never a hardcoded literal (review C2, 2026-07-18
    codebase audit part 2).

    an earlier fix's binding contract already requires the tick to resolve ``ledger-system`` by registered name
    and fail closed if absent (see ``tick.run_tick``). A merged/restored/noncanonical database can
    legitimately assign ``ledger-system`` an id other than the fresh-``init-db`` default of 2 — a caller that
    hardcodes ``= 2`` would then attribute a record to the wrong actor, or to a nonexistent one, on such a
    database. This is the one chokepoint retention.py / retention_expiry.py resolve through instead.

    Fails closed (raises ``SystemActorUnavailableError``) if the actor is absent or its ``actor_type`` is
    not ``'system'`` — never silently falls back to a guessed id.
    """
    actor = get_actor_by_name(database_path, SYSTEM_ACTOR_NAME)
    if actor is None:
        raise SystemActorUnavailableError(
            f"'{SYSTEM_ACTOR_NAME}' actor not registered; run: "
            "run `ledger init` to re-seed it"
        )
    if actor.actor_type != "system":
        raise SystemActorUnavailableError(
            f"'{SYSTEM_ACTOR_NAME}' actor (id={actor.id}) has actor_type={actor.actor_type!r}, expected "
            f"'system' — refusing to attribute a system-originated record to a misregistered actor"
        )
    return actor.id


def get_actor_by_name(database_path: Path, name: str) -> Actor | None:
    with connect(database_path) as connection:
        row = connection.execute(
            """
            SELECT id, name, actor_type, external_ref, notes, created_at
            FROM actors
            WHERE name = ?
            LIMIT 1
            """,
            (name,),
        ).fetchone()
    if row is None:
        return None
    return _row_to_actor(row)


def _fetch_actor_row(
    connection: sqlite3.Connection,
    actor_id: int,
) -> sqlite3.Row | None:
    return connection.execute(
        """
        SELECT id, name, actor_type, external_ref, notes, created_at
        FROM actors
        WHERE id = ?
        """,
        (actor_id,),
    ).fetchone()


def _row_to_actor(row: sqlite3.Row) -> Actor:
    return Actor(
        id=int(row["id"]),
        name=str(row["name"]),
        actor_type=str(row["actor_type"]),
        external_ref=row["external_ref"],
        notes=row["notes"],
        created_at=str(row["created_at"]),
    )
