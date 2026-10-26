from ledger_memory.paths import default_vectors_path
import json
import math
import os
import struct
from collections import defaultdict
from dataclasses import dataclass, fields, is_dataclass
from pathlib import Path

from ledger_memory.presence import effective_ceiling
from ledger_memory.tiers import TIER_NAMES, TIER_SHAREABLE, may_see


@dataclass(frozen=True)
class Candidate:
    record_type: str
    record_id: int
    score: float
    lane: str
    # the mem0 explain-shape receipt: compact JSON of {lanes: {lane: {rank, rrf}},
    # primary, bonus}. Defaulted so every pre-existing constructor call (eligible_candidates,
    # v1_service, the legacy chokepoint candidate list, every prior test file) keeps working
    # unchanged — an empty-object default reads as "no fusion receipt was computed here", never as a
    # fabricated one.
    score_components: str = "{}"


def _is_str_type(field_type) -> bool:
    if field_type is str:
        return True
    if isinstance(field_type, str):
        stripped = field_type.strip("'\"")
        if stripped == "str":
            return True
        # Handle Optional[str], Union[str, ...], etc.
        if stripped.startswith(("Optional[", "Union[")) and "str" in stripped:
            return True
    return False


def carries_content(obj) -> bool:
    if not is_dataclass(obj):
        return False
    for f in fields(obj):
        if f.name in {"title", "text", "snippet", "body", "content", "statement", "summary"}:
            return True
        # `score_components` joins `record_type`/`lane` in the exclusion set. It is
        # a str field by type, but never a content field by construction — `_fuse_lane_ranks` builds
        # it entirely from lane names and `_rrf()`-derived rank/float arithmetic (see its docstring's
        # C3 rank-only guarantee); no code path ever writes a content column (`statement`, `title`,
        # ...) into it. Widening the exclusion here, rather than relaxing the detector's str-type
        # heuristic generally, keeps the guard strict for every OTHER str field while naming this one
        # specific, verified-safe exception.
        if _is_str_type(f.type) and f.name not in {"record_type", "lane", "score_components"}:
            return True
    return False


def _effective_ceiling(ceiling: int, *, present: bool) -> int:
    """Delegates to the ONE canonical clamp. Kept only as a private alias for call sites below.

    The previous body was an inline reimplementation, written on the false premise that
    `ledger_memory.presence.effective_ceiling` did not exist — it did, and was committed in the same slice.
    It computed `ceiling - (1 if present else 0)`, which is not a weaker version of the rule but an
    INVERTED one, wrong in both directions:

      * a PRESENT T2 principal was raised to T1 — a privilege escalation that handed household-tier
        callers owner-sensitive rows;
      * an ABSENT T1 principal was left at T1 — the clamp the whole presence mechanism exists to apply
        simply did not fire;
      * a present T1 principal was pushed to 0, a tier that is outside the ceiling range entirely.

    Two implementations of one security rule is the defect; a second one that is merely correct today
    would still drift tomorrow. Hence delegation, not a corrected copy.
    """
    return effective_ceiling(ceiling, present=present)


def _get_row_value(row, key, default=None):
    """Extract a value from a row that may be a dict, sqlite3.Row, or object."""
    if hasattr(row, "get"):
        return row.get(key, default)
    return getattr(row, key, default)


def _has_row_key(row, key) -> bool:
    """Check if a row has a key/attribute."""
    if hasattr(row, "get"):
        return key in row
    return hasattr(row, key)


def eligible_candidates(rows, ceiling: int, *, present: bool) -> list[Candidate]:
    eff_ceiling = _effective_ceiling(ceiling, present=present)
    filtered = []
    for row in rows:
        tier = _get_row_value(row, "sensitivity_tier")
        if tier is not None and may_see(tier, eff_ceiling):
            filtered.append(row)
    # Sort by score descending, handling missing/non-numeric scores gracefully
    def _score_key(r):
        val = _get_row_value(r, "score", 0)
        try:
            return float(val)
        except (TypeError, ValueError):
            return 0.0
    filtered.sort(key=_score_key, reverse=True)
    candidates = []
    for r in filtered:
        try:
            candidates.append(
                Candidate(
                    record_type=_get_row_value(r, "record_type"),
                    record_id=int(_get_row_value(r, "record_id")),
                    score=float(_get_row_value(r, "score")),
                    lane=_get_row_value(r, "lane"),
                )
            )
        except (KeyError, AttributeError, TypeError, ValueError):
            continue
    return candidates


