"""an earlier fix / PA-3b C6 — the read log is not exempt from the rules.

``read_audit_entries`` records everything the owner ever looked at. Left unbounded it becomes the
largest sensitive corpus in the system, built by the very mechanism meant to protect it.

**Stated plainly so nobody discovers it during an incident: a leak older than the retention window
cannot be reconstructed from this log.** One month is a deliberate trade — a shorter memory of the
owner's own reading habits, bought against a shorter forensic reach. Revisit it as its own decision,
never by drift.

Pruning runs through the EXISTING retention machinery (``retention_policies`` +
``scan-retention-expiry``), not a bespoke deleter. A bespoke deleter would make this the one table
nobody's rules cover — and the point of a design decision is that this table is covered like any other.
"""
from __future__ import annotations

from pathlib import Path

from ledger_memory.db import connect
from ledger_memory.retention_policies import validate_condition, validate_selector

READ_AUDIT_TABLE = "read_audit_entries"
READ_AUDIT_POLICY_NAME = "read-audit-log"
DEFAULT_RETENTION_DAYS = 30


def ensure_read_audit_policy(database_path, *, retention_days: int = DEFAULT_RETENTION_DAYS) -> None:
    """Register the read-audit retention policy, once, as part of the schema baseline.

    **Inserted directly rather than through ``add_retention_policy``, and deliberately silent.** That
    helper emits ``retention.policy_added`` — an owner-attributed governance event. Emitting it on every
    ``init-db`` would claim that somebody decided this policy at that moment, when in fact it ships with
    the schema; it would also write a governance audit row into every database ledger ever creates. The
    validators are still called explicitly, so an invalid selector or condition fails here exactly as it
    would through the helper.

    Idempotent: ``retention_policies.name`` is UNIQUE, and the INSERT is ``OR IGNORE``, so two
    concurrent initializations cannot produce two rows and neither has to lose a read-then-write race.
    """
    database_path = Path(database_path)
    condition_value = f"{int(retention_days)}d"

    # Fail loudly on a bad selector/condition rather than writing an unenforceable policy.
    validate_selector(READ_AUDIT_TABLE)
    validate_condition("age", condition_value)

    with connect(database_path) as connection:
        connection.execute(
            """
            INSERT OR IGNORE INTO retention_policies
                (name, label, selector_tables, condition_type, condition_value, status)
            VALUES (?, ?, ?, 'age', ?, 'active')
            """,
            (
                READ_AUDIT_POLICY_NAME,
                "Read-audit log (that a read happened, never what it said)",
                READ_AUDIT_TABLE,
                condition_value,
            ),
        )


__all__ = ["DEFAULT_RETENTION_DAYS", "READ_AUDIT_POLICY_NAME", "READ_AUDIT_TABLE",
           "ensure_read_audit_policy"]
