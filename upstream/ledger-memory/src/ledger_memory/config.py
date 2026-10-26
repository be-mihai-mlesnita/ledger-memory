from __future__ import annotations

"""Recorded key-value config store for an earlier fix.

Implements get_config, set_config, and list_config backed by the ``config``
table (the baseline schema's). Every set_config call emits audit-and-event
(config.set) in the same transaction so that config changes are attributable,
auditable, and round-tripped by export/import.
"""

from dataclasses import dataclass
from pathlib import Path

from ledger_memory.db import connect, connect_readonly
from ledger_memory.emission import emit_audit_and_event


# ---------------------------------------------------------------------------
# Dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ConfigEntry:
    id: int
    key: str
    value: str
    actor_id: int | None
    reason: str | None
    created_at: str
    updated_at: str


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def get_config(
    database_path: Path,
    key: str,
    default: str | None = None,
) -> str | None:
    """Return the stored value for *key*, or *default* if absent.

    Read-only: no emission.
    """
    with connect_readonly(database_path) as connection:  # a pure read — must not flip journal_mode
        row = connection.execute(
            "SELECT value FROM config WHERE key = ?",
            (key,),
        ).fetchone()
    if row is None:
        return default
    return str(row["value"])


def set_config(
    database_path: Path,
    *,
    key: str,
    value: str,
    actor_id: int,
    reason: str | None = None,
) -> ConfigEntry:
    """UPSERT *key* → *value* and emit config.set audit-and-event.

    Returns the resulting ConfigEntry (re-SELECTed after the upsert).
    """
    with connect(database_path) as connection:
        connection.execute(
            """
            INSERT INTO config (key, value, actor_id, reason)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                value      = excluded.value,
                actor_id   = excluded.actor_id,
                reason     = excluded.reason,
                updated_at = CURRENT_TIMESTAMP
            """,
            (key, value, actor_id, reason),
        )
        emit_audit_and_event(
            connection,
            action_type="config.set",
            target_type="config",
            target_id=None,
            actor_id=actor_id,
            reason=reason,
            event_type="config.set",
            summary=f"config {key} set",
        )
        row = connection.execute(
            "SELECT id, key, value, actor_id, reason, created_at, updated_at FROM config WHERE key = ?",
            (key,),
        ).fetchone()

    if row is None:
        raise RuntimeError(f"config entry could not be loaded after upsert: {key}")
    return _row_to_entry(row)


def list_config(database_path: Path) -> list[ConfigEntry]:
    """Return all config rows ordered by key."""
    with connect_readonly(database_path) as connection:  # a pure read
        rows = connection.execute(
            "SELECT id, key, value, actor_id, reason, created_at, updated_at FROM config ORDER BY key",
        ).fetchall()
    return [_row_to_entry(row) for row in rows]


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _row_to_entry(row) -> ConfigEntry:
    return ConfigEntry(
        id=int(row["id"]),
        key=str(row["key"]),
        value=str(row["value"]),
        actor_id=None if row["actor_id"] is None else int(row["actor_id"]),
        reason=row["reason"],
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )
