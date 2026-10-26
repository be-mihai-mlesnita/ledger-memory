from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import sqlite3

from ledger_memory.db import connect
from ledger_memory.emission import emit_audit_and_event


@dataclass(frozen=True)
class Decision:
    id: int
    title: str
    summary: str
    rationale: str
    status: str
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class DecisionFact:
    fact_id: int
    relationship: str




def add_decision(
    database_path: Path,
    *,
    title: str,
    summary: str,
    rationale: str,
    status: str,
    fact_id: int | None = None,
    actor_id: int | None = None,
    reason: str | None = None,
) -> Decision:
    with connect(database_path) as connection:
        cursor = connection.execute(
            """
            INSERT INTO decisions (title, summary, rationale, status)
            VALUES (?, ?, ?, ?)
            """,
            (title, summary, rationale, status),
        )
        decision_id = int(cursor.lastrowid)

        if fact_id is not None:
            connection.execute(
                """
                INSERT INTO decision_facts (decision_id, fact_id, relationship)
                VALUES (?, ?, 'supports')
                """,
                (decision_id, fact_id),
            )


        metadata_dict: dict[str, object] = {"decision_id": decision_id}
        if reason is not None:
            metadata_dict["reason"] = reason
        emit_audit_and_event(
            connection,
            action_type="decision.created",
            target_type="decision",
            target_id=decision_id,
            actor_id=actor_id,
            reason=reason,
            event_type="decision.created",
            summary=f"Decision created: {title}",
            metadata=json.dumps(metadata_dict, sort_keys=True),
        )
        row = _fetch_decision_row(connection, decision_id)

    if row is None:
        raise RuntimeError(f"created decision could not be loaded: {decision_id}")
    return _row_to_decision(row)


DECISION_STATES = ("proposed", "accepted", "rejected", "superseded")


def update_decision_status(
    database_path: Path,
    *,
    decision_id: int,
    new_status: str,
    actor_id: int | None = None,
    reason: str,
) -> Decision:
    """Move a decision to a new lifecycle state, recording an immutable what/who/why/when event.

    The reason is REQUIRED — the reasoning between proposed and enacted is the part a future model needs;
    rejected/superseded paths are kept (a successor learns from the roads not taken).
    """
    if new_status not in DECISION_STATES:
        raise ValueError(f"invalid decision status {new_status!r}; valid: {', '.join(DECISION_STATES)}")
    if not reason or not reason.strip():
        raise ValueError("a non-empty reason is required for a decision status change")
    with connect(database_path) as connection:
        row = _fetch_decision_row(connection, decision_id)
        if row is None:
            raise ValueError(f"decision not found: {decision_id}")
        old_status = str(row["status"])
        if old_status == new_status:
            raise ValueError(f"decision {decision_id} is already {new_status!r}")
        connection.execute(
            "UPDATE decisions SET status = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (new_status, decision_id),
        )
        emit_audit_and_event(
            connection,
            action_type="decision.status_changed",
            target_type="decision",
            target_id=decision_id,
            actor_id=actor_id,
            reason=reason,
            event_type="decision.status_changed",
            task_id=None,
            summary=f"Decision {old_status}→{new_status} (reason in audit)",  # an earlier fix
            metadata=json.dumps(
                {"from": old_status, "to": new_status, "reason": reason}, sort_keys=True
            ),
        )
        updated = _fetch_decision_row(connection, decision_id)
    if updated is None:
        raise RuntimeError(f"updated decision could not be loaded: {decision_id}")
    return _row_to_decision(updated)


def list_decisions(database_path: Path) -> list[Decision]:
    with connect(database_path) as connection:
        rows = connection.execute(
            """
            SELECT id, title, summary, rationale, status, created_at, updated_at
            FROM decisions
            ORDER BY id
            """
        ).fetchall()
    return [_row_to_decision(row) for row in rows]


def get_decision(database_path: Path, decision_id: int) -> Decision | None:
    with connect(database_path) as connection:
        row = _fetch_decision_row(connection, decision_id)
    if row is None:
        return None
    return _row_to_decision(row)


def list_decision_facts(database_path: Path, decision_id: int) -> list[DecisionFact]:
    with connect(database_path) as connection:
        rows = connection.execute(
            """
            SELECT fact_id, relationship
            FROM decision_facts
            WHERE decision_id = ?
            ORDER BY id
            """,
            (decision_id,),
        ).fetchall()
    return [
        DecisionFact(fact_id=int(row["fact_id"]), relationship=str(row["relationship"]))
        for row in rows
    ]




def _fetch_decision_row(
    connection: sqlite3.Connection,
    decision_id: int,
) -> sqlite3.Row | None:
    return connection.execute(
        """
        SELECT id, title, summary, rationale, status, created_at, updated_at
        FROM decisions
        WHERE id = ?
        """,
        (decision_id,),
    ).fetchone()


def _row_to_decision(row: sqlite3.Row) -> Decision:
    return Decision(
        id=int(row["id"]),
        title=str(row["title"]),
        summary=str(row["summary"]),
        rationale=str(row["rationale"]),
        status=str(row["status"]),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )
