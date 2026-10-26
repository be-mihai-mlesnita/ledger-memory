from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ledger_memory.db import connect_readonly  # the LIKE search lane is a pure read — no journal_mode mutation
from ledger_memory.facts import CURRENT_FACT_SQL
from ledger_memory.textfold import fold_for_match


@dataclass(frozen=True)
class SearchResult:
    record_type: str
    record_id: int
    text: str


def _escape_like(value: str) -> str:
    r"""Escape LIKE metacharacters (``%``, ``_``, and the ``\`` escape char) so a query is matched LITERALLY — paired
    with ``ESCAPE '\'`` on each LIKE clause. Without this, searching ``%`` or ``_`` matched almost
    every row."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def search_records(database_path: Path, query: str) -> list[SearchResult]:
    pattern = f"%{_escape_like(query)}%"
    # C4: the fact predicate below folds both sides (`ledger_fold`, backed by
    # `textfold.fold_for_match`) so it is diacritic- and case-insensitive for Romanian — measured
    # defect: `'%inimă%'` missed `inima`, `'%MĂ%'` missed `Mă`. Escaping is applied AFTER folding, to
    # the folded query. `search_records` owns this connection (opened below), so it can register the
    # deterministic SQL function on it directly — `unified_search.py` (PROTECTED) calls this function
    # but never touches the connection itself.
    folded_pattern = f"%{_escape_like(fold_for_match(query))}%"
    results: list[SearchResult] = []

    with connect_readonly(database_path) as connection:
        connection.create_function("ledger_fold", 1, fold_for_match, deterministic=True)
        event_rows = connection.execute(
            """
            SELECT id, event_type, summary
            FROM events
            WHERE event_type LIKE ? ESCAPE '\\' OR summary LIKE ? ESCAPE '\\'
            ORDER BY id
            """,
            (pattern, pattern),
        ).fetchall()
        fact_rows = connection.execute(
            f"""
            SELECT id, fact_type, statement
            FROM facts
            WHERE {CURRENT_FACT_SQL} AND (ledger_fold(fact_type) LIKE ? ESCAPE '\\'
                                       OR ledger_fold(statement) LIKE ? ESCAPE '\\')
            ORDER BY id
            """,
            (folded_pattern, folded_pattern),
        ).fetchall()  # the LIKE lane must match the FTS source policy
        # (_fact_rows uses the SAME CURRENT_FACT_SQL predicate) — else rejected/proposed/disputed/superseded
        # OR pointer-superseded (dedup member) facts leak into default/unified search.
        decision_rows = connection.execute(
            """
            SELECT id, title, summary, rationale, status
            FROM decisions
            WHERE title LIKE ? ESCAPE '\\'
               OR summary LIKE ? ESCAPE '\\'
               OR rationale LIKE ? ESCAPE '\\'
               OR status LIKE ? ESCAPE '\\'
            ORDER BY id
            """,
            (pattern, pattern, pattern, pattern),
        ).fetchall()
        context_package_rows = connection.execute(
            """
            SELECT id, objective, status_summary, facts_summary,
                   events_summary, open_questions, risks,
                   recommended_next_actions, created_at
            FROM context_packages
            WHERE objective LIKE ? ESCAPE '\\'
               OR status_summary LIKE ? ESCAPE '\\'
               OR facts_summary LIKE ? ESCAPE '\\'
               OR events_summary LIKE ? ESCAPE '\\'
               OR open_questions LIKE ? ESCAPE '\\'
               OR risks LIKE ? ESCAPE '\\'
               OR recommended_next_actions LIKE ? ESCAPE '\\'
            ORDER BY id
            """,
            (pattern, pattern, pattern, pattern, pattern, pattern, pattern),
        ).fetchall()
        actor_rows = connection.execute(
            """
            SELECT id, name, actor_type, external_ref, notes
            FROM actors
            WHERE name LIKE ? ESCAPE '\\'
               OR actor_type LIKE ? ESCAPE '\\'
               OR external_ref LIKE ? ESCAPE '\\'
               OR notes LIKE ? ESCAPE '\\'
            ORDER BY id
            """,
            (pattern, pattern, pattern, pattern),
        ).fetchall()
        relationship_rows = connection.execute(
            """
            SELECT id, source_type, source_id, target_type, target_id,
                   relationship, summary
            FROM relationships
            WHERE source_type LIKE ? ESCAPE '\\'
               OR target_type LIKE ? ESCAPE '\\'
               OR relationship LIKE ? ESCAPE '\\'
               OR summary LIKE ? ESCAPE '\\'
            ORDER BY id
            """,
            (pattern, pattern, pattern, pattern),
        ).fetchall()
        audit_entry_rows = connection.execute(
            """
            SELECT id, action_type, target_type, target_id, reason, changed_fields
            FROM audit_entries
            WHERE action_type LIKE ? ESCAPE '\\'
               OR target_type LIKE ? ESCAPE '\\'
               OR CAST(target_id AS TEXT) LIKE ? ESCAPE '\\'
               OR reason LIKE ? ESCAPE '\\'
               OR changed_fields LIKE ? ESCAPE '\\'
            ORDER BY id
            """,
            (pattern, pattern, pattern, pattern, pattern),
        ).fetchall()

    for row in event_rows:
        results.append(
            SearchResult(
                "event",
                int(row["id"]),
                f"{row['event_type']} - {row['summary']}",
            )
        )

    for row in fact_rows:
        results.append(
            SearchResult(
                "fact",
                int(row["id"]),
                f"{row['fact_type']} - {row['statement']}",
            )
        )

    for row in decision_rows:
        results.append(
            SearchResult(
                "decision",
                int(row["id"]),
                f"{row['title']} - {row['summary']}",
            )
        )

    for row in context_package_rows:
        # identical snapshot label to the indexed lanes (see search_index._context_package_rows)
        # — a package hit is a historical artifact of its creation date, never current fact truth.
        results.append(
            SearchResult(
                "context_package",
                int(row["id"]),
                f"[snapshot {str(row['created_at'])[:10]}] {row['objective']} - {row['status_summary']}",
            )
        )

    for row in actor_rows:
        results.append(
            SearchResult(
                "actor",
                int(row["id"]),
                f"{row['actor_type']} - {row['name']}",
            )
        )

    for row in relationship_rows:
        results.append(
            SearchResult(
                "relationship",
                int(row["id"]),
                (
                    f"{row['source_type']}:{row['source_id']} - "
                    f"{row['relationship']} - "
                    f"{row['target_type']}:{row['target_id']}"
                ),
            )
        )

    for row in audit_entry_rows:
        reason = row["reason"] or ""
        results.append(
            SearchResult(
                "audit_entry",
                int(row["id"]),
                f"{row['action_type']} - {_target_text(row['target_type'], row['target_id'])} - {reason}",
            )
        )

    return results


def _target_text(target_type: str, target_id: int | None) -> str:
    return f"{target_type}:{'' if target_id is None else target_id}"
