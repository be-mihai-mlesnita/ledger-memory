from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import sqlite3

from ledger_memory.db import connect, connect_readonly
from ledger_memory.emission import emit_audit_and_event, emit_event_only


@dataclass(frozen=True)
class ContextPackage:
    id: int
    task_id: int | None
    task_run_id: int | None
    objective: str
    status_summary: str
    constraints_summary: str | None
    facts_summary: str | None
    events_summary: str | None
    open_questions: str | None
    risks: str | None
    recommended_next_actions: str | None
    created_at: str


@dataclass(frozen=True)
class ContextPackageItem:
    item_type: str
    item_id: int
    relationship: str


@dataclass(frozen=True)
class ContextPackageIntegrityItem:
    item_type: str
    item_id: int | None
    relationship: str
    status: str
    message: str


@dataclass(frozen=True)
class ContextPackageIntegrityReport:
    package_id: int
    status: str
    item_count: int
    broken_count: int
    warning_count: int
    items: list[ContextPackageIntegrityItem]






@dataclass(frozen=True)
class ImportContextPackageResult:
    package_id: int
    items_imported: int
    missing_targets: list[str]
    integrity_status: str




class ContextPackageNotFoundError(ValueError):
    pass


CONTEXT_PACKAGE_ITEM_TABLES = {
    "event": "events",
    "fact": "facts",
    "decision": "decisions",
    "actor": "actors",
    "relationship": "relationships",
    "context_package": "context_packages",
    "audit_entry": "audit_entries",
    # `artifact` was missing, so integrity false-flagged every artifact-backed fact_evidence
    # (the deepread research syntheses) as an orphan without ever checking the artifacts table.
    "artifact": "artifacts",
    # same bug class — `chunk` was missing, so RAG chunk-backed fact_evidence (the design, e.g.
    # fact 110 ← chunk 44) was false-flagged as an orphan without ever checking the chunks table.
    "chunk": "chunks",
    # `document` is the EXPORTED, authoritative provenance target RAG fact_evidence
    # must cite — a `chunk` is derived/non-exported, so citing it breaks repository-only recovery.
    "document": "documents",
    # entity_record_links cite the span receipts (same bug class as 0861/
    # 0923 — an unmapped type false-flags every link as an orphan without checking the table).
    "extraction_candidate": "extraction_candidates",
    "entity": "entities",
}


