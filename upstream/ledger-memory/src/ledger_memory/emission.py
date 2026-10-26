from __future__ import annotations

"""Shared in-transaction emit helpers for an earlier fix.

Both functions accept an already-open sqlite3.Connection and execute within
the caller's transaction. They must not open a ``with connection`` block; the
caller controls the transaction boundary so that a failed emission rolls back
the enclosing write.
"""

import sqlite3


def emit_audit_and_event(
    connection: sqlite3.Connection,
    *,
    action_type: str,
    target_type: str,
    target_id: int | None,
    actor_id: int | None,
    reason: str | None = None,
    event_type: str,
    task_id: int | None = None,
    task_run_id: int | None = None,
    worker_id: int | None = None,
    summary: str | None = None,
    metadata: str | None = None,
) -> tuple[int, int]:
    """Insert one event and one audit entry linked to that event.

    Returns ``(audit_entry_id, event_id)``.  Both rows are written inside the
    caller's already-open transaction; no new transaction is started here.
    """
    if summary is None:
        summary = f"{action_type}: {target_type}:{target_id}"

    event_cursor = connection.execute(
        """
        INSERT INTO events (event_type, task_id, task_run_id, worker_id,
                            actor_id, summary, metadata)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (event_type, task_id, task_run_id, worker_id, actor_id, summary, metadata),
    )
    event_id = int(event_cursor.lastrowid)

    audit_cursor = connection.execute(
        """
        INSERT INTO audit_entries (actor_id, action_type, target_type, target_id,
                                   reason, event_id)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (actor_id, action_type, target_type, target_id, reason, event_id),
    )
    audit_entry_id = int(audit_cursor.lastrowid)

    return audit_entry_id, event_id


def emit_event_only(
    connection: sqlite3.Connection,
    *,
    event_type: str,
    actor_id: int | None = None,
    task_id: int | None = None,
    task_run_id: int | None = None,
    worker_id: int | None = None,
    summary: str | None = None,
    metadata: str | None = None,
) -> int:
    """Insert one event; emit no audit entry.

    Returns ``event_id``.  Executes inside the caller's already-open
    transaction; no new transaction is started here.
    """
    if summary is None:
        summary = event_type

    cursor = connection.execute(
        """
        INSERT INTO events (event_type, task_id, task_run_id, worker_id,
                            actor_id, summary, metadata)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (event_type, task_id, task_run_id, worker_id, actor_id, summary, metadata),
    )
    return int(cursor.lastrowid)
