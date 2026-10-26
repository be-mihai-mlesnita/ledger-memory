from __future__ import annotations

"""the typed knowledge graph as a rebuildable projection.

Three deterministic builders, no model anywhere:
- `co_occurrence` — canonical-resolved entities whose links cite candidates of the same source;
  weight = number of distinct shared sources.
- `temporal` — co-occurrence restricted to sources whose timestamps fall within a window
  (default 24h; a source without a timestamp derives no temporal edge — absence is honest).
- `name_semantic` — shared normalized alias tokens (≥4 chars): the v1 LEXICAL stand-in for
  semantic edges, method-labeled so nobody mistakes it for embeddings (§8.2 — the model-backed
  variant is a later config-gated enable).

Every edge carries a `provenance` JSON receipt naming the exact rows it derives from — complete
enough that a test re-derives an edge by hand from the receipt alone. Causality is structurally
unrepresentable here (the schema CHECK refuses the kind); causal claims live as proposed,
evidenced `KIND_CAUSAL` claims in the mental-model substrate. Rebuilds follow the search-index
discipline: ledger row `running` → projection write → `completed` in the same transaction, NO
event/audit emission (the ledger is the narration — see rebuild-search-index's precedent), and
no query surface ever rebuilds implicitly.
"""

import datetime
import json
from dataclasses import dataclass
from pathlib import Path

from ledger_memory.db import connect, connect_readonly

GRAPH_BUILDER_VERSION = "knowledge-graph/1"
TEMPORAL_WINDOW_HOURS = 24


class KnowledgeGraphError(ValueError):
    """A refused graph operation — the message names the exact reason."""


@dataclass(frozen=True, kw_only=True)
class RebuildResult:
    rebuild_id: int
    scope: str
    edges: int


@dataclass(frozen=True, kw_only=True)
class GraphStatus:
    fresh: bool
    reason: str
    last_completed_at: str | None
    edge_count: int


def _entity_sources(connection) -> dict[int, dict[int, dict]]:
    """{canonical_entity_id: {source_id: {"candidates": [...], "timestamp": str|None}}} from the
    live substrate — the ONE derivation both the builder and the hand-re-derivation test share
    shape with. Canonical resolution is one hop, at build time (§8.4: merges change the graph at
    the next rebuild, visibly via graph-status, never silently). Entity STATUS is deliberately
    not filtered (disclosed): proposed/confirmed/retired all derive edges today — the graph is
    inspect-only, and a status policy belongs to the consumer gate, not silently to the
    projection."""
    rows = connection.execute(
        "SELECT l.entity_id, e.canonical_entity_id, l.record_id AS candidate_id, "
        "r.source_id, s.source_timestamp "
        "FROM entity_record_links l "
        "JOIN entities e ON e.id = l.entity_id "
        "JOIN extraction_candidates c ON c.id = l.record_id "
        "JOIN extraction_runs r ON r.id = c.run_id "
        "JOIN imported_sources s ON s.id = r.source_id "
        "WHERE l.record_type = 'extraction_candidate' "
        "AND c.status IN ('proposed', 'promoted')",
    ).fetchall()
    out: dict[int, dict[int, dict]] = {}
    for row in rows:
        canonical = int(row["canonical_entity_id"]) if row["canonical_entity_id"] is not None \
            else int(row["entity_id"])
        per_source = out.setdefault(canonical, {})
        bucket = per_source.setdefault(int(row["source_id"]),
                                       {"candidates": [], "timestamp": row["source_timestamp"]})
        bucket["candidates"].append(int(row["candidate_id"]))
    return out


def _within_window(ts_a: str | None, ts_b: str | None) -> bool:
    if not ts_a or not ts_b:
        return False
    try:
        a = datetime.datetime.fromisoformat(str(ts_a))
        b = datetime.datetime.fromisoformat(str(ts_b))
    except ValueError:
        return False
    return abs((a - b).total_seconds()) <= TEMPORAL_WINDOW_HOURS * 3600


def _alias_tokens(connection, canonical_id: int, members: list[int]) -> set[str]:
    placeholders = ",".join("?" for _ in members)
    rows = connection.execute(
        f"SELECT normalized_alias FROM entity_aliases WHERE entity_id IN ({placeholders})",
        members).fetchall()
    tokens: set[str] = set()
    for r in rows:
        tokens.update(t for t in str(r[0]).split() if len(t) >= 4)
    return tokens