def create_context_package(
    database_path: Path,
    *,
    objective: str,
    status_summary: str,
    constraints_summary: str | None = None,
    facts_summary: str | None = None,
    events_summary: str | None = None,
    open_questions: str | None = None,
    risks: str | None = None,
    recommended_next_actions: str | None = None,
    items: list[ContextPackageItem] | None = None,
    actor_id: int | None = None,
    reason: str | None = None,
) -> ContextPackage:
    with connect(database_path) as connection:
        cursor = connection.execute(
            """
            INSERT INTO context_packages (
                task_id,
                task_run_id,
                objective,
                status_summary,
                constraints_summary,
                facts_summary,
                events_summary,
                open_questions,
                risks,
                recommended_next_actions
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                None,
                None,
                objective,
                status_summary,
                constraints_summary,
                facts_summary,
                events_summary,
                open_questions,
                risks,
                recommended_next_actions,
            ),
        )
        package_id = int(cursor.lastrowid)

        for item in items or []:
            connection.execute(
                """
                INSERT INTO context_package_items (
                    context_package_id,
                    item_type,
                    item_id,
                    relationship
                )
                VALUES (?, ?, ?, ?)
                """,
                (package_id, item.item_type, item.item_id, item.relationship),
            )

        metadata_dict: dict[str, object] = {"context_package_id": package_id}
        if reason is not None:
            metadata_dict["reason"] = reason
        emit_event_only(
            connection,
            event_type="context_package.created",
            actor_id=actor_id,
            summary=f"Context package created: {package_id}",
            metadata=json.dumps(metadata_dict, sort_keys=True),
        )
        row = _fetch_context_package_row(connection, package_id)

    if row is None:
        raise RuntimeError(f"created context package could not be loaded: {package_id}")
    return _row_to_context_package(row)






def list_context_packages(database_path: Path) -> list[ContextPackage]:
    with connect_readonly(database_path) as connection:  # pure read (integrity composes this)
        rows = connection.execute(
            """
            SELECT id, task_id, task_run_id, objective, status_summary,
                   constraints_summary, facts_summary, events_summary,
                   open_questions, risks, recommended_next_actions, created_at
            FROM context_packages
            ORDER BY id
            """
        ).fetchall()
    return [_row_to_context_package(row) for row in rows]


def get_context_package(
    database_path: Path,
    package_id: int,
) -> ContextPackage | None:
    with connect_readonly(database_path) as connection:  # pure read (integrity/show compose this)
        row = _fetch_context_package_row(connection, package_id)
    if row is None:
        return None
    return _row_to_context_package(row)


def add_context_package_item(
    database_path: Path,
    *,
    package_id: int,
    item_type: str,
    item_id: int,
    relationship: str,
    actor_id: int | None = None,
    reason: str | None = None,
) -> None:
    """Link an existing record to an existing context package — ground a handoff after the fact.

    Validates the package exists and the item ref resolves (unknown type or dangling id is rejected), so a
    backfill can never introduce the very orphan the integrity check flags.
    """
    if get_context_package(database_path, package_id) is None:
        raise ValueError(f"context package not found: {package_id}")
    table_name = CONTEXT_PACKAGE_ITEM_TABLES.get(item_type)
    if table_name is None:
        raise ValueError(f"unknown item_type: {item_type}")
    with connect(database_path) as connection:
        exists = connection.execute(
            f"SELECT 1 FROM {table_name} WHERE id = ?", (item_id,)
        ).fetchone() is not None
        if not exists:
            raise ValueError(f"{item_type} not found: {item_id}")
        connection.execute(
            """
            INSERT INTO context_package_items (context_package_id, item_type, item_id, relationship)
            VALUES (?, ?, ?, ?)
            """,
            (package_id, item_type, item_id, relationship),
        )
        emit_audit_and_event(
            connection,
            action_type="context_package.item_added",
            target_type="context_package",
            target_id=package_id,
            actor_id=actor_id,
            reason=reason,
            event_type="context_package.item_added",
            summary=f"Linked {item_type}:{item_id} ({relationship}) to context package {package_id}",
            metadata=json.dumps(
                {"item_type": item_type, "item_id": item_id, "relationship": relationship}, sort_keys=True
            ),
        )


def list_context_package_items(
    database_path: Path,
    package_id: int,
) -> list[ContextPackageItem]:
    with connect_readonly(database_path) as connection:  # pure read (integrity composes this)
        rows = connection.execute(
            """
            SELECT item_type, item_id, relationship
            FROM context_package_items
            WHERE context_package_id = ?
            ORDER BY id
            """,
            (package_id,),
        ).fetchall()
    return [
        ContextPackageItem(
            item_type=str(row["item_type"]),
            item_id=int(row["item_id"]),
            relationship=str(row["relationship"]),
        )
        for row in rows
    ]


def export_context_package_markdown(
    database_path: Path,
    package_id: int,
) -> str | None:
    package = get_context_package(database_path, package_id)
    if package is None:
        return None
    items = list_context_package_items(database_path, package_id)
    return render_context_package_markdown(package, items)


def render_context_package_markdown(
    package: ContextPackage,
    items: list[ContextPackageItem],
) -> str:
    sorted_items = sorted(
        items,
        key=lambda item: (item.item_type, item.item_id, item.relationship),
    )
    lines = [
        f"# Handover {package.id}",
        "",
        "## Objective",
        "",
        package.objective,
        "",
        "## Status Summary",
        "",
        package.status_summary,
        "",
        "## Constraints",
        "",
        package.constraints_summary or "",
        "",
        "## Facts",
        "",
        package.facts_summary or "",
        "",
        "## Events",
        "",
        package.events_summary or "",
        "",
        "## Open Questions",
        "",
        package.open_questions or "",
        "",
        "## Risks",
        "",
        package.risks or "",
        "",
        "## Recommended Next Actions",
        "",
        package.recommended_next_actions or "",
        "",
        "## Source Items",
        "",
        "| Type | ID | Relationship |",
        "| --- | ---: | --- |",
    ]
    for item in sorted_items:
        lines.append(f"| {item.item_type} | {item.item_id} | {item.relationship} |")

    lines.extend(
        [
            "",
            "## Metadata",
            "",
            f"- Package ID: {package.id}",
            f"- Task ID: {_optional_markdown_value(package.task_id)}",
            f"- Task Run ID: {_optional_markdown_value(package.task_run_id)}",
            f"- Created At: {package.created_at}",
            "",
        ]
    )
    return "\n".join(lines)


def check_context_package_integrity(
    database_path: Path,
    package_id: int,
) -> ContextPackageIntegrityReport | None:
    package = get_context_package(database_path, package_id)
    if package is None:
        return None

    package_items = list_context_package_items(database_path, package_id)
    integrity_items: list[ContextPackageIntegrityItem] = []

    with connect_readonly(database_path) as connection:  # pure read (integrity composes this)
        for item in package_items:
            table_name = CONTEXT_PACKAGE_ITEM_TABLES.get(item.item_type)
            if table_name is None:
                integrity_items.append(
                    ContextPackageIntegrityItem(
                        item_type=item.item_type,
                        item_id=item.item_id,
                        relationship=item.relationship,
                        status="warning",
                        message=f"unknown item type: {item.item_type}",
                    )
                )
                continue

            if _row_exists(connection, table_name, item.item_id):
                integrity_items.append(
                    ContextPackageIntegrityItem(
                        item_type=item.item_type,
                        item_id=item.item_id,
                        relationship=item.relationship,
                        status="ok",
                        message="",
                    )
                )
            else:
                integrity_items.append(
                    ContextPackageIntegrityItem(
                        item_type=item.item_type,
                        item_id=item.item_id,
                        relationship=item.relationship,
                        status="broken",
                        message=f"missing referenced {item.item_type}:{item.item_id}",
                    )
                )

    if not package_items:
        # a package with no item rows is not automatically suspect. `record_session`
        # links its package to the task and run BY COLUMN and fills the prose
        # fields; it never wrote `context_package_items`, and its docstring never claimed to. Warning on
        # those flagged 147 of 149 live packages — a signal that fires on 98.7% of the population
        # discriminates nothing, and the four genuinely unlinked packages (23, 24, 25, 138) were one line
        # in 148.
        #
        # The warning is NARROWED, never removed: no items AND no task/run linkage is still a warning, and
        # a dangling item ref is still `broken`. The linked case is reported as `ok` rather than dropped,
        # so the package still appears with its reason — a row that vanishes is a different defect.
        # linkage must be VALID, not merely present. If any column reference above was found
        # dangling or mismatched, this package is not "linked by task/run columns" — saying so would be a
        # second false reassurance layered on the first.
        column_refs_are_sound = not any(
            it.item_type.startswith("package.") and it.status == "broken" for it in integrity_items
        )
        linked_by_column = column_refs_are_sound and (
            package.task_id is not None or package.task_run_id is not None
        )
        integrity_items.append(
            ContextPackageIntegrityItem(
                item_type=("package linked by task/run columns" if linked_by_column
                           else "package has no source items"),
                item_id=None,
                relationship="",
                status="ok" if linked_by_column else "warning",
                message=("no item rows; linked by task/run columns" if linked_by_column
                         else "no source items and no task/run linkage"),
            )
        )

    broken_count = sum(1 for item in integrity_items if item.status == "broken")
    warning_count = sum(1 for item in integrity_items if item.status == "warning")
    status = "ok"
    if broken_count:
        status = "broken"
    elif warning_count:
        status = "warning"

    return ContextPackageIntegrityReport(
        package_id=package_id,
        status=status,
        item_count=len(package_items),
        broken_count=broken_count,
        warning_count=warning_count,
        items=integrity_items,
    )


def context_package_integrity_report_to_dict(
    report: ContextPackageIntegrityReport,
) -> dict[str, object]:
    return {
        "package_id": report.package_id,
        "status": report.status,
        "item_count": report.item_count,
        "broken_count": report.broken_count,
        "warning_count": report.warning_count,
        "items": [
            {
                "item_type": item.item_type,
                "item_id": item.item_id,
                "relationship": item.relationship,
                "status": item.status,
                "message": item.message,
            }
            for item in report.items
        ],
    }


# ---------------------------------------------------------------------------
# JSON export
# ---------------------------------------------------------------------------

def export_context_package_json(
    database_path: Path,
    *,
    package_id: int,
    output_path: Path | None = None,
) -> str:
    """Export a context package as JSON.

    Returns the JSON string. If output_path is provided, also writes to file.
    Raises ContextPackageNotFoundError when the package is absent.
    """
    package = get_context_package(database_path, package_id)
    if package is None:
        raise ContextPackageNotFoundError(f"context package not found: {package_id}")
    items = list_context_package_items(database_path, package_id)

    data = {
        "format": "ledger-context-package",
        "format_version": 1,
        "package": {
            "id": package.id,
            "task_id": package.task_id,
            "task_run_id": package.task_run_id,
            "objective": package.objective,
            "status_summary": package.status_summary,
            "constraints_summary": package.constraints_summary,
            "facts_summary": package.facts_summary,
            "events_summary": package.events_summary,
            "open_questions": package.open_questions,
            "risks": package.risks,
            "recommended_next_actions": package.recommended_next_actions,
            "created_at": package.created_at,
        },
        "items": [
            {
                "item_type": item.item_type,
                "item_id": item.item_id,
                "relationship": item.relationship,
            }
            for item in items
        ],
    }
    json_str = json.dumps(data, indent=2, sort_keys=False)
    if output_path is not None:
        output_path.write_text(json_str, encoding="utf-8")
    return json_str


# ---------------------------------------------------------------------------
# JSON import
# ---------------------------------------------------------------------------

def import_context_package(
    database_path: Path,
    *,
    json_path: Path,
    actor_id: int | None = None,
    reason: str | None = None,
) -> ImportContextPackageResult:
    """Import a context package from a JSON file.

    Reads the JSON file, validates format, inserts the package and items with
    new ids (original ids are not reused). Missing target rows are recorded as
    warnings but do not abort the import.
    Emits an audit-and-event for context_package.imported.
    """
    try:
        raw = json_path.read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"failed to read JSON: {exc}") from exc

    if data.get("format") != "ledger-context-package":
        raise ValueError(
            f"invalid format: expected 'ledger-context-package', got {data.get('format')!r}"
        )
    if data.get("format_version") != 1:
        raise ValueError(
            f"unsupported format_version: {data.get('format_version')!r}"
        )

    pkg = data.get("package", {})
    raw_items = data.get("items", [])
    missing_targets: list[str] = []

    with connect(database_path) as connection:
        # task_id and task_run_id are source-db references that may not exist
        # in the target; we do not carry them over to avoid FK violations.
        # The items array carries the authoritative item references instead.
        cursor = connection.execute(
            """
            INSERT INTO context_packages (
                task_id, task_run_id, objective, status_summary,
                constraints_summary, facts_summary, events_summary,
                open_questions, risks, recommended_next_actions
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                None,
                None,
                pkg.get("objective", ""),
                pkg.get("status_summary", ""),
                pkg.get("constraints_summary"),
                pkg.get("facts_summary"),
                pkg.get("events_summary"),
                pkg.get("open_questions"),
                pkg.get("risks"),
                pkg.get("recommended_next_actions"),
            ),
        )
        new_package_id = int(cursor.lastrowid)

        for item in raw_items:
            item_type = str(item.get("item_type", ""))
            item_id = int(item.get("item_id", 0))
            relationship = str(item.get("relationship", ""))

            table_name = CONTEXT_PACKAGE_ITEM_TABLES.get(item_type)
            if table_name is not None:
                exists = connection.execute(
                    f"SELECT 1 FROM {table_name} WHERE id = ?",
                    (item_id,),
                ).fetchone() is not None
                if not exists:
                    missing_targets.append(f"item_type={item_type} item_id={item_id}")

            connection.execute(
                """
                INSERT INTO context_package_items (
                    context_package_id, item_type, item_id, relationship
                )
                VALUES (?, ?, ?, ?)
                """,
                (new_package_id, item_type, item_id, relationship),
            )

        integrity_status = "warning" if missing_targets else "ok"

        metadata_dict: dict[str, object] = {
            "context_package_id": new_package_id,
            "items_imported": len(raw_items),
            "integrity_status": integrity_status,
        }
        if reason is not None:
            metadata_dict["reason"] = reason

        emit_audit_and_event(
            connection,
            action_type="context_package.imported",
            target_type="context_package",
            target_id=new_package_id,
            actor_id=actor_id,
            reason=reason,
            event_type="context_package.imported",
            summary=f"Context package imported: {new_package_id}",
            metadata=json.dumps(metadata_dict, sort_keys=True),
        )

    return ImportContextPackageResult(
        package_id=new_package_id,
        items_imported=len(raw_items),
        missing_targets=missing_targets,
        integrity_status=integrity_status,
    )


