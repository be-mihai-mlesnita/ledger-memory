from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Union

from ledger_memory.actors import get_actor_by_name
from ledger_memory.instruction_shape import is_instruction_shaped
from ledger_memory.authenticated_context import is_authentic
from ledger_memory.client_auth import verify_device_token
from ledger_memory.db import connect
from ledger_memory.emission import emit_audit_and_event
from ledger_memory.fact_authority import AuthorityAssignmentError, validate_authority_assignment
from ledger_memory.presence import is_present


class V1RecordRefused(Exception):
    """Raised when a v1 record write is refused due to forbidden fields or invalid context."""
    pass


@dataclass(frozen=True)
class V1RecordResult:
    record_id: int
    quarantined: bool
    authority_class: str


_FORBIDDEN_FIELDS = frozenset({
    "origin", "source_db", "source_record_id", "conversation_title",
    "source_file", "known_at", "authority_class"
})


def v1_record(
    context,
    *,
    statement: str,
    database_path: Optional[Union[str, Path]] = None,
    content_author: Optional[str] = None,
    **forbidden
) -> V1RecordResult:
    # Refuse forbidden fields
    if forbidden:
        forbidden_keys = set(forbidden.keys()) & _FORBIDDEN_FIELDS
        if forbidden_keys:
            raise V1RecordRefused(f"forbidden fields: {', '.join(sorted(forbidden_keys))}")

    # Refuse unauthenticated context
    if not is_authentic(context):
        raise V1RecordRefused("unauthenticated context")

    # Use provided database_path or fall back to context's database_path
    db_path = Path(database_path) if database_path is not None else Path(context.database_path)

    # Verify device token and get device
    device = verify_device_token(db_path, context.token)
    if device is None:
        raise V1RecordRefused("invalid or revoked device token")

    # Determine fresh presence from the database
    with connect(db_path) as conn:
        # Get current timestamp from database (SQLite format)
        now_row = conn.execute("SELECT CURRENT_TIMESTAMP").fetchone()
        now_str = now_row[0] if now_row else datetime.now(timezone.utc).isoformat()

        # Get device's last_activity_at from client_devices
        device_row = conn.execute(
            "SELECT last_activity_at FROM client_devices WHERE id = ?",
            (device.id,)
        ).fetchone()
        if device_row is None:
            raise V1RecordRefused("device not found")
        last_activity_at = device_row[0]

        # Check fresh presence
        fresh_presence = is_present(last_activity_at, now_str) if last_activity_at else False

        # Determine quarantine and authority_class based on content_author
        # Rule: only content_author="owner" with fresh_presence lands un-quarantined
        if content_author == "owner" and fresh_presence:
            quarantined = False
            authority_class = "actor_asserted"
        else:
            quarantined = True
            authority_class = "model_inferred"

        # an earlier fix / A2: this door was setting `authority_class = "actor_asserted"` with NO
        # `asserted_by_actor_id` at all — a null assertor on an asserted row, exactly what A2 forbids.
        # This does not change WHEN a write lands as `actor_asserted` (that policy — content_author ==
        # "owner" under fresh device presence — is an earlier fix's, unchanged here); it only closes
        # the gap between "we decided this is asserted" and "we recorded WHO asserted it". The assertor
        # is the well-known 'owner' actor (a design decision, item 1 — the single-owner system's one
        # verified identity), looked up fresh rather than trusted from any caller-suppliable field.
        owner_actor_id = None
        if authority_class == "actor_asserted":
            owner_actor = get_actor_by_name(db_path, "owner")
            owner_actor_id = owner_actor.id if owner_actor is not None else None
        try:
            validate_authority_assignment(authority_class, owner_actor_id, connection=conn)
        except AuthorityAssignmentError as exc:
            # A corrupted install (no registered 'owner' actor) must refuse the write, not silently
            # store an assertion with no verifiable assertor.
            raise V1RecordRefused(f"authority assignment invalid: {exc}") from exc

        # Server-set provenance
        origin = f"api:{device.id}"

        # Insert the fact
        cursor = conn.execute(
            """
            INSERT INTO facts (statement, fact_type, authority_class, asserted_by_actor_id,
                              sensitivity_tier, status, quarantined, content_author,
                              submitting_principal_id, origin, source_record_id, instruction_shape)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                statement,
                "fact",  # fact_type
                authority_class,
                owner_actor_id,
                1,  # sensitivity_tier (restrictive default)
                "active",  # status
                1 if quarantined else 0,  # quarantined
                content_author,
                device.id,  # submitting_principal_id
                origin,
                None,  # source_record_id
                # an earlier fix / F1: screen for instruction shape at write. This FLAGS and never refuses —
                # quarantine gates entry, this gates interpretation, and a false positive must cost a
                # tag rather than a memory. The statement is stored verbatim either way.
                1 if is_instruction_shaped(statement) else 0,
            )
        )
        record_id = cursor.lastrowid

        # Audit the write
        emit_audit_and_event(
            conn,
            action_type="fact.created",
            target_type="fact",
            target_id=record_id,
            actor_id=None,
            reason=None,
            event_type="fact.created",
            summary=f"fact created via v1_record: {statement[:100]}",
        )

        conn.commit()

    return V1RecordResult(
        record_id=record_id,
        quarantined=quarantined,
        authority_class=authority_class
    )


def screen_verdict(*, quarantined: bool, requested: str) -> str:
    """
    One-directional screener: can hold/demote but never promote a quarantined row.
    Promotion is owner-only and not on this API.
    """
    if requested == "hold":
        return "held"
    elif requested == "release":
        if quarantined:
            return "held"  # Never promote a quarantined row
        else:
            return "released"
    else:
        return "held"