# ======================================================================================================
# content-free `fact` candidate emission for the funnel's chokepoint path.
#
# `ledger_memory.unified_search` fuses LIKE + FTS + semantic lanes via RRF, but every lane it calls
# (`search_records`, `fts_search`, `semantic_search_outcome`) SELECTs a content column (`statement`,
# `title`/`body`) as part of finding a match — that read is exactly what the reachability guard's
# `CONTENT_COLUMNS` check flags, and exactly what routing `about_me_recall` through `tiered_read` is
# meant to close. Re-implementing the three lanes here, scoped to `fact` rows, keeps the SAME
# algorithm (RRF, k=60, per-lane freshness gating) recall parity depends on while
# never projecting a content column: every lane's SELECT list names `id` (or a score input) only. A
# WHERE clause may still reference `statement` to MATCH it server-side — that leaks nothing, since no
# caller ever receives the value, only whether a row matched. Content is fetched later, exactly once,
# through `tiered_read`.
# ======================================================================================================


def _rrf(rank: int, k: int = 60) -> float:
    """Same formula, same k, as `ledger_memory.unified_search._rrf` — kept identical on purpose (a different k
    here would silently re-rank the funnel relative to every other caller of the RRF idea)."""
    return 1.0 / (k + rank)


def _like_lane_fact_ids(database_path, query: str) -> list[int]:
    """The LIKE floor lane, scoped to `facts`, id-only.

    an earlier fix C4: the predicate folds both sides (`ledger_fold`, backed by `textfold.fold_for_match`)
    so it is diacritic- and case-insensitive for Romanian — measured defect: `'%inimă%'` missed
    `inima` and `'%MĂ%'` missed `Mă` under the previous byte-exact LIKE. The pattern is built from the
    FOLDED query, escaped AFTER folding (folding never introduces `%`/`_`/`\\`, but escaping a
    pre-fold query could still leave un-folded literal `%`/`_` the escape step wouldn't catch).
    """
    from ledger_memory.db import connect_readonly
    from ledger_memory.facts import CURRENT_FACT_SQL
    from ledger_memory.search import _escape_like
    from ledger_memory.textfold import fold_for_match

    pattern = f"%{_escape_like(fold_for_match(query))}%"
    with connect_readonly(database_path) as connection:  # discipline: a pure read
        connection.create_function("ledger_fold", 1, fold_for_match, deterministic=True)
        rows = connection.execute(
            f"SELECT id FROM facts WHERE {CURRENT_FACT_SQL} AND "
            "(ledger_fold(fact_type) LIKE ? ESCAPE '\\' OR ledger_fold(statement) LIKE ? ESCAPE '\\') "
            "ORDER BY id",
            (pattern, pattern),
        ).fetchall()
    return [int(row["id"]) for row in rows]


def _fts_lane_fact_ids(database_path, query: str) -> list[int]:
    """The FTS lane, scoped to `record_type = 'fact'` and id-only — mirrors `search_index.fts_search`'s
    precise-AND-then-widened-OR policy without ever selecting `title`/`body`."""
    from ledger_memory.db import connect_readonly
    from ledger_memory.search_index import _literal_fts_query, _widened_fts_query

    sql = (
        "SELECT record_id FROM search_fts WHERE search_fts MATCH ? AND record_type = 'fact' "
        "ORDER BY rank"
    )
    with connect_readonly(database_path) as connection:
        rows = connection.execute(sql, (_literal_fts_query(query),)).fetchall()
        if not rows:
            widened = _widened_fts_query(query)
            if widened is not None:
                rows = connection.execute(sql, (widened,)).fetchall()
    return [int(row["record_id"]) for row in rows]


def _semantic_lane_fact_ids(
    database_path, query: str, *, limit: int, sidecar_path
) -> list[int]:
    """Review fix (0.3.0-rc2): the semantic lane for facts, id-only. It no longer scans vectors itself
    (which skipped the per-record revision receipts); it delegates to the single validated semantic read
    (`semantic_search_outcome`), which runs the lane gate, embeds the query, binds core and sidecar
    snapshots, and verifies every vector against its per-record revision receipt and core metadata.
    A refusal there (missing sidecar, stale receipts, model mismatch, empty index, etc.) contributes
    nothing here — the caller's keyword lanes still run. We then filter to `record_type == "fact"`,
    intersect with the current fact set (one read-only transaction), preserve score order, and return
    at most `limit` ids. This function remains content-free in what it RETURNS."""
    from pathlib import Path

    from ledger_memory.db import connect_readonly
    from ledger_memory.facts import CURRENT_FACT_SQL
    from ledger_memory.semantic_search import semantic_search_outcome

    if limit <= 0:
        return []
    if sidecar_path is None:
        sidecar_path = default_vectors_path(database_path)
    else:
        sidecar_path = Path(sidecar_path)
    if not sidecar_path.exists():
        return []

    try:
        results, reason = semantic_search_outcome(  # over-fetch: results mix record types
            database_path, query, limit=limit * 4, sidecar_path=sidecar_path
        )
    except Exception:
        return []
    if reason is not None:
        return []

    try:
        with connect_readonly(database_path) as core_conn:
            core_conn.execute("BEGIN")
            try:
                valid_ids = {
                    int(row["id"])
                    for row in core_conn.execute(f"SELECT id FROM facts WHERE {CURRENT_FACT_SQL}").fetchall()
                }
            finally:
                core_conn.rollback()
    except Exception:
        return []  # never raise: the caller's keyword lanes still run

    if not valid_ids:
        return []

    fact_ids = []
    for res in results:
        if res.record_type == "fact" and res.record_id in valid_ids:
            fact_ids.append(res.record_id)
            if len(fact_ids) >= limit:
                break
    return fact_ids