# ---------------------------------------------------------------------------
# Freshness
# ---------------------------------------------------------------------------

# Content columns per table (for size accounting)
_TABLE_CONTENT_COLUMNS: dict[str, list[str]] = {
    "events": ["summary", "metadata"],
    "facts": ["statement"],
    "decisions": ["title", "summary", "rationale"],
    "actors": ["name", "notes"],
    "relationships": ["summary"],
    "context_packages": ["objective", "status_summary", "constraints_summary",
                          "facts_summary", "events_summary", "open_questions",
                          "risks", "recommended_next_actions"],
    "audit_entries": ["action_type", "reason"],
}


def get_context_package_freshness(
    database_path: Path,
    *,
    package_id: int,
) -> list[dict]:
    """Return freshness info for each item in a context package.

    Each dict: {item_type, item_id, source_exists, stale, source_updated_at}.
    stale is None when updated_at is unavailable for that table type.
    """
    package = get_context_package(database_path, package_id)
    if package is None:
        return []

    items = list_context_package_items(database_path, package_id)
    result: list[dict] = []

    with connect_readonly(database_path) as connection:  # pure read (freshness)
        for item in items:
            table_name = CONTEXT_PACKAGE_ITEM_TABLES.get(item.item_type)
            if table_name is None:
                result.append({
                    "item_type": item.item_type,
                    "item_id": item.item_id,
                    "source_exists": False,
                    "stale": None,
                    "source_updated_at": None,
                })
                continue

            row = connection.execute(
                f"SELECT * FROM {table_name} WHERE id = ?",
                (item.item_id,),
            ).fetchone()

            if row is None:
                result.append({
                    "item_type": item.item_type,
                    "item_id": item.item_id,
                    "source_exists": False,
                    "stale": None,
                    "source_updated_at": None,
                })
                continue

            # Check if the table has updated_at
            col_names = [desc[0] for desc in connection.execute(
                f"SELECT * FROM {table_name} WHERE id = ?", (item.item_id,)
            ).description]

            if "updated_at" in col_names:
                source_updated_at = str(row["updated_at"])
                stale = source_updated_at > package.created_at
            else:
                source_updated_at = None
                stale = None

            result.append({
                "item_type": item.item_type,
                "item_id": item.item_id,
                "source_exists": True,
                "stale": stale,
                "source_updated_at": source_updated_at,
            })

    return result


