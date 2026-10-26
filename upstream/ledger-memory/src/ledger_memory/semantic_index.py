"""Semantic index contract.

Core-side metadata only. Vector data lives in the adapter-owned sidecar at
the vectors file beside the database (see `ledger_memory.paths`, or overridden by
`LEDGER_VECTORS_PATH` env). Core code never opens or writes the sidecar.
When no backend is installed or the sidecar is absent, `semantic-index-status`
reports the metadata state as of the core tables and exits 0.

Sidecar layout decision: plain SQLite file, not in the core database,
so the core schema has no dependency on any loadable extension. The adapter
 creates and manages the sidecar. The core only reads
`semantic_index_entries` and `semantic_index_rebuilds`.
"""

from __future__ import annotations
from ledger_memory.paths import default_vectors_path

import os
from dataclasses import dataclass
from pathlib import Path

from ledger_memory.db import connect_readonly  # status/diff/integrity are pure reads — no journal_mode mutation
from ledger_memory.embeddings import get_embedding_backend
from ledger_memory.search_index import _source_rows, _content_hash


@dataclass(frozen=True)
class SemanticIndexStatus:
    indexed_rows: int
    expected_rows: int
    metadata_rows: int
    missing_rows: int
    orphaned_rows: int
    stale_rows: int
    latest_rebuild_id: int | None
    latest_rebuild_status: str | None
    latest_rebuild_started_at: str | None
    latest_rebuild_completed_at: str | None
    latest_rebuild_indexed_rows: int | None
    latest_rebuild_embedding_model: str | None
    empty: bool
    never_rebuilt: bool
    fresh: bool


@dataclass(frozen=True)
class SemanticIndexDiffRow:
    category: str
    record_type: str
    record_id: int
    expected_hash: str | None
    actual_hash: str | None
    title: str | None
    embedding_model: str | None


@dataclass(frozen=True)
class SemanticIndexIntegrityReport:
    status: SemanticIndexStatus
    missing_sidecar_rows: int
    unexpected_sidecar_rows: int
    recommendation: str
    diff_rows: list[SemanticIndexDiffRow]


SEMANTIC_INDEX_DIFF_CATEGORIES = ("missing", "stale", "orphaned")


def _active_embedding_model(database_path: Path) -> str | None:
    """The active backend's `model_name`, or None if it cannot be resolved (an unknown/misconfigured backend). On
    None, freshness falls back to unscoped — the semantic lane is unusable anyway, and status must still work without a
    valid backend. model_name itself is a cheap config string, no network."""
    try:
        return get_embedding_backend(database_path).model_name
    except Exception:
        return None


