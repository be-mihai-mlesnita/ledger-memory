from __future__ import annotations

"""Owner-gated record expiration — the policy/milestone model + setters.

Distinct from `retention.py`, which AUTOMATICALLY prunes routine watchdog noise on a time
window. THIS module is the owner-driven, APPROVAL-GATED expiration of arbitrary operational logs: the owner
attaches an expiry CONDITION to a selector; when it fires (0814b) ledger asks for deletion approval; only on
approve does it back up + delete + certify (0814c). The DB is NEVER cleaned without explicit owner approval.
This module is the durable records + validation only — no detection, no deletion.

SAFETY: a policy selector may target ONLY operational-log tables (TELEMETRY_TABLES). Durable knowledge
(facts/decisions/context_packages/…) can NEVER be targeted — add_retention_policy rejects a non-telemetry
table at creation, and the 0814c executor re-asserts the same guard (defense in depth).
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import re
import sqlite3

from ledger_memory.db import connect
from ledger_memory.emission import emit_audit_and_event


# The ONLY tables a retention policy may target — operational logs that churn and lose value. Durable
# knowledge is deliberately absent: it can never be scheduled for deletion.
TELEMETRY_TABLES: frozenset[str] = frozenset(
    {
        "events",
        "heartbeats",
        "diagnostics",
        "task_runs",
        "dispatch_attempts",
        "audit_entries",
        "recovery_attempts",
        # the read-audit log. A separate table from audit_entries precisely so a
        # 30-day read retention cannot delete the governance audit trail.
        "read_audit_entries",
    }
)

# Per-telemetry-table "age" timestamp column (the row's birth time) — used by the age condition (0814b) and
# the scoped delete (0814c). Every telemetry table has one; recovery_attempts uses started_at (no created_at).
TELEMETRY_TIMESTAMP_COLUMN: dict[str, str] = {
    "events": "created_at",
    "read_audit_entries": "created_at",
    "heartbeats": "created_at",
    "diagnostics": "created_at",
    "task_runs": "created_at",
    "dispatch_attempts": "created_at",
    "audit_entries": "created_at",
    "recovery_attempts": "started_at",
}

CONDITION_TYPES: frozenset[str] = frozenset({"date", "milestone", "age"})

# age-spec unit → days. m/y are FIXED approximations (no calendar drift): deliberate + documented.
_AGE_UNIT_DAYS = {"d": 1, "w": 7, "m": 30, "y": 365}
_AGE_RE = re.compile(r"^\s*(\d+)\s*([dwmy])\s*$")


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RetentionPolicy:
    id: int
    name: str
    label: str
    selector_tables: str
    selector_scope: str | None
    condition_type: str
    condition_value: str
    status: str
    approval_id: int | None
    last_notified_at: str | None
    created_by_actor_id: int | None
    created_at: str
    updated_at: str

    def tables(self) -> list[str]:
        """The selector as a clean list of table names."""
        return [t.strip() for t in self.selector_tables.split(",") if t.strip()]


@dataclass(frozen=True)
class Milestone:
    id: int
    name: str
    label: str | None
    status: str
    done_at: str | None
    created_by_actor_id: int | None
    created_at: str
    updated_at: str


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def validate_selector(selector_tables: str) -> list[str]:
    """Split/trim a comma-list of table names and reject empty or any non-telemetry table.

    This is the knowledge-deletion guard: a selector can only ever name operational-log tables, so a policy
    can never be pointed at facts/decisions/context_packages/etc. Raises ValueError on a bad selector.
    """
    tables = [t.strip() for t in (selector_tables or "").split(",") if t.strip()]
    if not tables:
        raise ValueError("selector must name at least one telemetry table")
    bad = [t for t in tables if t not in TELEMETRY_TABLES]
    if bad:
        allowed = ", ".join(sorted(TELEMETRY_TABLES))
        raise ValueError(
            f"selector may target only telemetry tables (got {bad}); allowed: {allowed}. "
            "Durable knowledge can never be a retention target."
        )
    return tables


def parse_age(spec: str) -> int:
    """An age spec ('30d','4w','6m','1y') → a number of days (d=1, w=7, m=30, y=365). Raises ValueError."""
    match = _AGE_RE.match(spec or "")
    if not match:
        raise ValueError(f"bad age spec {spec!r}; expected <int><d|w|m|y>, e.g. '180d' or '6m'")
    count, unit = int(match.group(1)), match.group(2)
    return count * _AGE_UNIT_DAYS[unit]


# moved here from `retention_proposals` so EVERY date condition — standing policies too —
# is held to the stored-timestamp alphabet; the executor's `_candidates` compares the stored TEXT.
# The cutoff must be a spelling BOTH comparators agree on, and it must SORT chronologically against the stored
# rows. `validate_condition("date", …)` accepts every ISO-8601 form Python's `datetime.fromisoformat` knows
# (`20990101`, `2099-W01-1`, `…T00:00:00+0000`, `…T000000`), but the rails never parse it with Python again:
#   * the elapsed check is SQLite's `julianday()` — NULL for those spellings, and NULL is not "in the future",
#     so an unguarded check fails OPEN (`_check_cutoff` therefore treats NULL as a refusal);
#   * the executor's `_candidates` compares the STORED TEXT lexicographically (`WHERE {column} < ?`).
# Every telemetry timestamp column is `TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP` and no writer ever supplies its
# own value (verified 2026-09-03 across events/heartbeats/diagnostics/task_runs/dispatch_attempts/audit_entries/
# recovery_attempts/read_audit_entries), so every row reads `'YYYY-MM-DD HH:MM:SS'` — SPACE-separated, UTC, no
# offset. That is the alphabet the cutoff has to live in:
#   * `'20990101'` sorts BELOW every stored row (`'2026-…' < '2099…'` is False, `'2026-…' < '20990101'` is True);
#   * a `T` separator is worse because it looks right: byte 10 is `' '` (0x20) in a stored row and `'T'` (0x54)
#     in the cutoff, so EVERY row on the cutoff's own date sorts as "older". A proposal filed
#     `--before <today>T00:00:00` is shown 0 rows and then deletes every row written that day, including rows
#     born AFTER the owner was asked — the approved scope grows behind the grant;
#   * a `Z`/±HH:MM suffix is the same trap plus a timezone the text compare silently ignores.
# So only the space-separated, offset-free family is accepted: it is the exact shape SQLite stores, `julianday()`
# parses it, and it sorts chronologically against the rows. Anything else is refused at filing with nothing
# written. Pinned by `test_future_cutoff_is_refused` (proposals) and an earlier fix's
# `test_a_t_separated_date_condition_is_refused_at_creation` (standing policies).
CUTOFF_FORMS = "YYYY-MM-DD or 'YYYY-MM-DD HH:MM[:SS]' (space-separated, no timezone suffix)"
_CUTOFF_FORM = re.compile(r"^\d{4}-\d{2}-\d{2}(?: \d{2}:\d{2}(?::\d{2})?)?$")


def validate_condition(condition_type: str, condition_value: str) -> None:
    """Reject a malformed condition for its type. Raises ValueError."""
    if condition_type not in CONDITION_TYPES:
        raise ValueError(f"condition_type must be one of {sorted(CONDITION_TYPES)} (got {condition_type!r})")
    value = (condition_value or "").strip()
    if not value:
        raise ValueError("condition_value must not be empty")
    if condition_type == "age":
        parse_age(value)  # raises on junk
    elif condition_type == "date":
        _parse_iso_date(value)  # raises on junk
        if not _CUTOFF_FORM.match(value):
            raise ValueError(
                f"cutoff {value!r} is a spelling the rails cannot compare against the stored rows — use "
                f"{CUTOFF_FORMS}. The rows carry SQLite's CURRENT_TIMESTAMP text ('YYYY-MM-DD HH:MM:SS') and the "
                "executor compares that text as-is, so a T separator, a Z/+HH:MM suffix or a compact/week form "
                "mis-sorts and would delete rows newer than the cutoff"
            )
    # 'milestone': any non-empty name is valid — the milestone need not exist yet.


def _parse_iso_date(value: str) -> datetime:
    """Parse an ISO date/datetime ('2026-12-01' or '2026-12-01T09:00:00'). Raises ValueError."""
    try:
        return datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"bad ISO date {value!r}; expected e.g. '2026-12-01'") from exc


# ---------------------------------------------------------------------------
# Retention policies
# ---------------------------------------------------------------------------


def add_retention_policy(
    database_path: Path,
    *,
    name: str,
    label: str,
    selector_tables: str,
    condition_type: str,
    condition_value: str,
    actor_id: int,
    selector_scope: str | None = None,
) -> RetentionPolicy:
    """Record a new (status='active') retention policy. Validates the selector + condition, then inserts and
    emits retention.policy_added. Raises ValueError on a bad selector/condition (nothing is written)."""
    tables = validate_selector(selector_tables)
    validate_condition(condition_type, condition_value)
    clean_selector = ",".join(tables)
    try:
        with connect(database_path) as connection:
            cursor = connection.execute(
                """
                INSERT INTO retention_policies
                    (name, label, selector_tables, selector_scope, condition_type, condition_value,
                     created_by_actor_id)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (name, label, clean_selector, selector_scope, condition_type, condition_value.strip(), actor_id),
            )
            policy_id = int(cursor.lastrowid)
            emit_audit_and_event(
                connection,
                action_type="retention.policy_added",
                target_type="retention_policy",
                target_id=policy_id,
                actor_id=actor_id,
                reason=None,
                event_type="retention.policy_added",
                summary=f"retention policy {name} added ({condition_type})",
            )
            row = connection.execute(
                "SELECT * FROM retention_policies WHERE id = ?", (policy_id,)
            ).fetchone()
    except sqlite3.IntegrityError as exc:
        # The realistic case is the UNIQUE(name) constraint; the INSERT fails before emit, so the
        # transaction rolls back clean (nothing written). Surface it as a ValueError like a bad selector.
        raise ValueError(f"could not add policy {name!r} — name may already be in use ({exc})") from exc
    return _row_to_policy(row)


def list_retention_policies(
    database_path: Path, *, status: str | None = None
) -> list[RetentionPolicy]:
    """All policies (optionally filtered by status), newest first."""
    with connect(database_path) as connection:
        if status is None:
            rows = connection.execute(
                "SELECT * FROM retention_policies ORDER BY id DESC"
            ).fetchall()
        else:
            rows = connection.execute(
                "SELECT * FROM retention_policies WHERE status = ? ORDER BY id DESC", (status,)
            ).fetchall()
    return [_row_to_policy(row) for row in rows]


def get_retention_policy(
    database_path: Path, *, policy_id: int | None = None, name: str | None = None
) -> RetentionPolicy | None:
    """Look up one policy by id or name (id wins if both given)."""
    if policy_id is None and name is None:
        raise ValueError("get_retention_policy needs policy_id or name")
    with connect(database_path) as connection:
        if policy_id is not None:
            row = connection.execute(
                "SELECT * FROM retention_policies WHERE id = ?", (policy_id,)
            ).fetchone()
        else:
            row = connection.execute(
                "SELECT * FROM retention_policies WHERE name = ?", (name,)
            ).fetchone()
    return None if row is None else _row_to_policy(row)


# ---------------------------------------------------------------------------
# Milestones
# ---------------------------------------------------------------------------