# ---------------------------------------------------------------------------
# Size budget
# ---------------------------------------------------------------------------

def get_context_package_size(
    database_path: Path,
    *,
    package_id: int,
) -> dict:
    """Return character-count size info for each item in a context package.

    Returns {"items": [{item_type, item_id, char_count}], "total_chars": int}.
    """
    items = list_context_package_items(database_path, package_id)
    item_sizes: list[dict] = []
    total_chars = 0

    with connect_readonly(database_path) as connection:  # pure read (size)
        for item in items:
            table_name = CONTEXT_PACKAGE_ITEM_TABLES.get(item.item_type)
            if table_name is None:
                item_sizes.append({
                    "item_type": item.item_type,
                    "item_id": item.item_id,
                    "char_count": 0,
                })
                continue

            row = connection.execute(
                f"SELECT * FROM {table_name} WHERE id = ?",
                (item.item_id,),
            ).fetchone()

            if row is None:
                item_sizes.append({
                    "item_type": item.item_type,
                    "item_id": item.item_id,
                    "char_count": 0,
                })
                continue

            content_cols = _TABLE_CONTENT_COLUMNS.get(table_name, [])
            char_count = 0
            for col in content_cols:
                try:
                    val = row[col]
                    if val is not None:
                        char_count += len(str(val))
                except (IndexError, KeyError):
                    pass

            total_chars += char_count
            item_sizes.append({
                "item_type": item.item_type,
                "item_id": item.item_id,
                "char_count": char_count,
            })

    return {"items": item_sizes, "total_chars": total_chars}