# the corroboration ceiling. FIXED by the spec (the pre-implementation amendment,
# 2026-08-07: the original 0.25 was proven arithmetically incapable of satisfying the spec's own
# canonical example — see the spec's C1 section for the full derivation). Implementer tunes nothing
# beyond this constant; a dev-set result arguing for a different value is a STOP-and-report, not a
# quiet edit here.
CORROBORATION_CEILING = 0.10


def _fuse_lane_ranks(lane_ranks: dict[str, int]) -> tuple[float, str]:
    """C1's fusion arithmetic, in one place both `fact_candidates_for_query` (C1) and
    `about_me_recall._recall_via_chokepoint` (C2, via the variant-prior wrapper) can share.

    Primary score = the best lane's RRF value (CombMAX — one lane ranking a record excellently
    dominates). Corroboration bonus = `min(sum_of_other_lanes, CORROBORATION_CEILING * primary)` — a
    second (or third) lane agreeing helps, capped so mediocre-but-multi-lane agreement can never
    outrank single-lane excellence (the measured 1/61-vs-3/68 inversion this slice kills).

    Consumes RANKS only (C3) — the parameter type is `dict[str, int]`, so no raw BM25/cosine
    magnitude has a way to reach this function; a lane's absolute score, whatever its scale, never
    survives past the `enumerate(..., start=1)` that turns it into a rank in the caller.

    Returns `(score, score_components)` — `score_components` is the compact JSON receipt (mem0's
    explain shape): per-lane `{rank, rrf}`, the chosen `primary`, and the applied `bonus`.
    """
    rrf_by_lane = {lane: _rrf(rank) for lane, rank in lane_ranks.items()}
    best_lane = max(rrf_by_lane, key=lambda lane: rrf_by_lane[lane])
    primary = rrf_by_lane[best_lane]
    others_sum = sum(rrf for lane, rrf in rrf_by_lane.items() if lane != best_lane)
    bonus = min(others_sum, CORROBORATION_CEILING * primary)
    score = primary + bonus
    components = json.dumps(
        {
            "lanes": {
                lane: {"rank": lane_ranks[lane], "rrf": rrf_by_lane[lane]}
                for lane in sorted(lane_ranks)
            },
            "primary": primary,
            "bonus": bonus,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return score, components


def _mark_interleaved(components_json: str) -> str:
    """stamp `"interleaved": true` into a fusion receipt without disturbing its shape —
    an inserted member's receipt must say WHY it is in the slice (the explain contract, C1)."""
    payload = json.loads(components_json)
    payload["interleaved"] = True
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


# ======================================================================================================
# the as-known lane: retrieval AS OF a past time, physically separate from the default.
#
# Every default lane is frozen on CURRENT_FACT_SQL (facts.py:22) and on indexes built through it
# (search_index.py:1155), so a superseded fact is unreachable through all three — the property
# tests/test_search_index_workflow.py::test_non_active_facts_absent_from_all_search_lanes pins. An
# as-known question ("what was the budget as known on 2026-07-10?") needs the OPPOSITE reachability, and
# one predicate cannot be both. So the two paths share nothing but fusion arithmetic: `as_known` takes a
# different function, and `as_known=None` never executes a line of it.
# ======================================================================================================


def _as_known_lane_fact_ids(database_path, query: str, *, as_known: str) -> list[int]:
    """The as-known lexical lane: per-token folded LIKE over the facts REACHABLE at `as_known`, id-only.

    Superseded rows are absent from `search_fts` AND from the semantic sidecar (both projections apply
    CURRENT_FACT_SQL), so neither index lane can answer an as-known question at all — this lane reads
    `facts` directly or the question has no answer surface.

    Per TOKEN, not per whole query: `_like_lane_fact_ids` matches the entire folded query as ONE
    substring, which a natural-language question never is (measured on the committed graph_qa corpus:
    every temporal question returns [] through it). Tokens come from `search_index._salient_query_tokens`
    — the SAME stopword + Romanian filtering the FTS lane's terms get, so the two lanes disagree about
    time, never about language. Precise-AND first, widened-OR fallback: the policy `_fts_lane_fact_ids`
    already follows.

    `status IN ('active', 'superseded')` — the SUPERSESSION half of CURRENT_FACT_SQL is what an as-known
    question must see through; the STATUS half is not. A rejected or proposed row was never a standing
    belief at any T, so an earlier fix's "non-active facts are invisible to search" survives here intact.
    Both supersession shapes are covered: the pointer-only one `facts.supersede_fact` writes (status
    stays 'active' — 13 of 13 such rows on the live DB) and the status-carrying one `consolidation.py`
    writes.

    `ledger_norm_ts` is `temporal_view._norm` registered as a SQLite function, so this scan and `fact_view`
    normalize the separator identically before comparing. Real `known_at` values are space-separated
    ('2026-08-30 05:51:05', 728 of 728 live rows) while ISO input uses 'T', and 'T' (0x54) sorts ABOVE
    ' ' (0x20): a T-format `known_at` compared raw against a space-format `as_known` on the same day is
    excluded however early its clock time is — the review's lexical trap, in the direction
    that silently empties this lane.

    Matches `statement` only, never `fact_type`: a per-token `fact_type` LIKE would let one category word
    ('observation') match the whole table and swamp the ranking — a hazard the whole-query lane
    structurally cannot have.
    """
    from ledger_memory.db import connect_readonly
    from ledger_memory.search import _escape_like
    from ledger_memory.search_index import _salient_query_tokens
    from ledger_memory.temporal_view import _norm
    from ledger_memory.textfold import fold_for_match

    tokens = [fold_for_match(token.strip('"').replace('""', '"'))
              for token in _salient_query_tokens(query)]
    tokens = [token for token in tokens if token]
    if not tokens:  # an all-stopword question: fall back to the folded phrase, never to an empty scan
        tokens = [token for token in (fold_for_match(query),) if token]
    if not tokens:
        return []

    hits: dict[int, int] = defaultdict(int)
    with connect_readonly(database_path) as connection:  # discipline: a pure read
        connection.create_function("ledger_fold", 1, fold_for_match, deterministic=True)
        connection.create_function("ledger_norm_ts", 1, _norm, deterministic=True)
        for token in tokens:
            rows = connection.execute(
                "SELECT id FROM facts WHERE status IN ('active', 'superseded') "
                "AND ledger_norm_ts(known_at) <= ? AND ledger_fold(statement) LIKE ? ESCAPE '\\' "
                "ORDER BY id",
                (_norm(as_known), f"%{_escape_like(token)}%"),
            ).fetchall()
            for row in rows:
                hits[int(row["id"])] += 1
    ranked = sorted(hits, key=lambda record_id: (-hits[record_id], record_id))
    strict = [record_id for record_id in ranked if hits[record_id] == len(tokens)]
    return strict if strict else ranked


def _as_known_candidates(database_path, query: str, *, limit: int, as_known: str,
                         want_pool: bool) -> "list[Candidate] | tuple[list[Candidate], int]":
    """the as-known branch, whole. Two stages, each with exactly one job.

    Stage 1 (`_as_known_lane_fact_ids`) answers "which facts could be REACHED at T". Stage 2
    (`temporal_view.fact_view(as_known=T).current`) answers "which of them was the STANDING belief at T"
    — the chain member whose successor was not yet known. A member already superseded at T is dropped,
    which is what makes an as-of answer clean of later frames instead of merely enriched with earlier
    ones. `fact_view` returns ids and timepoints only, so the chokepoint stays content-free.

    Fusion is `_fuse_lane_ranks`, unchanged: one lane, so score = its RRF and bonus = 0, and the receipt
    names `as_known` — the explain shape still says exactly which lane put the record here.
    """
    from ledger_memory.temporal_view import fact_view

    admissible = {point.fact_id for point in fact_view(database_path, as_known=as_known).current}
    lane_ranks: dict[int, dict[str, int]] = {}
    for rank, record_id in enumerate(
        (candidate_id
         for candidate_id in _as_known_lane_fact_ids(database_path, query, as_known=as_known)
         if candidate_id in admissible),
        start=1,
    ):
        lane_ranks[record_id] = {"as_known": rank}

    fused: dict[int, tuple[float, str]] = {
        record_id: _fuse_lane_ranks(ranks) for record_id, ranks in lane_ranks.items()
    }
    ordered = sorted(fused, key=lambda record_id: fused[record_id][0], reverse=True)
    candidates = [
        Candidate(record_type="fact", record_id=record_id, score=fused[record_id][0],
                  lane="as_known", score_components=fused[record_id][1])
        for record_id in ordered[:limit]
    ]
    if want_pool:
        return candidates, len(fused)
    return candidates


# ======================================================================================================
# the graph lane goes live: the 4th lane, OPT-IN and content-free.
#
# built the expander (`ledger_memory.graph_recall`) and deferred the wiring. This is the wiring, and
# it is deliberately the smallest one that can be true: an additive kwarg, default OFF, so the default
# path executes exactly the lines it executed before (the committed retrieval-eval baseline cannot
# move), and a lane that emits nothing but fact IDS — seeds resolved from the query by EXACT
# `entity_aliases` match, expansion mapped to facts over the `entity_record_links` ->
# `extraction_runs` -> `imported_sources` -> `facts` bridge. Content is still read exactly once, later,
# through `tiered_read`.
#
# Three properties this lane must have that the other three do not force you to think about:
#
#   * **A stale projection degrades, visibly.** `graph_recall` refuses a stale graph by raising
#     (`StaleGraphError`) — correct for a direct caller, wrong for a retrieval lane, where an
#     exception would take down a recall that three healthy lanes could still answer. So the lane
#     catches it and returns a RECEIPT saying it did not run and why. Never silence: staleness
# measured as "no graph evidence" is exactly the dishonesty an earlier fix's refusal exists to
#     prevent.
#   * **The ceiling is the caller's, or the most restrictive one there is.** The chokepoint has NO
#     tier ceiling at candidates-time — `tiered_read` resolves it downstream from the device row plus
#     the presence clamp, which is precisely why candidate emission may name ids it will never be
# allowed to materialize. A lane that must gate its OWN traversal (an earlier fix: graph rows
#     carry no tier) therefore cannot ask the chokepoint for one. It defaults to
#     `TIER_SHAREABLE` — the most restrictive value in the lattice (`may_see` admits only T3 to a T3
#     ceiling) — so an un-parameterised call can only ever surface a SUBSET of what the caller could
#     have seen, never a superset. A caller that knows its real ceiling passes it.
#   * **The receipt carries no content.** Its `reason` is drawn from a CLOSED vocabulary of literals
#     defined here, and `detail` is either empty or the projection's own status string (table names
# and fixed phrases from `knowledge_graph`, never a store row). `GraphLaneReceipt` is not a
#     `Candidate` and never enters `tiered_read`.
# ======================================================================================================

GRAPH_LANE_EXPANDED = "expanded"
GRAPH_LANE_NO_SEEDS = "no-seed-entity-resolved-from-the-query"
GRAPH_LANE_STALE = "graph-projection-stale"
GRAPH_LANE_UNAVAILABLE = "graph-substrate-unavailable"

# CLOSED: `GraphLaneReceipt.reason` is always one of these four. A closed vocabulary is what makes the
# marker machine-checkable AND proves it can never become a channel for a row from the store.
GRAPH_LANE_REASONS: tuple[str, ...] = (
    GRAPH_LANE_EXPANDED, GRAPH_LANE_NO_SEEDS, GRAPH_LANE_STALE, GRAPH_LANE_UNAVAILABLE,
)

# The most restrictive ceiling in the lattice: `may_see(tier, TIER_SHAREABLE)` admits T3 and nothing
# else. Used when the caller states none — fail-safe in the only direction that is safe here.
DEFAULT_GRAPH_TIER_CEILING = TIER_SHAREABLE


@dataclass(frozen=True)
class GraphLaneReceipt:
    """What the graph lane did, on the face of the result. Ints, a bool, and two closed strings —
    no float, so no path score from the expander can ride out here and be mistaken for relevance."""
    ran: bool
    reason: str
    tier_ceiling: int
    hops: int
    seeds: int
    fact_ids: int
    detail: str = ""


def _graph_lane_fact_ids(database_path, query: str, *, tier_ceiling: int,
                         limit: int) -> "tuple[list[int], GraphLaneReceipt]":
    """The graph lane, id-only: query text -> seed entities -> 2-hop expansion -> current fact ids.

    Freshness is checked FIRST, mirroring how the FTS and semantic lanes are gated by their own index
    status — then the expander's own `StaleGraphError` is still caught, because the projection can go
    stale between the check and the walk and a lane must not be able to raise into recall.

    `sqlite3.Error` is caught too and reported as `graph-substrate-unavailable`: a store whose
    migrations predate the graph tables has no projection to consult, which is a degraded lane, not a
    failed answer. Nothing else is caught — a TypeError here is a defect and must stay loud.
    """
    import sqlite3

    from ledger_memory.graph_recall import (
        DEFAULT_HOPS,
        GraphRecallError,
        StaleGraphError,
        expand_query_entities,
        seed_entities_from_text,
    )
    from ledger_memory.knowledge_graph import graph_status

    def _receipt(reason: str, *, seeds: int = 0, fact_ids: int = 0,
                 detail: str = "") -> GraphLaneReceipt:
        return GraphLaneReceipt(
            ran=reason == GRAPH_LANE_EXPANDED, reason=reason, tier_ceiling=tier_ceiling,
            hops=DEFAULT_HOPS, seeds=seeds, fact_ids=fact_ids, detail=detail,
        )

    try:
        status = graph_status(database_path)
        if not status.fresh:
            return [], _receipt(GRAPH_LANE_STALE, detail=str(status.reason))
        seeds = seed_entities_from_text(database_path, query, tier_ceiling=tier_ceiling)
        if not seeds:
            return [], _receipt(GRAPH_LANE_NO_SEEDS)
        expansion = expand_query_entities(database_path, seeds, tier_ceiling=tier_ceiling,
                                          hops=DEFAULT_HOPS)
    except StaleGraphError as exc:
        return [], _receipt(GRAPH_LANE_STALE, detail=str(exc))
    except (GraphRecallError, sqlite3.Error) as exc:
        # The class name, not the message: a sqlite message is schema vocabulary today, but it is not
        # a surface this module controls, and the receipt's job is to say WHICH lane failed, not to
        # forward text from a layer below it.
        return [], _receipt(GRAPH_LANE_UNAVAILABLE, detail=type(exc).__name__)

    fact_ids = [int(fact_id) for fact_id in expansion.fact_ids[:limit]]
    return fact_ids, _receipt(GRAPH_LANE_EXPANDED, seeds=len(expansion.seeds),
                              fact_ids=len(fact_ids))


def fact_candidates_for_query(
    database_path, query: str, *, limit: int = 10, sidecar_path=None, want_pool: bool = False,
    interleave: bool = False, as_known: str | None = None, graph_expand: bool = False,
    tier_ceiling: int | None = None,
) -> ("list[Candidate] | tuple[list[Candidate], int] | tuple[list[Candidate], GraphLaneReceipt] "
      "| tuple[list[Candidate], int, GraphLaneReceipt]"):
    """Content-free replacement for `unified_search` on the funnel's chokepoint path, scoped to
    `fact` rows and fused the same way (LIKE + FTS + semantic via RRF, k=60) — an earlier fix

    Not a wrapper around `unified_search`: a wrapper that still materialises content to compute its
    ranking would defeat the entire point of the re-route. Every lane here selects `id` (or a score
    input) only; `tiered_read` is the one place `statement` is ever read on this path.

    fusion is a bounded two-axis contract, not a SUM: primary = the best lane's RRF
    (CombMAX), corroboration bonus = `min(sum_of_other_lanes, 0.10 * primary)` (`_fuse_lane_ranks`).
    The previous SUM-across-lanes shape let a mediocre multi-lane match (e.g. rank 8 in three lanes,
    3/68 ≈ 0.0441) outscore a genuinely excellent single-lane match (rank 1 in one lane, 1/61 ≈
    0.0164) — the measured inversion this rewrite kills.

    `interleave=True` is an OPT-IN guaranteed-lane-representation mode:
    every lane that registered at least one rank gets at least one representative in the final slice
    whenever `limit >=` the number of contributing lanes (review-proven pigeonhole carve-out: seating
    never evicts a sole representative, so with more lanes than slots the safer invariant wins).
    Post-processing only, deterministic: lanes are visited in the FIXED order (like, fts, semantic);
    a missing lane's best-fused member is inserted by evicting the lowest-fused member that is not
    its own lane's sole representative (never evicting a sole representative — representation cannot
    be bought by destroying representation). At most lanes-1 evictions. Inserted members carry
    `"interleaved": true` in their fusion receipt so the explain shape stays honest. The DEFAULT
    (False) path is byte-identical to the pre-1377 fusion and pinned by test — the committed
    retrieval-eval baseline stays frozen; activation anywhere is a separate, data-in-hand decision.

    `want_pool=True` additionally returns the TRUE pre-truncation pool size: the
    count of every distinct record_id that earned ANY lane rank here, counted from `fused` BEFORE the
    `ordered[:limit]` slice below. Defaulted False and additive-only (the return type only changes for
    a caller that opts in) so every pre-existing caller (`about_me_recall` pre-C4, `retrieval_eval`,
    every prior test file) keeps working unchanged.

    `as_known="<ISO timestamp>"` is an OPT-IN as-of mode and the ONLY argument that can
    reach a superseded fact. It branches to `_as_known_candidates` BEFORE a single default lane runs, so
    `as_known=None` — every pre-existing caller — executes exactly the code it executed before; neither
    path can be reached from the other. `sidecar_path` is unused there (the sidecar is built through
    CURRENT_FACT_SQL and holds no superseded row). Only as-KNOWN (transaction time) exists: `valid_from`
    is NULL on 728 of 728 real facts, so an as-valid retrieval surface would read an empty column.

    `graph_expand=True` adds a FOURTH lane, `graph`, whose ranks come
    from `ledger_memory.graph_recall` (2-hop expansion, tier-gated, retired entities excluded, ids only). It is
    additive in the `want_pool`/`interleave`/`as_known` sense and DEFAULT OFF: with the flag unset not a
    line of the lane executes and the returned candidates are byte-identical to the pre-1403 fusion, so
    the committed retrieval-eval baseline cannot move.

    `tier_ceiling` is the lane's gate and has no other consumer here, so passing it without
    `graph_expand` is refused rather than silently ignored. Unset, it defaults to
    `DEFAULT_GRAPH_TIER_CEILING` (`TIER_SHAREABLE`, the most restrictive value in the lattice): the
    chokepoint has no ceiling at candidates-time — `tiered_read` resolves the real one downstream from
    the device row plus the presence clamp — so the lane assumes the least it can and a caller that
    knows its ceiling states it. Over-permissive is impossible by default; over-restrictive only costs
    recall.

    The RETURN SHAPE grows by one trailing element per opted-in flag, in a fixed order
    `(candidates, [pool], [graph receipt])`: `graph_expand=True` always returns the
    :class:`GraphLaneReceipt` last, so a caller that asked for the lane cannot fail to see that it
    degraded (a stale projection empties the lane; it never raises into recall and is never counted as
    "no graph evidence").
    """
    if tier_ceiling is not None and not graph_expand:
        raise ValueError(
            "tier_ceiling is the graph lane's traversal gate and has no other consumer on this "
            "path: the default lanes emit ids for `tiered_read` to clamp, so accepting a ceiling "
            "here without graph_expand would promise a filter nothing applies"
        )
    graph_ceiling = DEFAULT_GRAPH_TIER_CEILING if tier_ceiling is None else int(tier_ceiling)
    if graph_expand:
        if as_known is not None:
            raise ValueError(
                "graph_expand has no meaning on the as-known path: the graph bridge is scoped to "
                "CURRENT facts (graph_recall._BRIDGE_SQL), so a graph lane there would answer an "
                "as-of question with now-frame records"
            )
        if graph_ceiling not in TIER_NAMES:
            raise ValueError(
                f"tier_ceiling must be one of {sorted(TIER_NAMES)}, got {tier_ceiling!r}"
            )

    if as_known is not None:
        if interleave:
            raise ValueError(
                "interleave has no meaning on the as-known path: it guarantees representation ACROSS "
                "the three default lanes, and this path has exactly one — accepting the flag silently "
                "would promise a guarantee nothing here can keep"
            )
        return _as_known_candidates(database_path, query, limit=limit, as_known=as_known,
                                    want_pool=want_pool)

    from ledger_memory.search_index import get_search_index_status
    from ledger_memory.semantic_index import get_semantic_index_status

    lane_ranks: dict[int, dict[str, int]] = defaultdict(dict)

    def _register(record_id: int, lane: str, rank: int) -> None:
        lane_ranks[record_id][lane] = rank

    # LIKE lane — always runs.
    for rank, record_id in enumerate(_like_lane_fact_ids(database_path, query), start=1):
        _register(record_id, "like", rank)

    # FTS lane — only when the search index is fresh, matching unified_search's gating exactly.
    if get_search_index_status(database_path).fresh:
        for rank, record_id in enumerate(_fts_lane_fact_ids(database_path, query), start=1):
            _register(record_id, "fts", rank)

    # Semantic lane — only when the semantic index is fresh, matching unified_search's gating.
    if get_semantic_index_status(database_path).fresh:
        for rank, record_id in enumerate(
            _semantic_lane_fact_ids(database_path, query, limit=limit, sidecar_path=sidecar_path),
            start=1,
        ):
            _register(record_id, "semantic", rank)

    # Graph lane — an earlier fix, opt-in. Gated on the projection's own freshness the same way the two
    # index lanes are gated on theirs; the receipt says what it did either way.
    graph_receipt: GraphLaneReceipt | None = None
    if graph_expand:
        graph_ids, graph_receipt = _graph_lane_fact_ids(
            database_path, query, tier_ceiling=graph_ceiling, limit=limit,
        )
        for rank, record_id in enumerate(graph_ids, start=1):
            _register(record_id, "graph", rank)

    fused: dict[int, tuple[float, str]] = {
        record_id: _fuse_lane_ranks(ranks) for record_id, ranks in lane_ranks.items()
    }
    ordered = sorted(fused, key=lambda record_id: fused[record_id][0], reverse=True)
    chosen = list(ordered[:limit])
    interleaved_ids: set[int] = set()
    if interleave and chosen:
        # "graph" is APPENDED, never inserted: the three pre-1403 lanes keep their exact priority,
        # and with graph_expand off no record ever carries a "graph" rank, so the loop body is a
        # no-op for it and the default path stays byte-identical.
        for lane in ("like", "fts", "semantic", "graph"):   # FIXED order: deterministic across runs
            members = [rid for rid in ordered if lane in lane_ranks[rid]]
            if not members or any(lane in lane_ranks[rid] for rid in chosen):
                continue
            best = members[0]                      # ordered is fused-descending, so first is best
            evictable = [
                rid for rid in reversed(chosen)    # lowest-fused first
                if not any(
                    other_lane in lane_ranks[rid]
                    and sum(1 for c in chosen if other_lane in lane_ranks[c]) == 1
                    for other_lane in lane_ranks[rid]
                )
            ]
            if not evictable:
                continue                           # nothing may be evicted without breaking a sole rep
            chosen[chosen.index(evictable[0])] = best
            chosen.sort(key=lambda rid: fused[rid][0], reverse=True)
            interleaved_ids.add(best)
    candidates = [
        Candidate(
            record_type="fact", record_id=record_id, score=fused[record_id][0],
            lane=",".join(sorted(lane_ranks[record_id])),
            score_components=(
                _mark_interleaved(fused[record_id][1])
                if record_id in interleaved_ids else fused[record_id][1]
            ),
        )
        for record_id in chosen
    ]
    # Fixed order, one trailing element per opted-in flag: (candidates, [pool], [graph receipt]).
    if graph_receipt is not None:
        if want_pool:
            return candidates, len(fused), graph_receipt
        return candidates, graph_receipt
    if want_pool:
        return candidates, len(fused)
    return candidates


# content-free decision candidates (ids only), authorised later by tiered_read.
def decision_candidates_for_query(database_path, query: str, *, limit: int = 20) -> list[Candidate]:
    """The FTS lane for decisions, id-only — mirrors `search_index.fts_search`'s
    precise-AND-then-widened-OR policy without ever selecting `title`/`body`."""
    from ledger_memory.db import connect_readonly
    from ledger_memory.search_index import _literal_fts_query, _widened_fts_query

    sql = (
        "SELECT record_id FROM search_fts WHERE search_fts MATCH ? AND record_type = 'decision' "
        "ORDER BY rank"
    )
    with connect_readonly(database_path) as connection:
        rows = connection.execute(sql, (_literal_fts_query(query),)).fetchall()
        if not rows:
            widened = _widened_fts_query(query)
            if widened is not None:
                rows = connection.execute(sql, (widened,)).fetchall()
    ids = list(dict.fromkeys(int(row["record_id"]) for row in rows))[:max(limit, 0)]  # review: dedupe, clamp
    return [
        Candidate(record_type="decision", record_id=i, score=1.0 / (rank + 1), lane="fts")
        for rank, i in enumerate(ids)
    ]


def decision_candidates_for_facts(database_path, fact_ids, *, ceiling: int, limit: int = 20) -> list[Candidate]:
    """Decisions linked (`decision_facts`) to the given facts, ids only.

    Review SHOULD-FIX 1: a link is itself a relationship signal, so (a) callers pass ONLY fact ids that
    `tiered_read` already returned to this reader, and (b) a link row whose own `sensitivity_tier` the reader's
    `ceiling` may not see is not followed. The decision found is still authorised separately by `tiered_read`.
    """
    from ledger_memory.db import connect_readonly
    from ledger_memory.tiers import may_see

    ids = list(dict.fromkeys(int(i) for i in fact_ids))[:MAX_LINK_FACTS]
    if not ids or limit <= 0:
        return []
    placeholders = ",".join("?" * len(ids))
    sql = (
        f"SELECT decision_id, sensitivity_tier FROM decision_facts WHERE fact_id IN ({placeholders}) "
        "ORDER BY decision_id"
    )
    with connect_readonly(database_path) as connection:
        rows = connection.execute(sql, ids).fetchall()
    found = list(dict.fromkeys(int(row["decision_id"]) for row in rows if may_see(row["sensitivity_tier"], ceiling)))
    return [Candidate(record_type="decision", record_id=i, score=1.0, lane="link") for i in found[:limit]]


# Review NIT 6: the link lane binds one parameter per fact id; cap it far below SQLite's variable limit.
MAX_LINK_FACTS = 500