def rebuild_graph(database_path: Path, *, entity_id: int | None = None) -> RebuildResult:
    """Full (or one-entity-scoped) rebuild: ledger `running` → delete-in-scope → derive →
    insert → `completed`, one transaction. Deterministic: same rows in, byte-identical edges out
    (provenance JSON is sort_keys, id-ordered). A scoped id is canonical-resolved FIRST (review
    blocker 2: edges store canonical ids only, so scoping to a merged entity's own id was a
    silent no-op reporting `completed` — the resolution is recorded in the ledger scope)."""
    with connect(database_path) as connection:
        if entity_id is not None:
            row = connection.execute(
                "SELECT canonical_entity_id FROM entities WHERE id = ?", (entity_id,)).fetchone()
            if row is None:
                raise KnowledgeGraphError(f"entity not found: {entity_id}")
            if row["canonical_entity_id"] is not None:
                entity_id = int(row["canonical_entity_id"])
        scope = "full" if entity_id is None else f"entity={entity_id}"
        watermarks = _watermarks(connection)
        cursor = connection.execute(
            "INSERT INTO graph_rebuilds (status, scope, builder_version, watermarks) "
            "VALUES ('running', ?, ?, ?)",
            (scope, GRAPH_BUILDER_VERSION, json.dumps(watermarks, sort_keys=True)))
        rebuild_id = int(cursor.lastrowid)

        if entity_id is None:
            connection.execute("DELETE FROM graph_edges")
        else:
            connection.execute(
                "DELETE FROM graph_edges WHERE entity_a_id = ? OR entity_b_id = ?",
                (entity_id, entity_id))

        by_entity = _entity_sources(connection)
        # members per canonical (for alias tokens): canonical id -> [entity ids resolving to it]
        member_rows = connection.execute(
            "SELECT id, COALESCE(canonical_entity_id, id) AS canonical FROM entities").fetchall()
        members: dict[int, list[int]] = {}
        for r in member_rows:
            members.setdefault(int(r["canonical"]), []).append(int(r["id"]))

        ids = sorted(by_entity)
        edges = 0
        for i, a in enumerate(ids):
            for b in ids[i + 1:]:
                if entity_id is not None and entity_id not in (a, b):
                    continue
                shared = sorted(set(by_entity[a]) & set(by_entity[b]))
                if shared:
                    provenance = {"builder": GRAPH_BUILDER_VERSION, "shared_sources": shared,
                                  "candidates": {str(s): sorted(set(by_entity[a][s]["candidates"])
                                                                | set(by_entity[b][s]["candidates"]))
                                                 for s in shared}}
                    connection.execute(
                        "INSERT INTO graph_edges (entity_a_id, entity_b_id, edge_kind, weight, "
                        "provenance) VALUES (?, ?, 'co_occurrence', ?, ?)",
                        (a, b, float(len(shared)), json.dumps(provenance, sort_keys=True)))
                    edges += 1
                    windowed = [
                        (sa, sb) for sa in by_entity[a] for sb in by_entity[b]
                        if _within_window(by_entity[a][sa]["timestamp"],
                                          by_entity[b][sb]["timestamp"])
                    ]
                    if windowed:
                        provenance_t = {"builder": GRAPH_BUILDER_VERSION,
                                        "window_hours": TEMPORAL_WINDOW_HOURS,
                                        "source_pairs": sorted(windowed)}
                        connection.execute(
                            "INSERT INTO graph_edges (entity_a_id, entity_b_id, edge_kind, "
                            "weight, provenance) VALUES (?, ?, 'temporal', ?, ?)",
                            (a, b, float(len(windowed)),
                             json.dumps(provenance_t, sort_keys=True)))
                        edges += 1
                tokens_a = _alias_tokens(connection, a, members.get(a, [a]))
                tokens_b = _alias_tokens(connection, b, members.get(b, [b]))
                common = sorted(tokens_a & tokens_b)
                if common:
                    provenance_s = {"builder": GRAPH_BUILDER_VERSION, "method": "name_token_overlap",
                                    "shared_tokens": common}
                    connection.execute(
                        "INSERT INTO graph_edges (entity_a_id, entity_b_id, edge_kind, weight, "
                        "provenance) VALUES (?, ?, 'name_semantic', ?, ?)",
                        (a, b, float(len(common)), json.dumps(provenance_s, sort_keys=True)))
                    edges += 1

        connection.execute(
            "UPDATE graph_rebuilds SET status = 'completed', edge_count = ?, "
            "completed_at = datetime('now') WHERE id = ?", (edges, rebuild_id))
    return RebuildResult(rebuild_id=rebuild_id, scope=scope, edges=edges)


_WATERMARK_TABLES = ("entities", "entity_aliases", "entity_record_links", "imported_sources")