def _optional_markdown_value(value: object | None) -> str:
    return "" if value is None else str(value)


def _fetch_context_package_row(
    connection: sqlite3.Connection,
    package_id: int,
) -> sqlite3.Row | None:
    return connection.execute(
        """
        SELECT id, task_id, task_run_id, objective, status_summary,
               constraints_summary, facts_summary, events_summary,
               open_questions, risks, recommended_next_actions, created_at
        FROM context_packages
        WHERE id = ?
        """,
        (package_id,),
    ).fetchone()


def _row_to_context_package(row: sqlite3.Row) -> ContextPackage:
    task_id = row["task_id"]
    task_run_id = row["task_run_id"]
    return ContextPackage(
        id=int(row["id"]),
        task_id=None if task_id is None else int(task_id),
        task_run_id=None if task_run_id is None else int(task_run_id),
        objective=str(row["objective"]),
        status_summary=str(row["status_summary"]),
        constraints_summary=row["constraints_summary"],
        facts_summary=row["facts_summary"],
        events_summary=row["events_summary"],
        open_questions=row["open_questions"],
        risks=row["risks"],
        recommended_next_actions=row["recommended_next_actions"],
        created_at=str(row["created_at"]),
    )




def _row_exists(
    connection: sqlite3.Connection,
    table: str,
    row_id: int,
) -> bool:
    return connection.execute(
        f"SELECT 1 FROM {table} WHERE id = ?",
        (row_id,),
    ).fetchone() is not None