def set_milestone(
    database_path: Path,
    *,
    name: str,
    status: str,
    actor_id: int,
    label: str | None = None,
) -> Milestone:
    """UPSERT a named milestone to status 'pending'|'done' (stamping done_at on 'done'); emit
    retention.milestone_set. A 'milestone' retention policy fires when its named milestone is 'done'."""
    if status not in {"pending", "done"}:
        raise ValueError(f"milestone status must be 'pending' or 'done' (got {status!r})")
    done_at = datetime.now(timezone.utc).isoformat() if status == "done" else None
    with connect(database_path) as connection:
        connection.execute(
            """
            INSERT INTO milestones (name, label, status, done_at, created_by_actor_id)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET
                label      = COALESCE(excluded.label, milestones.label),
                status     = excluded.status,
                done_at    = excluded.done_at,
                updated_at = CURRENT_TIMESTAMP
            """,
            (name, label, status, done_at, actor_id),
        )
        emit_audit_and_event(
            connection,
            action_type="retention.milestone_set",
            target_type="milestone",
            target_id=None,
            actor_id=actor_id,
            reason=None,
            event_type="retention.milestone_set",
            summary=f"milestone {name} set {status}",
        )
        row = connection.execute("SELECT * FROM milestones WHERE name = ?", (name,)).fetchone()
    return _row_to_milestone(row)


def get_milestone(database_path: Path, *, name: str) -> Milestone | None:
    with connect(database_path) as connection:
        row = connection.execute("SELECT * FROM milestones WHERE name = ?", (name,)).fetchone()
    return None if row is None else _row_to_milestone(row)


def list_milestones(database_path: Path) -> list[Milestone]:
    with connect(database_path) as connection:
        rows = connection.execute("SELECT * FROM milestones ORDER BY name").fetchall()
    return [_row_to_milestone(row) for row in rows]


# ---------------------------------------------------------------------------
# Row mappers
# ---------------------------------------------------------------------------


def _row_to_policy(row) -> RetentionPolicy:
    return RetentionPolicy(
        id=int(row["id"]),
        name=str(row["name"]),
        label=str(row["label"]),
        selector_tables=str(row["selector_tables"]),
        selector_scope=row["selector_scope"],
        condition_type=str(row["condition_type"]),
        condition_value=str(row["condition_value"]),
        status=str(row["status"]),
        approval_id=None if row["approval_id"] is None else int(row["approval_id"]),
        last_notified_at=row["last_notified_at"],
        created_by_actor_id=None if row["created_by_actor_id"] is None else int(row["created_by_actor_id"]),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


def _row_to_milestone(row) -> Milestone:
    return Milestone(
        id=int(row["id"]),
        name=str(row["name"]),
        label=row["label"],
        status=str(row["status"]),
        done_at=row["done_at"],
        created_by_actor_id=None if row["created_by_actor_id"] is None else int(row["created_by_actor_id"]),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )
