"""
Unified search: merges LIKE (floor), FTS5, and semantic lanes via RRF.

Lane policy:
- LIKE lane always runs as the authoritative floor (never retired).
- FTS lane runs only when the search index is fresh.
- Semantic lane runs only when the semantic index is fresh.
- Every degraded derived lane emits a warning to stderr.

RRF formula: score(rank) = 1.0 / (60 + rank), 1-based rank.
"""
from __future__ import annotations

import sys
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from ledger_memory.search import search_records
from ledger_memory.search_index import fts_search, get_search_index_status
from ledger_memory.semantic_index import get_semantic_index_status
from ledger_memory.semantic_search import semantic_search_outcome

# Lane name constants (module-level, not an enum to stay stdlib-clean).
UnifiedSearchLane = str
LANE_FTS = "fts"
LANE_SEMANTIC = "semantic"
LANE_LIKE = "like"

# Verbatim stale warning from the design / CLI-REFERENCE (copied once here).
_STALE_WARNING = "warning: search index is not fresh; falling back to LIKE search"


@dataclass(frozen=True)
class UnifiedSearchResult:
    record_type: str
    record_id: int
    title: str
    score: float
    lanes: tuple[str, ...]
    explain: str


def _rrf(rank: int, k: int = 60) -> float:
    """Reciprocal Rank Fusion score: 1.0 / (k + rank), 1-based rank."""
    return 1.0 / (k + rank)


def unified_search(
    database_path: Path,
    query: str,
    *,
    limit: int = 10,
    sidecar_path: Path | None = None,
    permission_filter: Callable[[str, int], bool] | None = None,
) -> list[UnifiedSearchResult]:
    """
    Merge LIKE, FTS, and semantic lanes via RRF with k=60.

    Args:
        database_path: Path to the ledger SQLite database.
        query: Free-text search query.
        limit: Maximum number of results to return (applied after filtering).
        sidecar_path: Optional path to the semantic sidecar database.
        permission_filter: Optional callback (record_type, record_id) -> bool.
            Defaults to allow-all. Applied before the limit.

    Returns:
        Ranked list of UnifiedSearchResult, up to ``limit`` items.
    """
    if permission_filter is None:
        permission_filter = lambda record_type, record_id: True  # noqa: E731

    fts_status = get_search_index_status(database_path)
    semantic_status = get_semantic_index_status(database_path)
    fts_fresh = fts_status.fresh
    semantic_fresh = semantic_status.fresh

    # Emit per-lane degradation warnings to stderr.
    if not fts_fresh:
        print(_STALE_WARNING, file=sys.stderr)
    if not semantic_fresh:
        print(
            "warning: semantic index is not fresh; semantic lane disabled"
            " — run rebuild-semantic-index to restore",
            file=sys.stderr,
        )

    # Accumulators: keyed by (record_type, record_id).
    rrf_scores: dict[tuple[str, int], float] = defaultdict(float)
    # Store per-record data: record_type, record_id, title, contributing lanes.
    record_data: dict[tuple[str, int], dict] = {}

    def _register(record_type: str, record_id: int, title: str, lane: str, rank: int) -> None:
        key = (record_type, record_id)
        rrf_scores[key] += _rrf(rank)
        if key not in record_data:
            record_data[key] = {
                "record_type": record_type,
                "record_id": record_id,
                "title": title,
                "lanes": set(),
            }
        record_data[key]["lanes"].add(lane)

    # LIKE lane — always runs as the authoritative floor.
    like_results = search_records(database_path, query)
    for rank, result in enumerate(like_results, start=1):
        _register(result.record_type, result.record_id, result.text, LANE_LIKE, rank)

    # FTS lane — only when fresh.
    if fts_fresh:
        fts_results = fts_search(database_path, query)
        for rank, result in enumerate(fts_results, start=1):
            _register(result.record_type, result.record_id, result.title, LANE_FTS, rank)

    # Semantic lane — only when fresh, AND only when the sidecar gate admits it (an earlier fix, owner-
    # approved scoped edit). Freshness is a statement about core metadata; the gate is a statement about
    # the sidecar. Before this, a fresh-but-blocked lane ran nothing while every explain line still read
    # `semantic_fresh=yes` — a silent degraded lane, the exact state an earlier fix's warning rule forbids.
    semantic_ready = False
    if semantic_fresh:
        sem_results, semantic_blocked = semantic_search_outcome(
            database_path, query, limit=limit, sidecar_path=sidecar_path
        )
        if semantic_blocked is not None:
            print(f"warning: semantic lane disabled: {semantic_blocked}", file=sys.stderr)
        else:
            semantic_ready = True
            for rank, result in enumerate(sem_results, start=1):
                _register(result.record_type, result.record_id, result.title, LANE_SEMANTIC, rank)

    # Build merged results, apply permission filter (before limit).
    final_results: list[UnifiedSearchResult] = []
    for key, data in record_data.items():
        record_type = data["record_type"]
        record_id = data["record_id"]

        if not permission_filter(record_type, record_id):
            continue

        combined_lanes = tuple(sorted(data["lanes"]))
        explain = (
            f"lanes={','.join(combined_lanes)}"
            f" fts_fresh={'yes' if fts_fresh else 'no'}"
            f" semantic_fresh={'yes' if semantic_fresh else 'no'}"
            f" semantic_ready={'yes' if semantic_ready else 'no'}"
        )
        final_results.append(
            UnifiedSearchResult(
                record_type=record_type,
                record_id=record_id,
                title=data["title"],
                score=rrf_scores[key],
                lanes=combined_lanes,
                explain=explain,
            )
        )

    # Sort by summed RRF score descending, then apply limit.
    final_results.sort(key=lambda r: rrf_scores[(r.record_type, r.record_id)], reverse=True)
    return final_results[:limit]