def get_semantic_index_status(database_path: Path) -> SemanticIndexStatus:
    # scope freshness to the ACTIVE embedding model — semantic_search filters sidecar vectors by
    # backend.model_name, so an index built by an obsolete model must read NOT fresh, not "fresh" off stale-model rows.
    active_model = _active_embedding_model(database_path)
    with connect_readonly(database_path) as connection:
        source_rows = _source_rows(connection)
        expected_hashes = {
            (record_type, record_id): _content_hash(record_type, record_id, title, body)
            for record_type, record_id, title, body in source_rows
        }

        indexed_rows_row = connection.execute(
            """
            SELECT COUNT(DISTINCT record_type || '|' || record_id) AS cnt
            FROM semantic_index_entries
            WHERE (? IS NULL OR embedding_model = ?)
            """,
            (active_model, active_model),
        ).fetchone()
        indexed_rows = int(indexed_rows_row["cnt"])

        metadata_raw = connection.execute(
            """
            SELECT record_type, record_id, content_hash, embedding_model
            FROM semantic_index_entries
            WHERE (? IS NULL OR embedding_model = ?)
            ORDER BY id
            """,
            (active_model, active_model),
        ).fetchall()

        metadata: dict[tuple[str, int], str] = {}
        for row in metadata_raw:
            key = (str(row["record_type"]), int(row["record_id"]))
            if key not in metadata:
                metadata[key] = str(row["content_hash"])

        metadata_rows = len(metadata)

        latest_rebuild = connection.execute(
            """
            SELECT id, status, embedding_model, started_at, completed_at, indexed_rows
            FROM semantic_index_rebuilds
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()

    expected_keys = set(expected_hashes)
    metadata_keys = set(metadata)
    missing_rows = len(expected_keys - metadata_keys)
    orphaned_rows = len(metadata_keys - expected_keys)
    stale_rows = sum(
        1
        for key in expected_keys & metadata_keys
        if expected_hashes[key] != metadata[key]
    )
    expected_rows = len(expected_hashes)
    never_rebuilt = latest_rebuild is None
    empty = indexed_rows == 0
    fresh = (
        not never_rebuilt
        and missing_rows == 0
        and orphaned_rows == 0
        and stale_rows == 0
        and indexed_rows == expected_rows
        and metadata_rows == expected_rows
    )

    return SemanticIndexStatus(
        indexed_rows=indexed_rows,
        expected_rows=expected_rows,
        metadata_rows=metadata_rows,
        missing_rows=missing_rows,
        orphaned_rows=orphaned_rows,
        stale_rows=stale_rows,
        latest_rebuild_id=None if latest_rebuild is None else int(latest_rebuild["id"]),
        latest_rebuild_status=None
        if latest_rebuild is None
        else str(latest_rebuild["status"]),
        latest_rebuild_started_at=None
        if latest_rebuild is None
        else str(latest_rebuild["started_at"]),
        latest_rebuild_completed_at=None
        if latest_rebuild is None or latest_rebuild["completed_at"] is None
        else str(latest_rebuild["completed_at"]),
        latest_rebuild_indexed_rows=None
        if latest_rebuild is None or latest_rebuild["indexed_rows"] is None
        else int(latest_rebuild["indexed_rows"]),
        latest_rebuild_embedding_model=None
        if latest_rebuild is None or latest_rebuild["embedding_model"] is None
        else str(latest_rebuild["embedding_model"]),
        empty=empty,
        never_rebuilt=never_rebuilt,
        fresh=fresh,
    )


def get_semantic_index_diff(database_path: Path) -> list[SemanticIndexDiffRow]:
    active_model = _active_embedding_model(database_path)  # diff against the ACTIVE model (None ⇒ unscoped)
    with connect_readonly(database_path) as connection:
        source_rows = _source_rows(connection)
        source: dict[tuple[str, int], tuple[str, str]] = {}
        for record_type, record_id, title, body in source_rows:
            key = (record_type, record_id)
            if key not in source:
                source[key] = (_content_hash(record_type, record_id, title, body), title)

        metadata: dict[tuple[str, int], tuple[str, str]] = {}
        for row in connection.execute(
            "SELECT record_type, record_id, content_hash, embedding_model FROM semantic_index_entries WHERE (? IS NULL OR embedding_model = ?) ORDER BY embedded_at DESC",
            (active_model, active_model),
        ).fetchall():
            key = (str(row["record_type"]), int(row["record_id"]))
            if key not in metadata:
                metadata[key] = (str(row["content_hash"]), str(row["embedding_model"]))

        diff_rows: list[SemanticIndexDiffRow] = []
        for (record_type, record_id), (source_hash, title) in source.items():
            if (record_type, record_id) not in metadata:
                diff_rows.append(
                    SemanticIndexDiffRow(
                        category="missing",
                        record_type=record_type,
                        record_id=record_id,
                        expected_hash=None,
                        actual_hash=source_hash,
                        title=title,
                        embedding_model=None,
                    )
                )
            else:
                metadata_hash, embedding_model = metadata[(record_type, record_id)]
                if source_hash != metadata_hash:
                    diff_rows.append(
                        SemanticIndexDiffRow(
                            category="stale",
                            record_type=record_type,
                            record_id=record_id,
                            expected_hash=metadata_hash,
                            actual_hash=source_hash,
                            title=title,
                            embedding_model=embedding_model,
                        )
                    )

        for (record_type, record_id), (metadata_hash, embedding_model) in metadata.items():
            if (record_type, record_id) not in source:
                diff_rows.append(
                    SemanticIndexDiffRow(
                        category="orphaned",
                        record_type=record_type,
                        record_id=record_id,
                        expected_hash=metadata_hash,
                        actual_hash=None,
                        title=None,
                        embedding_model=embedding_model,
                    )
                )

        category_rank = {
            category: index for index, category in enumerate(SEMANTIC_INDEX_DIFF_CATEGORIES)
        }
        return sorted(
            diff_rows,
            key=lambda row: (category_rank[row.category], row.record_type, row.record_id),
        )


def semantic_index_diff_row_to_dict(row: SemanticIndexDiffRow) -> dict[str, object]:
    return {
        "category": row.category,
        "record_type": row.record_type,
        "record_id": row.record_id,
        "expected_hash": row.expected_hash,
        "actual_hash": row.actual_hash,
        "title": row.title,
        "embedding_model": row.embedding_model,
    }


def get_semantic_index_integrity_report(
    database_path: Path, *, limit: int = 20, category: str | None = None, sidecar_path: Path | None = None
) -> SemanticIndexIntegrityReport:
    status = get_semantic_index_status(database_path)
    all_diff_rows = get_semantic_index_diff(database_path)

    # default the sidecar the same way rebuild/search do, so the no-argument command inspects the
    # REAL default sidecar instead of treating None as "skip the sidecar check".
    if sidecar_path is None:
        sidecar_path = default_vectors_path(database_path)

    # scope the sidecar/core comparison to the ACTIVE embedding model (the composite key includes
    # embedding_model) so obsolete-model vectors with the same keys don't mask the active model having no usable
    # vectors. also reject a sidecar vector whose blob length != embedding_dimensions*4 (corrupt) — it
    # surfaces as a missing sidecar row rather than silently passing integrity while semantic_search would crash on it.
    active_model = _active_embedding_model(database_path)

    missing_sidecar_rows = 0
    unexpected_sidecar_rows = 0

    if sidecar_path.exists():
        with connect_readonly(database_path) as core_conn:
            core_rows = core_conn.execute(
                "SELECT record_type, record_id, embedding_dimensions FROM semantic_index_entries WHERE (? IS NULL OR embedding_model = ?)",
                (active_model, active_model),
            ).fetchall()
        core_keys = {(str(r["record_type"]), int(r["record_id"])) for r in core_rows}
        expected_dims = {int(r["embedding_dimensions"]) for r in core_rows}
        expected_len = next(iter(expected_dims)) * 4 if len(expected_dims) == 1 else None

        with connect_readonly(sidecar_path) as sidecar_conn:
            sidecar_keys = set()
            for r in sidecar_conn.execute(
                "SELECT record_type, record_id, vector FROM semantic_vectors WHERE (? IS NULL OR embedding_model = ?)",
                (active_model, active_model),
            ).fetchall():
                if expected_len is not None and len(r["vector"]) != expected_len:
                    continue  # corrupt-length vector → not a usable sidecar row (counts as missing)
                sidecar_keys.add((str(r["record_type"]), int(r["record_id"])))

        missing_sidecar_rows = len(core_keys - sidecar_keys)
        unexpected_sidecar_rows = len(sidecar_keys - core_keys)
    elif status.metadata_rows > 0:
        # core metadata exists but the vector sidecar is GONE — every indexed row is missing its
        # vector. Report that instead of silently passing (the old `is not None and exists()` gate hid this).
        missing_sidecar_rows = status.metadata_rows

    recommendation = "run_rebuild_semantic_index_manually" if status.never_rebuilt else (
        "no_action_needed"
        if status.fresh and not all_diff_rows and missing_sidecar_rows == 0 and unexpected_sidecar_rows == 0
        else "review_diff_or_rebuild_manually"
    )

    filtered = all_diff_rows
    if category is not None:
        filtered = [row for row in all_diff_rows if row.category == category]

    diff_rows = filtered[:max(limit, 0)]

    return SemanticIndexIntegrityReport(
        status=status,
        missing_sidecar_rows=missing_sidecar_rows,
        unexpected_sidecar_rows=unexpected_sidecar_rows,
        recommendation=recommendation,
        diff_rows=diff_rows,
    )


def semantic_index_integrity_report_to_dict(report: SemanticIndexIntegrityReport) -> dict[str, object]:
    status = report.status
    return {
        "fresh": status.fresh,
        "never_rebuilt": status.never_rebuilt,
        "indexed_rows": status.indexed_rows,
        "expected_rows": status.expected_rows,
        "metadata_rows": status.metadata_rows,
        "missing_rows": status.missing_rows,
        "stale_rows": status.stale_rows,
        "orphaned_rows": status.orphaned_rows,
        "missing_sidecar_rows": report.missing_sidecar_rows,
        "unexpected_sidecar_rows": report.unexpected_sidecar_rows,
        "latest_rebuild": {
            "id": status.latest_rebuild_id,
            "status": status.latest_rebuild_status,
            "embedding_model": status.latest_rebuild_embedding_model,
            "started_at": status.latest_rebuild_started_at,
            "completed_at": status.latest_rebuild_completed_at,
            "indexed_rows": status.latest_rebuild_indexed_rows,
        },
        "recommendation": report.recommendation,
        "diff": [semantic_index_diff_row_to_dict(row) for row in report.diff_rows],
    }
