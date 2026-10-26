from __future__ import annotations

"""the graph's FIRST consumer: tier-gated multi-hop expansion.

an earlier fix left two obligations for whoever read `graph_edges` first. This is that surface,
so they fall due here:

* **§8.5 — graph rows carry no sensitivity tier.** Every entity, name and fact id that leaves
  this module is admitted by `tiers.may_see` against a caller-supplied ceiling, resolved at
  expansion time from the AUTHORITATIVE rows (`entities.sensitivity_tier`, `facts`, and the
  `imported_sources` row the fact bridges through) — never from the derived edge, which carries
  no tier to trust. The gate is on TRAVERSAL, not just on output: an edge with a withheld
  endpoint is not walkable, so a withheld entity cannot leak as a path segment either (an
  output-only filter would still let a caller infer the hidden node from what it connects).
* **§8.6 — entity status policy belongs to the consumer.** `TRAVERSABLE_STATUSES` is this
  consumer's answer: `proposed` and `confirmed` traverse, `retired` does not. Retirement is the
  owner's statement that a node stopped being a thing; a retrieval lane that keeps walking it
  re-animates it. The policy is a parameter, so the next consumer states its own rather than
  inheriting this one by accident.

Freshness follows the search-index discipline (`knowledge_graph.graph_status`): a stale
projection is REFUSED, visibly (`StaleGraphError`) — never expanded over, never implicitly
rebuilt. Facts are the CURRENT generation only (`facts.CURRENT_FACT_SQL`): a consumer that
resurrects a superseded generation beside its replacement is the exact failure the
gate exists to catch.

Content-free by construction: the only strings this module selects are entity display names,
each gated by its own entity's tier. No statement, source text, or candidate span is ever
selected here — ranked ids and names are the whole output surface.
"""

import re
from dataclasses import dataclass
from pathlib import Path

from ledger_memory.db import connect_readonly
from ledger_memory.entity_resolution import normalize_alias
from ledger_memory.facts import CURRENT_FACT_SQL
from ledger_memory.knowledge_graph import graph_status
from ledger_memory.tiers import may_see

EXPANDER_VERSION = "graph-recall/1"
DEFAULT_HOPS = 2
TRAVERSABLE_STATUSES: tuple[str, ...] = ("proposed", "confirmed")

_TOKEN_RE = re.compile(r"[^\W_]+", re.UNICODE)

# The bridge an earlier fix left implicit: an entity's evidence is a candidate, a candidate belongs
# to a run over a source, and a fact points back at that source by (source_record_id, origin) —
# the same pair `about_me_recall` joins on. Selecting ids and tiers only keeps this read outside
# the content surface the reachability guard watches.
_BRIDGE_SQL = (
    "SELECT l.entity_id AS entity_id, f.id AS fact_id, "
    "f.sensitivity_tier AS fact_tier, s.sensitivity_tier AS source_tier "
    "FROM entity_record_links l "
    "JOIN extraction_candidates c ON c.id = l.record_id "
    "JOIN extraction_runs r ON r.id = c.run_id "
    "JOIN imported_sources s ON s.id = r.source_id "
    "JOIN facts f ON f.source_record_id = s.id AND f.origin = s.import_origin "
    "WHERE l.record_type = 'extraction_candidate' "
    "AND c.status IN ('proposed', 'promoted') "
    f"AND f.id IN (SELECT id FROM facts WHERE {CURRENT_FACT_SQL}) "
)


class GraphRecallError(ValueError):
    """A refused expansion — the message names the exact reason."""


class StaleGraphError(GraphRecallError):
    """The projection is stale: expansion refuses instead of answering over a stale graph."""


@dataclass(frozen=True, kw_only=True)
class ReachedEntity:
    entity_id: int
    display_name: str
    hops: int
    score: float


@dataclass(frozen=True, kw_only=True)
class Expansion:
    entities: tuple[ReachedEntity, ...]
    fact_ids: tuple[int, ...]
    seeds: tuple[int, ...]
    hops: int
    tier_ceiling: int
    version: str = EXPANDER_VERSION


def _canonical_map(connection) -> dict[int, int]:
    """{entity_id: canonical_entity_id} — one hop, resolved once per call (edges carry canonical
    ids only, so every id crossing this module resolves through here)."""
    return {int(row["id"]): int(row["canonical"]) for row in connection.execute(
        "SELECT id, COALESCE(canonical_entity_id, id) AS canonical FROM entities")}


