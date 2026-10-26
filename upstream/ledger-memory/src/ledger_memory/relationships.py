from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ledger_memory.db import connect
from ledger_memory.emission import emit_event_only


@dataclass(frozen=True)
class Relationship:
    id: int
    source_type: str
    source_id: int
    target_type: str
    target_id: int
    relationship: str
    summary: str | None
    created_at: str


@dataclass(frozen=True)
class RecordRelationship:
    relationship: Relationship
    direction: str


def add_relationship(
    database_path: Path,
    *,
    source_type: str,
    source_id: int,
    target_type: str,
    target_id: int,
    relationship: str,
    summary: str | None = None,
    actor_id: int | None = None,
) -> Relationship:
    with connect(database_path) as connection:
        cursor = connection.execute(
            """
            INSERT INTO relationships (
                source_type,
                source_id,
                target_type,
                target_id,
                relationship,
                summary
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (source_type, source_id, target_type, target_id, relationship, summary),
        )
        relationship_id = int(cursor.lastrowid)
        emit_event_only(
            connection,
            event_type="relationship.created",
            actor_id=actor_id,
            summary=f"Relationship created: {relationship_id}",
        )
        row = _fetch_relationship_row(connection, relationship_id)

    if row is None:
        raise RuntimeError(f"created relationship could not be loaded: {relationship_id}")
    return _row_to_relationship(row)


def list_relationships(
    database_path: Path,
    *,
    source_type: str | None = None,
    source_id: int | None = None,
    target_type: str | None = None,
    target_id: int | None = None,
    relationship: str | None = None,
) -> list[Relationship]:
    clauses: list[str] = []
    values: list[str | int] = []
    if source_type is not None:
        clauses.append("source_type = ?")
        values.append(source_type)
    if source_id is not None:
        clauses.append("source_id = ?")
        values.append(source_id)
    if target_type is not None:
        clauses.append("target_type = ?")
        values.append(target_type)
    if target_id is not None:
        clauses.append("target_id = ?")
        values.append(target_id)
    if relationship is not None:
        clauses.append("relationship = ?")
        values.append(relationship)

    where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    with connect(database_path) as connection:
        rows = connection.execute(
            f"""
            SELECT id, source_type, source_id, target_type, target_id,
                   relationship, summary, created_at
            FROM relationships
            {where_sql}
            ORDER BY id
            """,
            tuple(values),
        ).fetchall()
    return [_row_to_relationship(row) for row in rows]


def list_record_relationships(
    database_path: Path,
    *,
    record_type: str,
    record_id: int,
) -> list[RecordRelationship]:
    with connect(database_path) as connection:
        rows = connection.execute(
            """
            SELECT id, source_type, source_id, target_type, target_id,
                   relationship, summary, created_at,
                   CASE
                       WHEN source_type = ? AND source_id = ? THEN 'outgoing'
                       ELSE 'incoming'
                   END AS direction
            FROM relationships
            WHERE (source_type = ? AND source_id = ?)
               OR (target_type = ? AND target_id = ?)
            ORDER BY id
            """,
            (record_type, record_id, record_type, record_id, record_type, record_id),
        ).fetchall()
    return [
        RecordRelationship(
            relationship=_row_to_relationship(row),
            direction=str(row["direction"]),
        )
        for row in rows
    ]


def _fetch_relationship_row(connection, relationship_id: int):
    return connection.execute(
        """
        SELECT id, source_type, source_id, target_type, target_id,
               relationship, summary, created_at
        FROM relationships
        WHERE id = ?
        """,
        (relationship_id,),
    ).fetchone()


def _row_to_relationship(row) -> Relationship:
    return Relationship(
        id=int(row["id"]),
        source_type=str(row["source_type"]),
        source_id=int(row["source_id"]),
        target_type=str(row["target_type"]),
        target_id=int(row["target_id"]),
        relationship=str(row["relationship"]),
        summary=row["summary"],
        created_at=str(row["created_at"]),
    )
