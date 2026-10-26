"""
ledger v1 Service Layer

Provides authenticated v1 API endpoints for search, context, and fact retrieval.
All functions require an authenticated context from ledger_memory.authenticated_context.
Record content is obtained ONLY via tiered_read (P0 chokepoint).
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Union

import dataclasses

from ledger_memory.client_auth import verify_device_token
from ledger_memory.instruction_shape import delimit
from ledger_memory.authenticated_context import AuthenticatedContext
from ledger_memory.facts import CURRENT_FACT_SQL
# The ONE FTS query builder. It already removes this exact defect class: FTS5 reads
# unquoted AND/OR/NOT, `*` and punctuation as SYNTAX, so a raw caller query raises OperationalError and
# the error text leaks FTS internals. Measured on this endpoint before the fix: `a-b`, `"unclosed` and
# `*` each crashed it. Quoting every token makes each one a literal and closes the class — reusing that
# rather than writing a second sanitiser, because two implementations of one rule is the defect.
from ledger_memory.search_index import _literal_fts_query
from ledger_memory.tiered_read import ENVELOPE_VERSION, tiered_read, TieredReadRefused
from ledger_memory.candidates import Candidate
from ledger_memory.search_index import rebuild_search_index
from ledger_memory.token_budget import _estimate_text_tokens

log = logging.getLogger(__name__)

# the briefing key is on unless the store's config is exactly "off"
V1_BRIEFING_KEY = "context.v1_briefing"


def _v1_briefing_enabled(db_path) -> bool:
    """Return True if v1 briefing sections should be included. Fail toward facts-only (False) on any error."""
    try:
        from ledger_memory.config import get_config
        return (get_config(Path(db_path), V1_BRIEFING_KEY, None) or "").strip() != "off"
    except Exception:
        return False


# an earlier fix / D1: this is now the ONE named production read-eligibility rule for `/v1` — not
# `memory_eligibility.is_eligible`, which v1 IMPORTED but never called (the dangling-import defect
# v2 of this slice's spec was written to correct: v1's earlier draft claimed this surface was
# unwired while it silently ran its own hand-written gate). The two rules are DELIBERATELY different,
# not accidentally: `memory_eligibility._INELIGIBLE_CLASSES` also excludes `legacy_unclassified`,
# which would newly hide roughly two-thirds of the store the day this file started calling it — a
# read-gate policy change the Non-goals section of the spec forbids ("no read-gate policy change
# beyond making the existing one honest and visible"), and a design decision (item 2) affirms:
# legacy_unclassified rows STAY visible. `test_v1_service.py::
# test_an_unclassified_row_is_emitted_labelled_not_silently_upgraded` pins exactly this. If
# `memory_eligibility`'s stricter policy is ever wanted here, that is a NEW, reviewed read-gate
# change — not a silent side effect of finally calling the import that already existed.
def _v1_read_ineligibility_reason(authority_class: str) -> str | None:
    """None if eligible for a default `/v1` read; otherwise the reason it was excluded (D2 — an
    excluded row must produce a REASON, never a bare `continue`)."""
    if authority_class == "model_inferred":
        return "model_inferred rows are excluded by default (activation is eligibility, never truth)"
    return None


class V1Refused(Exception):
    """Raised when a v1 request is refused (unauthenticated, revoked, or access denied)."""
    pass


@dataclass(frozen=True)
class V1SearchResult:
    """A single search result row emitted by v1_search."""
    fact_id: int
    content: str
    authority_class: str
    sensitivity_tier: str
    provenance: dict | None = None

    # NO `score`, and NO `lane`. An earlier version carried `score: float = field(repr=False)`, which
    # keeps the candidate tuple ON the emitted record and merely hides it from `repr()` — defeating a
    # test that greps repr while leaving the actual leak in place. The candidate tuple is stable
    # correlation material: repeated queries against stable ids reveal structure even when no content
    # is returned. It stays internal, which means it is not a field here at all.


@dataclass(frozen=True)
class V1Decision:
    """A decision shipped in a /v1 context package: title and summary only, never rationale."""
    decision_id: int
    content: str
    sensitivity_tier: int | None
    known_at: str | None
    current: bool
    superseded: bool


@dataclass(frozen=True)
class V1Contradiction:
    """Two shipped facts that disagree; ids and kind only."""
    fact_id_a: int
    fact_id_b: int
    kind: str


@dataclass(frozen=True)
class V1Interpretation:
    """An eligible mental-model claim, model inference."""
    claim_id: int
    content: str
    basis: str


@dataclass(frozen=True)
class V1ContextPackage:
    """A context package emitted by v1_context.

    an earlier fix / D4: NO package-level `authority_class`. The removed field was
    `max(f.authority_class for f in facts)` — a lexicographic `max` over class NAMES ("alphabetical,
    not epistemic", the spec's own words), which happened to order `actor_asserted < evidence_attested
    < legacy_unclassified < model_inferred < system_observed` for no reason but string sort. A2/A4
    forbid inventing a REAL ranking here in its place: `evidence_attested` (verified fidelity to a
    source) and `actor_asserted` (the owner's own word) are different DIMENSIONS, not two points on one
    scale — A4 exists precisely so "the owner said X" is never collapsed into "X is true", and a single
    scalar "which is the highest-authority fact in this package" would collapse exactly that. Each
    `V1SearchResult` in `facts` still carries its OWN honest, per-row `authority_class` — only the
    package-level aggregate (which had no consumer — grep confirms) is gone.
    the sections after the facts come from `context_compiler.briefing_sections`, the same
    builder the owner's chat briefing uses, all inside the caller's budget."""
    package_id: int
    name: str
    description: str
    facts: tuple[V1SearchResult, ...]
    created_at: str
    sensitivity_tier: str
    decisions: tuple = ()
    contradictions: tuple = ()
    interpretations: tuple = ()
    newest_known_at: str | None = None
    gaps: tuple = ()


def v1_search(
    ctx: Optional[AuthenticatedContext],
    *,
    query: str,
    limit: int = 50,
    offset: int = 0,
    min_score: float = -float('inf'),
    database_path: Optional[Union[str, Path]] = None,
) -> tuple[V1SearchResult, ...]:
    """
    Execute a full-text search against the fact store.

    Args:
        ctx: Authenticated context (enforces principal tier ceiling). Can be None for unauthenticated calls.
        query: FTS5 query string.
        limit: Maximum rows to return (ceiling enforced by principal tier).
        offset: Pagination offset.
        min_score: Minimum BM25 score threshold.
        database_path: Path to database file (required when ctx is None).

    Returns:
        Tuple of V1SearchResult, each carrying authority labels.

    Raises:
        V1Refused: If context is invalid, revoked, or access denied.
        ValueError: If neither ctx nor database_path is provided.
    """
    # Determine database path and context
    if ctx is not None:
        db_path = ctx.database_path
    elif database_path is not None:
        db_path = Path(database_path)
        # Unauthenticated calls require database_path but cannot access content
        ctx = None
    else:
        raise ValueError("Either ctx or database_path must be provided")

    # Ensure search index is built
    try:
        rebuild_search_index(db_path)
    except Exception:
        # If rebuild fails, continue anyway - the index might still be valid
        pass

    # Open connection for this request
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row

        # Fail-closed: verify context is still valid (not revoked) if authenticated
        if ctx is not None:
            # Identity from the CREDENTIAL, not from a method the caller owns. Probed: a forged context
            # is not exploitable here, because `tiered_read` re-verifies the token and is the real gate.
            # Changed anyway — this is the exact shape that WAS a live leak one layer down (a duck-typed
            # context with its own `resolve()` read T1 content), and a defence-in-depth check built on a
            # caller-owned method invites the next reader to rely on it as though it held.
            device = verify_device_token(Path(db_path), getattr(ctx, "token", "") or "")
            if device is None or device.revoked_at is not None:
                raise V1Refused("Context revoked or invalid")

        # Get effective result limit based on principal tier
        if ctx is not None:
            ceiling = _get_principal_ceiling(conn, device)
            effective_limit = min(limit, ceiling)
            if effective_limit <= 0:
                return ()
        else:
            # Unauthenticated: use a minimal default ceiling
            effective_limit = min(limit, 10)
            if effective_limit <= 0:
                return ()

        # Search for matching facts
        # The shared current-fact predicate, not a hand-rolled literal. It also excludes
        # `superseded_by_fact_id IS NOT NULL`, which the hand-rolled version missed — a fact can be
        # superseded while its status row still reads active, so the literal was letting stale rows
        # through. This is not a caller-overridable default: an `include_superseded` switch briefly
        # existed here, and a caller-flippable toggle on a correctness boundary is the same defect
        # class as an opt-in privacy clamp.
        # two corrections, both reproduced before the fix.
        # (1) `record_type = 'fact'` — without it, any FTS row of another type whose record_id collides
        #     with a fact id surfaced that unrelated fact, scored by the OTHER record's text (and a fact
        #     could appear twice). candidates.py:178 always had the predicate; this join did not.
        # (2) FTS5 bm25() is smaller-is-better (more negative = stronger). The old `ORDER BY bm25 DESC`
        #     returned weakest-first — v1_context filled the owner's context budget from the WORST 100
        #     candidates — and `bm25 >= min_score` cut strong matches while keeping weak ones. Score is
        #     now emitted NEGATED (bigger = stronger), ordered best-first, and min_score means
        #     "at least this strong". No caller passed min_score before this slice (verified), so the
        #     inert default (-inf) is unchanged in effect.
        sql = f"""
            SELECT
                f.id,
                f.authority_class,
                f.sensitivity_tier,
                f.status,
                -bm25(search_fts) AS score
            FROM search_fts
            JOIN facts f ON f.id = search_fts.record_id
            WHERE search_fts MATCH ?
              AND search_fts.record_type = 'fact'
              AND -bm25(search_fts) >= ?
              AND f.quarantined = 0
              AND {CURRENT_FACT_SQL.replace('status', 'f.status').replace('superseded_by_fact_id', 'f.superseded_by_fact_id')}
            ORDER BY bm25(search_fts) ASC
            LIMIT ? OFFSET ?
        """

        params = (
            _literal_fts_query(query),
            min_score,
            effective_limit,
            offset,
        )

        cur = conn.execute(sql, params)
        rows = cur.fetchall()

        results = []

        # For unauthenticated calls, we cannot read content
        if ctx is None:
            raise V1Refused("Unauthenticated requests cannot read content")

        # Build candidates for tiered_read
        candidates = []
        row_metadata = {}
        for row in rows:
            fact_id = row["id"]
            authority_class = row["authority_class"]
            status = row["status"]

            # Fail closed: never emit rows without authority_class
            if not authority_class:
                raise ValueError(f"Fact {fact_id} missing authority_class")

            # D2: an excluded row produces a REASON, not a bare `continue` — logged (never persisted;
            # this is a read path, see `_v1_read_ineligibility_reason`'s docstring) so the exclusion is
            # observable rather than silently discarded. legacy_unclassified rows are NOT excluded here
            # (see the module-level rule comment above) — they are always returned, carrying their true
            # label.
            ineligibility_reason = _v1_read_ineligibility_reason(authority_class)
            if ineligibility_reason is not None:
                log.debug("v1_search excluded fact %s: %s", fact_id, ineligibility_reason)
                continue

            # `Candidate` legitimately carries a score — it is the INTERNAL ranking tuple. The rule is
            # that it never reaches an emitted record, not that it never exists.
            candidates.append(Candidate(
                record_type="fact",
                record_id=fact_id,
                score=float(row["score"]),
                lane="fts",
            ))
            row_metadata[fact_id] = {
                "authority_class": authority_class,
                "sensitivity_tier": row["sensitivity_tier"],
            }

        if not candidates:
            return ()

        # Obtain content via tiered_read chokepoint
        try:
            records = tiered_read(ctx, candidates, surface="v1-context")
        except TieredReadRefused:
            # Fail closed: if tiered_read refuses, return empty
            return ()

        # Build results from tiered_read records and metadata
        for record in records:
            fact_id = record.record_id
            if fact_id not in row_metadata:
                continue

            metadata = row_metadata[fact_id]
            # Materialization is the fresh content snapshot; its authority must label that content.
            authority = record.provenance.authority_class if record.provenance is not None else metadata["authority_class"]
            if not authority:
                raise ValueError(f"Fact {fact_id} missing authority_class")
            reason = _v1_read_ineligibility_reason(authority)
            if reason is not None:
                log.debug("v1_search excluded fact %s: %s", fact_id, reason)
                continue
            results.append(V1SearchResult(
                fact_id=fact_id,
                content=record.content,
                authority_class=authority,
                sensitivity_tier=metadata["sensitivity_tier"],
                provenance={
                    "v": ENVELOPE_VERSION, **dataclasses.asdict(record.provenance),
                    "known_at": record.known_at, "current": record.current,
                    "superseded": record.superseded,
                } if record.provenance is not None else None,
            ))

        return tuple(results)


def _is_flagged(conn, fact_id: int) -> bool:
    """Read the stored instruction-shape flag. Read-only: nothing on this path may clear it."""
    row = conn.execute("SELECT instruction_shape FROM facts WHERE id = ?", (fact_id,)).fetchone()
    return bool(row and row[0])


def _tier(x) -> int | None:
    """Coerce a sensitivity tier to int, or None on failure."""
    try:
        return int(x)
    except Exception:
        return None


def v1_context(
    ctx: AuthenticatedContext,
    *,
    query: str,
    budget_tokens: int,
) -> Optional[V1ContextPackage]:
    """
    Retrieve a context package by query, building it from search results within token budget.

    Args:
        ctx: Authenticated context.
        query: Search query to build context from.
        budget_tokens: Maximum tokens for the context package.

    Returns:
        V1ContextPackage if found and accessible, None otherwise.

    Raises:
        V1Refused: If context is invalid, revoked, or access denied.
    """
    db_path = ctx.database_path

    # Open connection for this request
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row

        # Fail-closed: verify context is still valid (not revoked)
        # Identity from the CREDENTIAL, not from a method the caller owns. Probed: a forged context
        # here is not exploitable, because `tiered_read` re-verifies the token and is the real gate.
        # Changed anyway — this is the exact shape that WAS a live leak one layer down (a duck-typed
        # context with its own `resolve()` read T1 content), and leaving a defence-in-depth check built
        # on a caller-owned method invites the next reader to rely on it as though it held.
        device = verify_device_token(Path(db_path), getattr(ctx, "token", "") or "")
        if device is None or device.revoked_at is not None:
            raise V1Refused("Context revoked or invalid")

        # Search for relevant facts
        search_results = v1_search(
            ctx,
            query=query,
            limit=100,  # Fetch more to fill budget
        )

        if not search_results:
            return None

        # Build package from search results within token budget. each line costs the shared estimator's count
        # + 1, the briefing's per-line rule, not chars / 4 + 50.
        facts = []
        token_count = 0
        for result in search_results:
            content = delimit(result.content, flagged=_is_flagged(conn, result.fact_id))
            cost = _estimate_text_tokens(content) + 1
            if token_count + cost > budget_tokens:
                break
            # an earlier fix / F1: every recalled statement is delimited as quoted data before it can
            # reach a model. NOT only the flagged ones — a model cannot tell which rows were flagged, so
            # delimiting only the suspicious ones would teach a reader that undelimited text is
            # trustworthy. The flag adds a tag; it does not decide whether to quote.
            facts.append(dataclasses.replace(
                result,
                content=content,
            ))
            token_count += cost

        if not facts:
            return None

        # Create a synthetic package ID (negative to indicate ad-hoc)
        package_id = -1

        # newest_known_at from shipped facts
        newest_known_at = None
        for f in facts:
            known_at = (f.provenance or {}).get("known_at")  # the date rides in provenance
            if known_at:
                candidate = str(known_at)[:10]
                if newest_known_at is None or candidate > newest_known_at:
                    newest_known_at = candidate

        gaps = []

        # fetch briefing sections if enabled
        decisions = []
        contradictions = []
        interpretations = []
        interpretation_tiers = []

        # Review NIT: with no budget left the sections could only be read, audited and dropped, so they are skipped
        # (and named under gaps, never silently).
        briefing_on = _v1_briefing_enabled(db_path)
        if briefing_on and token_count >= budget_tokens:
            gaps.append("sections omitted: budget")
        if token_count < budget_tokens and briefing_on:
            try:
                from ledger_memory.context_compiler import briefing_sections

                @dataclass(frozen=True)
                class _Shipped:
                    record_id: int
                    sensitivity_tier: int | None

                shipped = [_Shipped(f.fact_id, _tier(f.sensitivity_tier)) for f in facts]
                sections = briefing_sections(ctx, db_path, query, surface="v1-context", shipping=shipped)

                # Degraded sections become gaps
                for section, exc_name in sections.degraded:
                    gaps.append(f"{section} unavailable")

                # Fit decisions
                for d in sections.decisions:
                    content = delimit(d.content)
                    cost = _estimate_text_tokens(content) + 1
                    if token_count + cost > budget_tokens:
                        if "decisions omitted: budget" not in gaps:
                            gaps.append("decisions omitted: budget")
                        break
                    decisions.append(V1Decision(
                        decision_id=d.record_id,
                        content=content,
                        sensitivity_tier=d.sensitivity_tier,
                        known_at=d.known_at or None,
                        current=bool(d.current),
                        superseded=bool(d.superseded),
                    ))
                    token_count += cost

                # Fit contradictions
                for a, b, kind in sections.conflict_pairs:
                    cost = _estimate_text_tokens(f"{a} {b} {kind}") + 1
                    if token_count + cost > budget_tokens:
                        if "contradictions omitted: budget" not in gaps:
                            gaps.append("contradictions omitted: budget")
                        break
                    contradictions.append(V1Contradiction(a, b, kind))
                    token_count += cost

                # Fit interpretations
                from ledger_memory.mental_model_provenance import format_basis_profile
                for v in sections.claims:
                    basis = format_basis_profile(v.basis_profile) if v.basis_profile is not None else "{model inference}"
                    content = delimit(v.text)
                    cost = _estimate_text_tokens(content + " " + basis) + 1
                    if token_count + cost > budget_tokens:
                        if "interpretations omitted: budget" not in gaps:
                            gaps.append("interpretations omitted: budget")
                        break
                    interpretations.append(V1Interpretation(
                        claim_id=v.claim_id,
                        content=content,
                        basis=basis,
                    ))
                    interpretation_tiers.append(v.sensitivity_tier)
                    token_count += cost

            except Exception:
                gaps.append("sections unavailable")

        # Package sensitivity = the MOST RESTRICTIVE member fact. In the tiers.py lattice LOWER
        # means MORE sensitive (T0 secret < T1 < T2 < T3 shareable; `may_see` is
        # `row_tier >= ceiling`), so most-restrictive is min — an earlier fix: the previous max
        # under a "most restrictive" comment labeled a T1+T3 package shareable (3), the same
        # inversion 1073 §3c and 1074 §3.1 fixed in the mental-model substrate. Nothing in ledger
        # gates on this label (the facts were tiered_read-authorized upstream), but it ships to
        # external MCP consumers via brain_context, and a consumer trusting it for onward
        # handling would have over-shared T1 content. NOT authority — see D4's note on
        # `V1ContextPackage`: there is no single "highest authority" across a mixed set of
        # `evidence_attested`/`actor_asserted` facts, so this package no longer claims one.
        # decisions and interpretations count too — a package is as sensitive as anything it ships.
        all_tiers = [_tier(f.sensitivity_tier) for f in facts]
        all_tiers.extend([d.sensitivity_tier for d in decisions])
        all_tiers.extend(interpretation_tiers)
        valid_tiers = [t for t in all_tiers if t is not None]
        # Same type as before this slice (the raw stored tier); never a string, never a made-up fallback.
        package_sensitivity = min(valid_tiers) if valid_tiers else min(f.sensitivity_tier for f in facts)

        return V1ContextPackage(
            package_id=package_id,
            name=f"Context for: {query}",
            description=f"Auto-generated context package from query '{query}' with {len(facts)} facts",
            facts=tuple(facts),
            created_at=datetime.now(timezone.utc).isoformat(),
            sensitivity_tier=package_sensitivity,
            decisions=tuple(decisions),
            contradictions=tuple(contradictions),
            interpretations=tuple(interpretations),
            newest_known_at=newest_known_at,
            gaps=tuple(gaps),
        )


# the honest constant `_get_principal_ceiling` always returned in practice. Its
# tier-to-limit map was dead code — `ClientDevice` deliberately carries no `tier_ceiling`
# attribute (an earlier fix keeps ceilings in `device_tier_ceilings` precisely so no duck-typed
# attribute read exists), so the hasattr branch never ran — and the map itself confused TIER
# units with RESULT-LIMIT units. Simplifying preserves
# observable behavior exactly; choosing a different limit is a product decision, not this fix's.
_V1_RESULT_CEILING = 50


def _get_principal_ceiling(conn: sqlite3.Connection, device) -> int:
    """The authenticated result limit — a constant. Tier enforcement lives upstream in the
    authorized read path, never in a result count."""
    return _V1_RESULT_CEILING


__all__ = [
    "V1Refused",
    "V1SearchResult",
    "V1Decision",
    "V1Contradiction",
    "V1Interpretation",
    "V1ContextPackage",
    "v1_search",
    "v1_context",
]
