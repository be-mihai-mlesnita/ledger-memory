from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ledger_memory.context_packages import (
    CONTEXT_PACKAGE_ITEM_TABLES,
    ContextPackageIntegrityReport,
    check_context_package_integrity,
    list_context_packages,
)
from ledger_memory.db import DERIVED_TABLES, REQUIRED_TABLES, connect_readonly
from ledger_memory.search_index import SearchIndexIntegrityReport, get_search_index_integrity_report


@dataclass(frozen=True)
class OrphanReference:
    table: str
    row_id: int
    ref_type: str
    ref_id: int


@dataclass(frozen=True)
class IntegrityReport:
    tables_ok: bool
    fk_violations: list[object]
    orphan_references: list[OrphanReference]
    export_closure_violations: list[OrphanReference]
    search_index_fresh: bool
    search_index_recommendation: str
    package_reports: list[ContextPackageIntegrityReport]
    storage_ok: bool
    ok: bool


def check_integrity(database_path: Path) -> IntegrityReport:
    with connect_readonly(database_path) as connection:  # F006: the integrity scan is pure read
        # (a) Required tables present
        present_tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        missing_tables = [t for t in REQUIRED_TABLES if t not in present_tables]
        tables_ok = len(missing_tables) == 0

        # (b) SQLite PRAGMA foreign_key_check
        fk_violations = connection.execute("PRAGMA foreign_key_check").fetchall()

        # (c) Polymorphic orphan scanning
        orphan_references = _scan_polymorphic_orphans(connection)
        # (c2) Export-recovery closure: authoritative refs to DERIVED, non-exported tables (audit F001)
        export_closure_violations = _scan_export_closure(connection)

        # (f) Page-level storage integrity via PRAGMA quick_check
        qc_row = connection.execute("PRAGMA quick_check").fetchone()
        storage_ok = qc_row is not None and str(qc_row[0]) == "ok"

    # (d) Search index integrity — composed from existing function
    search_report: SearchIndexIntegrityReport = get_search_index_integrity_report(
        database_path
    )
    search_index_fresh = search_report.status.fresh
    search_index_recommendation = search_report.recommendation

    # (e) Context package integrity for every package — composed from existing function
    packages = list_context_packages(database_path)
    package_reports: list[ContextPackageIntegrityReport] = []
    for package in packages:
        report = check_context_package_integrity(database_path, package.id)
        if report is not None:
            package_reports.append(report)

    # a `warning` (e.g. a handoff with no source items) is NOT corruption — only `broken`
    # fails integrity. Warnings stay visible in the report (counted + surfaced), so they still drive a fix
    # without crying "broken" and training everyone to ignore the signal.
    packages_ok = all(r.status != "broken" for r in package_reports)

    # ok = tables ok, zero FK violations, zero orphans, every package ok, storage ok.
    # Search-index staleness does NOT fail integrity.
    ok = (
        tables_ok
        and len(fk_violations) == 0
        and len(orphan_references) == 0
        and len(export_closure_violations) == 0
        and packages_ok
        and storage_ok
    )

    return IntegrityReport(
        tables_ok=tables_ok,
        fk_violations=list(fk_violations),
        orphan_references=orphan_references,
        export_closure_violations=export_closure_violations,
        search_index_fresh=search_index_fresh,
        search_index_recommendation=search_index_recommendation,
        package_reports=package_reports,
        storage_ok=storage_ok,
        ok=ok,
    )


def _scan_export_closure(connection) -> list[OrphanReference]:
    """Export-recovery closure: an exported (authoritative) polymorphic reference that
    points at a DERIVED, non-exported table breaks repository-only recovery — import-from-export recreates the
    referrer but never the referent. Distinct from an orphan: the referent may exist live yet still not round-trip
    through the committed export. RAG ``fact_evidence`` must cite the exported ``document`` (+ a stable selector),
    never the derived ``chunk`` id."""
    derived = set(DERIVED_TABLES)
    violations: list[OrphanReference] = []
    scans = (
        ("relationships",
         "SELECT id, source_type, source_id, target_type, target_id FROM relationships",
         lambda r: ((r[1], r[2]), (r[3], r[4]))),
        ("context_package_items",
         "SELECT id, item_type, item_id FROM context_package_items",
         lambda r: ((r[1], r[2]),)),
        ("fact_evidence",
         "SELECT id, evidence_type, evidence_id FROM fact_evidence",
         lambda r: ((r[1], r[2]),)),
    )
    for table, sql, extract in scans:
        for row in connection.execute(sql).fetchall():
            row_id = int(row[0])
            for ref_type, ref_id in extract(row):
                if ref_type is None or ref_id is None:
                    continue
                if CONTEXT_PACKAGE_ITEM_TABLES.get(str(ref_type)) in derived:
                    violations.append(OrphanReference(
                        table=table, row_id=row_id, ref_type=str(ref_type), ref_id=int(ref_id)))
    return violations


def _scan_polymorphic_orphans(connection) -> list[OrphanReference]:
    orphans: list[OrphanReference] = []

    # --- relationships: source_type/source_id and target_type/target_id ---
    rel_rows = connection.execute(
        """
        SELECT id, source_type, source_id, target_type, target_id
        FROM relationships
        """
    ).fetchall()
    for row in rel_rows:
        row_id = int(row[0])
        for ref_type, ref_id in (
            (str(row[1]), int(row[2])),
            (str(row[3]), int(row[4])),
        ):
            table_name = CONTEXT_PACKAGE_ITEM_TABLES.get(ref_type)
            if table_name is None:
                orphans.append(OrphanReference(
                    table="relationships",
                    row_id=row_id,
                    ref_type=ref_type,
                    ref_id=ref_id,
                ))
            else:
                exists = connection.execute(
                    f"SELECT 1 FROM {table_name} WHERE id = ? LIMIT 1",
                    (ref_id,),
                ).fetchone()
                if exists is None:
                    orphans.append(OrphanReference(
                        table="relationships",
                        row_id=row_id,
                        ref_type=ref_type,
                        ref_id=ref_id,
                    ))

    # --- context_package_items: item_type/item_id ---
    cpi_rows = connection.execute(
        """
        SELECT id, item_type, item_id
        FROM context_package_items
        """
    ).fetchall()
    for row in cpi_rows:
        row_id = int(row[0])
        ref_type = str(row[1])
        ref_id = int(row[2])
        table_name = CONTEXT_PACKAGE_ITEM_TABLES.get(ref_type)
        if table_name is None:
            orphans.append(OrphanReference(
                table="context_package_items",
                row_id=row_id,
                ref_type=ref_type,
                ref_id=ref_id,
            ))
        else:
            exists = connection.execute(
                f"SELECT 1 FROM {table_name} WHERE id = ? LIMIT 1",
                (ref_id,),
            ).fetchone()
            if exists is None:
                orphans.append(OrphanReference(
                    table="context_package_items",
                    row_id=row_id,
                    ref_type=ref_type,
                    ref_id=ref_id,
                ))

    # --- fact_evidence: evidence_type/evidence_id ---
    fe_rows = connection.execute(
        """
        SELECT id, evidence_type, evidence_id
        FROM fact_evidence
        """
    ).fetchall()
    for row in fe_rows:
        row_id = int(row[0])
        ref_type = str(row[1])
        ref_id = int(row[2])
        table_name = CONTEXT_PACKAGE_ITEM_TABLES.get(ref_type)
        if table_name is None:
            orphans.append(OrphanReference(
                table="fact_evidence",
                row_id=row_id,
                ref_type=ref_type,
                ref_id=ref_id,
            ))
        else:
            exists = connection.execute(
                f"SELECT 1 FROM {table_name} WHERE id = ? LIMIT 1",
                (ref_id,),
            ).fetchone()
            if exists is None:
                orphans.append(OrphanReference(
                    table="fact_evidence",
                    row_id=row_id,
                    ref_type=ref_type,
                    ref_id=ref_id,
                ))

    return orphans
