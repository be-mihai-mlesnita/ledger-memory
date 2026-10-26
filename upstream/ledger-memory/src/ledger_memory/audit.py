from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ledger_memory.db import connect


@dataclass(frozen=True)
class AuditEntry:
    id: int
    actor_id: int | None
    action_type: str
    target_type: str
    target_id: int | None
    reason: str | None
    changed_fields: str | None
    occurred_at: str
    event_id: int | None
    created_at: str


def add_audit_entry(
    database_path: Path,
    *,
    action_type: str,
    target_type: str,
    actor_id: int | None = None,
    target_id: int | None = None,
    reason: str | None = None,
    changed_fields: str | None = None,
    event_id: int | None = None,
) -> AuditEntry:
    with connect(database_path) as connection:
        cursor = connection.execute(
            """
            INSERT INTO audit_entries (
                actor_id,
                action_type,
                target_type,
                target_id,
                reason,
                changed_fields,
                event_id
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                actor_id,
                action_type,
                target_type,
                target_id,
                reason,
                changed_fields,
                event_id,
            ),
        )
        audit_entry_id = int(cursor.lastrowid)
        row = _fetch_audit_entry_row(connection, audit_entry_id)

    if row is None:
        raise RuntimeError(f"created audit entry could not be loaded: {audit_entry_id}")
    return _row_to_audit_entry(row)


def list_audit_entries(
    database_path: Path,
    *,
    actor_id: int | None = None,
    target_type: str | None = None,
    target_id: int | None = None,
    action_type: str | None = None,
) -> list[AuditEntry]:
    clauses = []
    values: list[object] = []
    if actor_id is not None:
        clauses.append("actor_id = ?")
        values.append(actor_id)
    if target_type is not None:
        clauses.append("target_type = ?")
        values.append(target_type)
    if target_id is not None:
        clauses.append("target_id = ?")
        values.append(target_id)
    if action_type is not None:
        clauses.append("action_type = ?")
        values.append(action_type)

    where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    with connect(database_path) as connection:
        rows = connection.execute(
            f"""
            SELECT id, actor_id, action_type, target_type, target_id,
                   reason, changed_fields, occurred_at, event_id, created_at
            FROM audit_entries
            {where_sql}
            ORDER BY id
            """,
            tuple(values),
        ).fetchall()
    return [_row_to_audit_entry(row) for row in rows]


def get_audit_entry(database_path: Path, audit_entry_id: int) -> AuditEntry | None:
    with connect(database_path) as connection:
        row = _fetch_audit_entry_row(connection, audit_entry_id)
    if row is None:
        return None
    return _row_to_audit_entry(row)


def _fetch_audit_entry_row(connection, audit_entry_id: int):
    return connection.execute(
        """
        SELECT id, actor_id, action_type, target_type, target_id,
               reason, changed_fields, occurred_at, event_id, created_at
        FROM audit_entries
        WHERE id = ?
        """,
        (audit_entry_id,),
    ).fetchone()


def _row_to_audit_entry(row) -> AuditEntry:
    actor_id = row["actor_id"]
    target_id = row["target_id"]
    event_id = row["event_id"]
    return AuditEntry(
        id=int(row["id"]),
        actor_id=None if actor_id is None else int(actor_id),
        action_type=str(row["action_type"]),
        target_type=str(row["target_type"]),
        target_id=None if target_id is None else int(target_id),
        reason=row["reason"],
        changed_fields=row["changed_fields"],
        occurred_at=str(row["occurred_at"]),
        event_id=None if event_id is None else int(event_id),
        created_at=str(row["created_at"]),
    )