def _visible_entities(connection, *, tier_ceiling: int,
                      status_policy: tuple[str, ...]) -> dict[int, tuple[str, int]]:
    """{canonical_entity_id: (display_name, tier)} for what this caller may see.

    Gated on the CANONICAL row's own tier and status: an absorbed member cannot widen what its
    canonical row admits, and a name is withheld with the entity rather than surfacing alone.
    """
    rows = connection.execute(
        "SELECT id, display_name, sensitivity_tier, status FROM entities "
        "WHERE canonical_entity_id IS NULL ORDER BY id").fetchall()
    visible: dict[int, tuple[str, int]] = {}
    for row in rows:
        tier = int(row["sensitivity_tier"])
        if str(row["status"]) not in status_policy or not may_see(tier, tier_ceiling):
            continue
        visible[int(row["id"])] = (str(row["display_name"]), tier)
    return visible


def seed_entities_from_text(database_path: Path, text: str, *, tier_ceiling: int,
                            status_policy: tuple[str, ...] = TRAVERSABLE_STATUSES) -> list[int]:
    """Query text → canonical entity ids, by EXACT `normalize_alias` match on token windows.

    Exact, not fuzzy: an alias lane that guesses is an entity-resolution change, and this slice
    consumes the graph rather than widening what resolves. A multi-token alias is matched
    against every same-length window of the query, so "Ana Popescu" hits inside a sentence
    without the alias ever being compared to the whole sentence.
    """
    tokens = [token.casefold() for token in _TOKEN_RE.findall(text or "")]
    if not tokens:
        return []
    with connect_readonly(database_path) as connection:
        visible = _visible_entities(connection, tier_ceiling=tier_ceiling,
                                    status_policy=status_policy)
        canonical_of = _canonical_map(connection)
        alias_rows = connection.execute(
            "SELECT entity_id, normalized_alias FROM entity_aliases "
            "ORDER BY entity_id, normalized_alias").fetchall()
    by_alias: dict[str, set[int]] = {}
    for row in alias_rows:
        canonical = canonical_of.get(int(row["entity_id"]))
        if canonical is None or canonical not in visible:
            continue
        by_alias.setdefault(normalize_alias(str(row["normalized_alias"])), set()).add(canonical)
    if not by_alias:
        return []
    widest = max(len(alias.split()) for alias in by_alias)
    windows = {" ".join(tokens[start:start + size])
               for size in range(1, min(widest, len(tokens)) + 1)
               for start in range(0, len(tokens) - size + 1)}
    hits: set[int] = set()
    for alias, entity_ids in by_alias.items():
        if alias in windows:
            hits.update(entity_ids)
    return sorted(hits)


def seed_entities_from_facts(database_path: Path, fact_ids, *, tier_ceiling: int,
                             status_policy: tuple[str, ...] = TRAVERSABLE_STATUSES) -> list[int]:
    """Retrieved fact ids → the canonical entities those facts' own sources evidence.

    The second seeding lane, and the one a first-stage retriever needs: a question can name the
    SUBJECT without naming a person ("which two people touch the Cluj move?"), where alias
    matching has nothing to bite on and the retrieved records do.
    """
    ids = sorted({int(fact_id) for fact_id in fact_ids})
    if not ids:
        return []
    placeholders = ",".join("?" for _ in ids)
    with connect_readonly(database_path) as connection:
        visible = _visible_entities(connection, tier_ceiling=tier_ceiling,
                                    status_policy=status_policy)
        canonical_of = _canonical_map(connection)
        rows = connection.execute(
            _BRIDGE_SQL + f"AND f.id IN ({placeholders})", ids).fetchall()
    hits: set[int] = set()
    for row in rows:
        if not _row_visible(row, tier_ceiling):
            continue
        canonical = canonical_of.get(int(row["entity_id"]))
        if canonical is not None and canonical in visible:
            hits.add(canonical)
    return sorted(hits)


def _row_visible(row, tier_ceiling: int) -> bool:
    """A bridged fact is visible only if BOTH its own tier and its source's admit the caller —
    the most restrictive of the two wins."""
    fact_tier = row["fact_tier"]
    if fact_tier is None or not may_see(int(fact_tier), tier_ceiling):
        return False
    source_tier = row["source_tier"]
    return source_tier is not None and may_see(int(source_tier), tier_ceiling)