def _watermarks(connection) -> dict[str, list[int]]:
    """[MAX(id), live_count] per substrate table + the entity-event high-water mark.

    Ids, not timestamps (`datetime('now')` is second-granular — the first build's hole), AND
    counts, not ids alone (review blocker 1, reproduced: a delta re-run that ONLY
    supersedes candidates flips `status` without inserting a row, so MAX(id) alone reported
    `fresh: yes` over a graph whose evidence no longer qualified). `extraction_candidates`
    counts under the builder's OWN predicate (`status IN ('proposed','promoted')`) so any flip
    that changes derivation inputs moves the mark; a proposed→promoted flip stays inside the
    live set and correctly moves nothing."""
    marks = {table: [int(connection.execute(
        f"SELECT COALESCE(MAX(id), 0) FROM {table}").fetchone()[0]),
        int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])]
        for table in _WATERMARK_TABLES}
    marks["extraction_candidates"] = [
        int(connection.execute(
            "SELECT COALESCE(MAX(id), 0) FROM extraction_candidates").fetchone()[0]),
        int(connection.execute(
            "SELECT COUNT(*) FROM extraction_candidates "
            "WHERE status IN ('proposed', 'promoted')").fetchone()[0])]
    # A merge/split flips pointers without adding substrate rows — watch those events (§8.4).
    marks["entity_events"] = [int(connection.execute(
        "SELECT COALESCE(MAX(id), 0) FROM events "
        "WHERE event_type IN ('entity.merged', 'entity.split')").fetchone()[0]), 0]
    return marks


def graph_status(database_path: Path) -> GraphStatus:
    """Fresh iff a completed FULL rebuild exists and every id-watermark it captured still equals
    the live one. Stale is a visible state, never an implicit rebuild (search-index honesty)."""
    with connect_readonly(database_path) as connection:
        last = connection.execute(
            "SELECT id, completed_at, watermarks FROM graph_rebuilds "
            "WHERE status = 'completed' AND scope = 'full' ORDER BY id DESC LIMIT 1").fetchone()
        edge_count = int(connection.execute("SELECT COUNT(*) FROM graph_edges").fetchone()[0])
        if last is None:
            return GraphStatus(fresh=False, reason="never built", last_completed_at=None,
                               edge_count=edge_count)
        recorded = json.loads(last["watermarks"] or "{}")
        live = _watermarks(connection)
    # A recorded mark of the wrong SHAPE (an old scalar format, a corrupted row) is STALE, not
    # a crash — the delta reviewer disproved the first "fail-safe" claim by feeding a pre-fix
    # scalar row to list(): TypeError. isinstance guards the comparison itself.
    def _mismatch(name: str) -> bool:
        mark = recorded.get(name)
        return not isinstance(mark, list) or list(live[name]) != list(mark)

    stale = [name if name != "entity_events" else "canonical pointers"
             for name in live if _mismatch(name)]
    if stale:
        return GraphStatus(fresh=False,
                           reason="substrate changed since last full rebuild: "
                                  + ", ".join(sorted(stale)),
                           last_completed_at=str(last["completed_at"]), edge_count=edge_count)
    return GraphStatus(fresh=True, reason="no substrate change since last full rebuild",
                       last_completed_at=str(last["completed_at"]), edge_count=edge_count)


def show_graph(database_path: Path, *, entity_id: int) -> tuple[int, list[dict]]:
    """(resolved_canonical_id, edges). Edges store canonical ids only, so the lookup resolves
    one hop first and RETURNS the resolution — a merged entity's inspect surface says "showing
    canonical N" instead of the false "isolated" (review blocker 2)."""
    with connect_readonly(database_path) as connection:
        row = connection.execute(
            "SELECT canonical_entity_id FROM entities WHERE id = ?", (entity_id,)).fetchone()
        if row is None:
            raise KnowledgeGraphError(f"entity not found: {entity_id}")
        resolved = int(row["canonical_entity_id"]) if row["canonical_entity_id"] is not None \
            else entity_id
        rows = connection.execute(
            "SELECT entity_a_id, entity_b_id, edge_kind, weight, provenance, built_at "
            "FROM graph_edges WHERE entity_a_id = ? OR entity_b_id = ? "
            "ORDER BY edge_kind, entity_a_id, entity_b_id",
            (resolved, resolved)).fetchall()
    return resolved, [dict(r) for r in rows]


__all__ = [
    "GRAPH_BUILDER_VERSION",
    "GraphStatus",
    "KnowledgeGraphError",
    "RebuildResult",
    "TEMPORAL_WINDOW_HOURS",
    "graph_status",
    "rebuild_graph",
    "show_graph",
]