def _adjacency(connection, visible: dict[int, tuple[str, int]]) -> dict[int, dict[int, float]]:
    """{a: {b: strongest weight}} over edges BOTH of whose endpoints the caller may see.

    Kinds collapse to their strongest weight: co_occurrence, temporal and name_semantic are
    three views of one adjacency, and summing them would let a single shared source count three
    times. A withheld endpoint drops the edge entirely — that is the traversal gate.
    """
    adjacency: dict[int, dict[int, float]] = {}
    for row in connection.execute(
            "SELECT entity_a_id, entity_b_id, weight FROM graph_edges "
            "ORDER BY entity_a_id, entity_b_id, edge_kind").fetchall():
        a, b, weight = int(row["entity_a_id"]), int(row["entity_b_id"]), float(row["weight"])
        if a not in visible or b not in visible:
            continue
        adjacency.setdefault(a, {})[b] = max(adjacency.setdefault(a, {}).get(b, 0.0), weight)
        adjacency.setdefault(b, {})[a] = max(adjacency.setdefault(b, {}).get(a, 0.0), weight)
    return adjacency


def _edge_strength(weight: float) -> float:
    """weight → (0, 1). Composing these multiplicatively makes a longer path strictly weaker
    than its own prefix while a heavier edge still beats a lighter one at equal depth."""
    return weight / (weight + 1.0)


def expand_query_entities(database_path: Path, seed_entity_ids, *, tier_ceiling: int,
                          hops: int = DEFAULT_HOPS,
                          status_policy: tuple[str, ...] = TRAVERSABLE_STATUSES) -> Expansion:
    """Seeds → ranked reachable entities + the current facts reachable over the bridge.

    Path score composes multiplicatively (`_edge_strength`) from a seed's 1.0; a fact scores the
    SUM of the surfaced entities that evidence it, so a record bridging two reached entities
    outranks one touching a single entity — which is what a multi-hop question asks for.
    Ordering is total and deterministic: entities by (-score, hops, id), facts by (-score, id).
    """
    if hops < 0:
        raise GraphRecallError(f"hops must be >= 0, got {hops}")
    status = graph_status(database_path)
    if not status.fresh:
        raise StaleGraphError(
            f"graph projection is stale ({status.reason}) — refusing to expand; "
            "rebuild-graph is an explicit act, never an implicit one")

    with connect_readonly(database_path) as connection:
        visible = _visible_entities(connection, tier_ceiling=tier_ceiling,
                                    status_policy=status_policy)
        canonical_of = _canonical_map(connection)
        seeds: list[int] = []
        for raw in sorted({int(seed) for seed in seed_entity_ids}):
            canonical = canonical_of.get(raw)
            if canonical is not None and canonical in visible and canonical not in seeds:
                seeds.append(canonical)

        adjacency = _adjacency(connection, visible)
        best: dict[int, tuple[float, int]] = {seed: (1.0, 0) for seed in seeds}
        frontier = list(seeds)
        for hop in range(1, hops + 1):
            improved: set[int] = set()
            for node in sorted(frontier):
                score = best[node][0]
                for neighbour in sorted(adjacency.get(node, {})):
                    candidate = score * _edge_strength(adjacency[node][neighbour])
                    current = best.get(neighbour)
                    if current is None or candidate > current[0]:
                        best[neighbour] = (candidate, hop)
                        improved.add(neighbour)
            if not improved:
                break
            frontier = sorted(improved)

        entities = tuple(
            ReachedEntity(entity_id=entity_id, display_name=visible[entity_id][0],
                          hops=meta[1], score=round(meta[0], 6))
            for entity_id, meta in sorted(best.items(),
                                          key=lambda item: (-item[1][0], item[1][1], item[0])))

        fact_ids: tuple[int, ...] = ()
        if entities:
            score_of = {entity.entity_id: entity.score for entity in entities}
            contributions: dict[int, dict[int, float]] = {}
            for row in connection.execute(_BRIDGE_SQL + "ORDER BY f.id").fetchall():
                canonical = canonical_of.get(int(row["entity_id"]))
                if canonical not in score_of or not _row_visible(row, tier_ceiling):
                    continue
                contributions.setdefault(int(row["fact_id"]), {})[canonical] = score_of[canonical]
            scored = {fact_id: round(sum(per_entity.values()), 6)
                      for fact_id, per_entity in contributions.items()}
            fact_ids = tuple(fact_id for fact_id, _ in
                             sorted(scored.items(), key=lambda item: (-item[1], item[0])))

    return Expansion(entities=entities, fact_ids=fact_ids, seeds=tuple(seeds), hops=hops,
                     tier_ceiling=tier_ceiling)


__all__ = [
    "DEFAULT_HOPS",
    "EXPANDER_VERSION",
    "Expansion",
    "GraphRecallError",
    "ReachedEntity",
    "StaleGraphError",
    "TRAVERSABLE_STATUSES",
    "expand_query_entities",
    "seed_entities_from_facts",
    "seed_entities_from_text",
]
