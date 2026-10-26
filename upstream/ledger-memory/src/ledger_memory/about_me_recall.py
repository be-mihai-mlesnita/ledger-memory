"""About-me recall — J2 / an earlier fix: the store-first retrieval step of the answer path.

Enforces the owner's binding `store-first-answer` rule (methodology/rules.toml, 2026-07-02): any ledger agent that can
pull from the store MUST do so BEFORE composing an answer to the owner. `run_converse` calls
`build_brain_context` before generation; the result is either a dated, cited memory block or an EXPLICIT
"no memory found / store unavailable" line — the model is never left to answer from working context alone silently.

Design: retrieval = the existing unified search (LIKE + FTS + semantic) pointed at the store;
supersession-aware assembly (`superseded_by_fact_id` → history; exact-duplicate statements collapse to the newest
`known_at`, older copies become dated history); every memory carries its date + source conversation. Read-only over
the store — answering never mutates it.
"""
from __future__ import annotations

import os
import re
import traceback
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from ledger_memory.claim_envelope import EvidenceManifest, ManifestRow, content_sha256
from ledger_memory.config import get_config
from ledger_memory.context_tool import (
    BOUNDED,
    DEFAULT_MAX_READS,
    MAX_QUERY_CHARS,
    MAX_RECORDS_PER_READ,
    OFF,
    ManifestEntry,
    ToolReadRequest,
    ToolReadResult,
    clamp_max_reads,
    parse_mode,
)
from ledger_memory.db import connect_readonly
from ledger_memory.paths import default_vectors_path
from ledger_memory.interaction_authorization import PURPOSE_TOOL_READ_T1, peek_tool_allowance
from ledger_memory.tiered_read import (
    SOURCE_WITHHELD,
    SUPERSEDED_MARKER,
    _one_line,
    _SOURCE_CAP,
    _STATEMENT_CAP,
    Provenance,
    _provenance_inputs,
    _provenance_from_row,
    read_audit_capable,
    record_read_audit,
)
from ledger_memory.tiers import may_see
from ledger_memory.unified_search import unified_search


NO_MEMORY_LINE = "RECALLED MEMORY: no relevant memory found for this question (the store was searched)."
UNAVAILABLE_LINE = (
    "RECALLED MEMORY: the memory store is not reachable right now — say so if the owner asks about "
    "something you would normally remember; do NOT invent memories."
)
_DEFAULT_LIMIT = 8

# variant priors, FIXED by the spec. The raw query is the caller's own words (full
# trust); `_keywords(query)` fan-out variants are a deterministic derivative of it, corroborating but
# never equal to a direct match — 0.7 discounts them before they compete with the raw-query score.
_RAW_QUERY_VARIANT_PRIOR = 1.0
_KEYWORD_VARIANT_PRIOR = 0.7


@dataclass
class Memory:
    record_id: int
    statement: str
    known_at: str | None
    source: str | None          # the conversation/source label the memory came from (provenance)
    current: bool = True        # False = superseded / an older duplicate kept as history
    source_file: str | None = None  # the actual archive file (run-3 contract: Jon cites files)
    # carried so the manifest row built from this Memory can name a real tier —
    # defaulted so no prior constructor call (there were several before this slice) breaks.
    sensitivity_tier: int | None = None
    provenance: Provenance | None = None


@dataclass
class AboutMeContext:
    found: bool
    available: bool
    memories: list[Memory] = field(default_factory=list)
    context_block: str = UNAVAILABLE_LINE
    # the evidence manifest for whatever shipped in `context_block`: empty when no
    # memory shipped (nothing to cite). `build_converse_messages` carries this to validation.
    manifest: EvidenceManifest = field(default_factory=EvidenceManifest)




def resolve_brain_paths(database_path: Path) -> tuple[Path, Path] | None:
    """The store (db, semantic vectors) for this runtime: always the package's own database and its vectors file."""
    database_path = Path(database_path)
    return database_path, default_vectors_path(database_path)


def resolve_funnel_database(database_path: Path) -> Path:
    """C1 — the ONE database the funnel actually authenticates against, for a given
    runtime database: the configured store when `resolve_brain_paths` resolves one, else
    `database_path` itself (so a dev-only install, a fresh clone, and the test suite — none of
    which configure `store.db_path` — keep reading their own database exactly as before).

    `doctor` (`_compute_runtime_principal`, `_readiness_runtime_credential_check`) and
    `client_auth.refresh_runtime_presence` all call this (local import — the codebase's existing
    idiom for avoiding a cycle back into this module) so a green gauge can never again mean a
    DIFFERENT database than `_resolve_runtime_context` reads — the exact split defect 2 measured:
    the funnel verified against the store while every gauge verified against the runtime db."""
    resolved = resolve_brain_paths(database_path)
    return resolved[0] if resolved is not None else database_path


# one set, defined in `search_index` and imported here. The literal FTS lane (arm C) is
# specified as "drops the stopwords `_keywords` already drops" — two lists would let that definition
# quietly become false the first time either was edited.
#
# C2: `_STOPWORDS_RO` joins it, imported the same way and for the same reason (the
# Romanian stopword list `search_index._salient_query_tokens` filters against must be the SAME list
# `_keywords` drops, never a second copy that could drift). It is a SEPARATE frozenset, not merged
# into `_STOPWORDS` — see `search_index.py` for the cross-language collision rationale.
from ledger_memory.search_index import _STOPWORDS, _STOPWORDS_RO

# C2: Unicode-word-aware token pattern (was ASCII-only `[A-Za-z][A-Za-z0-9'-]{2,}`,
# measured to shatter Romanian diacritic words into fragments — `'sănătate țară înghețată'` ->
# `['tate', 'nghe']`). `[^\W\d_]` matches one Unicode letter (word char minus digit minus
# underscore); the continuation group repeats a Unicode letter-or-digit (`[^\W_]`) or an explicit
# apostrophe/hyphen, keeping the SAME 3-char-minimum-token shape (first char + 2 more) the ASCII
# pattern had, generalized to any script Python's Unicode-aware `\w` recognizes.
_KEYWORD_TOKEN = re.compile(r"[^\W\d_](?:[^\W_]|['-]){2,}")


def _keywords(message: str, max_terms: int = 4) -> list[str]:
    """Salient content terms of a natural-language question. The FTS/LIKE lanes are phrase-literal, so a raw question
    only recalls via the semantic lane — per-keyword fan-out makes retrieval robust in every lane."""
    from ledger_memory.textfold import looks_romanian

    romanian = looks_romanian(message)  # review fix 2026-08-07: RO stopwords never fire on English
    seen: list[str] = []
    for tok in _KEYWORD_TOKEN.findall(message.lower()):
        if tok in _STOPWORDS or (romanian and tok in _STOPWORDS_RO) or tok in seen:
            continue
        seen.append(tok)
    return sorted(seen, key=len, reverse=True)[:max_terms]


# an earlier fix (C1/C2/C4) — the indexed memory block + evidence manifest, shared by both recall
# paths so the numbering/hashing rule can never drift between them. --------------------------------

_MEMORY_HEADER = "RECALLED MEMORY (dated; cite the date AND the bracketed [index] when you use one):"

PROVENANCE_LEGEND = (
    "Basis tags describe stored provenance, not truth: attested = evidence-attested claim; "
    "stated: NAME = actor assertion; inferred = model inference; observed = system observation; "
    "unclassified = missing or unclassified authority; imported = stored import marker; "
    "interpretation = durable interpretation; projection = rebuildable projection; ev = "
    "attestation state (verified, rejected, unverified, none, or withheld). A memory of a "
    "promise, a plan or a prediction is evidence that it was SAID, not that it happened."
)


def _basis_tag(provenance: Provenance | None) -> str:
    if provenance is None:
        return "{unclassified; ev: none}"
    authority = {"evidence_attested": "attested", "model_inferred": "inferred",
                 "system_observed": "observed"}.get(provenance.authority_class, "unclassified")
    if provenance.authority_class == "actor_asserted":
        authority = f"stated: {provenance.asserted_by or 'someone'}"
    parts = [authority]
    if provenance.imported:
        parts.append("imported")
    materialization = {"durable_interpretation": "interpretation", "rebuildable_projection": "projection"}.get(provenance.materialization)
    if materialization:
        parts.append(materialization)
    parts.append(f"ev: {provenance.attestation}")
    return "{" + "; ".join(parts) + "}"


def _render_indexed_block(memories: list[Memory]) -> tuple[list[str], EvidenceManifest]:
    """Numbers each memory with its menu index (C2: "the model is told to cite indices ONLY") and
    builds the matching :class:`EvidenceManifest` from the SAME shipped (capped/one-lined) text the
    lines below render — `content_sha256` is a receipt over what the model could literally see, not
    the raw database row (C1: "emitted AFTER all truncation from what actually shipped")."""
    lines: list[str] = []
    rows: list[ManifestRow] = []
    for idx, m in enumerate(memories):
        date = (m.known_at or "undated")[:10]
        source = _one_line(m.source, _SOURCE_CAP) if m.source else ""
        src = f" — from: {source}" if source else ""
        if m.source_file:
            src += f" — file: {_one_line(m.source_file, _SOURCE_CAP)}"
        marker = "" if m.current else f" {SUPERSEDED_MARKER}"
        statement = _one_line(m.statement, _STATEMENT_CAP)
        lines.append(f"- [{idx}] [{date}] {statement}{src}{marker} {_basis_tag(m.provenance)}")
        rows.append(ManifestRow(
            index=str(idx), record_id=m.record_id, sensitivity_tier=m.sensitivity_tier,
            known_at=m.known_at, source_label=m.source, superseded=not m.current,
            content_sha256=content_sha256(statement),
        ))
    return lines, EvidenceManifest(tuple(rows))


def _omission_line(pool: int, shipped: int, *, exact: bool) -> str | None:
    """R3.3 — the honest-omission line: appended ONLY when the pool genuinely exceeds what shipped.
    `exact=False` (more than one query variant contributed to `pool`, so it may double-count a record
    found by more than one variant) labels the count as a lower bound, per the spec's own wording;
    `exact=True` (a single variant, already internally deduplicated) states it plainly."""
    omitted = pool - shipped
    if omitted <= 0:
        return None
    qualifier = "" if exact else "at least "
    return f"({qualifier}{omitted} additional retrieved candidates were omitted)"


def _resolve_runtime_context(brain_path: Path):
    """resolve the funnel's runtime credential (the local reader's token file) and build the
    authenticated context `tiered_read` will re-verify. None when unpaired (fresh clone, or a store
    this device's token was never redeemed against) — a capability gate on a REAL credential, never a
    caller-steerable flag (C2). The context's `database_path` is `brain_path` itself: `tiered_read`
    resolves the device AND fetches content from the same database, so the runtime device must be
    paired into whichever database is serving as the store (see `runtime-pair --db <that path>`)."""
    from ledger_memory.reader import reader_context

    return reader_context(brain_path)


def recall_about_me(brain_path: Path, query: str, *, limit: int | None = _DEFAULT_LIMIT,
                    sidecar_path: Path | None = None, surface: str | None = None) -> AboutMeContext:
    """Brain-first retrieval + supersession-aware assembly (read-only over the store).

    the lane switch. When the funnel's runtime credential is paired AND verifies against
    THIS store, retrieval goes through the one chokepoint: content-free candidate emission
    (`ledger_memory.candidates`) → `tiered_read` materialization under that credential. `tiered_read` writes
    the ONE audit row for the read itself (real `device_id` + `surface`) — this path never calls
    `record_read_audit` a second time (§2.4: one row per read, not two), and never calls
    `unified_search` (protected file; stays untouched, still serves every other caller).

    When no runtime credential exists (C2 — degraded mode, visible never silent: `doctor` reports
    `funnel: UNCLAMPED (no runtime principal)`), recall falls back to `_recall_legacy_unclamped`, the
    original an earlier fix implementation verbatim: `unified_search` + the NULL-device audit row.

    Args:
        surface: where this read originated ('api-chat', 'telegram', 'voice', 'v1-context').
                 None records 'unknown' — a valid value, never an error.
    """
    context = _resolve_runtime_context(brain_path)
    if limit is None:
        # the briefing's larger fetch only on the authenticated path, where the
        # budget trims it; the unpaired legacy path keeps its 8.
        limit = _DEFAULT_LIMIT
        if context is not None:
            from ledger_memory.context_compiler import FACT_FETCH_LIMIT, briefing_enabled
            try:
                limit = FACT_FETCH_LIMIT if briefing_enabled(brain_path) else _DEFAULT_LIMIT
            except Exception:
                limit = _DEFAULT_LIMIT
    if context is not None:
        return _recall_via_chokepoint(
            context, brain_path, query, limit=limit, sidecar_path=sidecar_path, surface=surface,
        )
    return _recall_legacy_unclamped(
        brain_path, query, limit=limit, sidecar_path=sidecar_path, surface=surface,
    )


def _fused_fact_candidates(brain_path: Path, query: str, *, limit: int,
                           sidecar_path: Path | None) -> tuple[list, int, int]:
    """Multi-variant, score-fused fact candidates for one query, shared by the
    chokepoint recall and the model-callable tool read. Returns
    ``(candidates, max_variant_pool, union_size)``; the caller computes its own pool lower bound from those.
    """
    from ledger_memory.candidates import CORROBORATION_CEILING, Candidate, fact_candidates_for_query

    variants: list[tuple[str, float]] = [(query, _RAW_QUERY_VARIANT_PRIOR)]
    variants.extend((kw, _KEYWORD_VARIANT_PRIOR) for kw in _keywords(query))

    # an earlier fix (C4), corrected by review BLOCK (2026-08-07): a SUM of per-variant pools is an
    # UPPER bound on the true union size — "at least <sum>" was mathematically false. The honest
    # LOWER bound "at least" requires is max(largest single-variant pool, distinct candidates
    # actually returned across variants): each is provably ≤ the true union. A single-variant
    # query still gets the exact figure.
    max_variant_pool = 0
    per_record_scores: dict[int, list[float]] = defaultdict(list)
    for variant_text, prior in variants:
        variant_candidates = fact_candidates_for_query(
            brain_path, variant_text, limit=limit * 2, sidecar_path=sidecar_path, want_pool=True
        )
        # A monkeypatched fake (pre-C4 test doubles) may still return a plain list — tolerate both
        # shapes rather than forcing every existing fixture to grow a pool-size return.
        if isinstance(variant_candidates, tuple):
            variant_candidates, variant_pool = variant_candidates
        else:
            variant_pool = len(variant_candidates)
        max_variant_pool = max(max_variant_pool, variant_pool)
        for candidate in variant_candidates:
            per_record_scores[candidate.record_id].append(candidate.score * prior)

    fused_score: dict[int, float] = {}
    for record_id, weighted_scores in per_record_scores.items():
        weighted_scores.sort(reverse=True)
        primary = weighted_scores[0]
        others_sum = sum(weighted_scores[1:])
        bonus = min(others_sum, CORROBORATION_CEILING * primary)
        fused_score[record_id] = primary + bonus

    ordered_ids = sorted(fused_score, key=lambda rid: fused_score[rid], reverse=True)
    candidates = [
        Candidate(record_type="fact", record_id=rid, score=fused_score[rid], lane="funnel")
        for rid in ordered_ids
    ]
    return candidates, max_variant_pool, len(per_record_scores)


def _recall_via_chokepoint(context, brain_path: Path, query: str, *, limit: int,
                           sidecar_path: Path | None, surface: str | None) -> AboutMeContext:
    """The paired-runtime-credential path.
    Candidate ids come from `ledger_memory.candidates.fact_candidates_for_query` (content-free, multi-lane, the
    SAME per-keyword fan-out `_recall_legacy_unclamped` uses for recall parity); content AND
    provenance come ONLY from `tiered_read`, which applies the tier ceiling, the presence clamp, gates
    each source title on its OWN authorization, and writes the one audit row this read gets — real
    `device_id`, this `surface`, no caller-supplied ceiling or count.

    A `tiered_read.Record` now carries `known_at`, `source_label`, and `current`/`superseded` (the
    B1 closure — an earlier fix's "surface-passthrough is the only sanctioned change" restriction was
    that slice's own scope statement, not standing law; an earlier fix supersedes it explicitly). A source
    title ships only when the `imported_sources` row itself clears the ceiling — every such row sits
    at the fail-closed default tier today, so a low ceiling sees `[source withheld]` broadly; that
    recall-quality cliff is accepted here deliberately (a future backfill slice, not this one).
    `source_file` still stays None on this path (invariant 4 — raw paths need an explicit authorized
    detail read, never this context block).

    variant fusion is a bounded two-axis contract, mirroring C1's lane fusion
    shape rather than the previous first-seen-order-discards-scores concatenation. Each query
    variant (the raw query, prior 1.0; each `_keywords` fan-out term, prior 0.7) keeps its OWN
    `fact_candidates_for_query` score (itself already a C1-fused, rank-derived value — never a raw
    BM25/cosine magnitude, so scaling it by a prior stays C3-safe: no cross-scale mixing happens
    here, only the same kind of number against itself). A record's fused primary is its best
    (variant-score * prior) across the variants it appears in; a corroboration bonus,
    `min(sum_of_other_variants, CORROBORATION_CEILING * primary)`, applies when more than one
    variant found it — the SAME capped-bonus shape as C1's `_fuse_lane_ranks`, applied on the
    variant axis instead of the lane axis. The full union is scored BEFORE any truncation — no
    per-variant or intermediate pool cap beyond each variant's own `limit * 2` over-fetch — so a
    record found only by a low-prior variant, or only deep in one variant's own ranking, still
    competes for the global top-`limit` on its fused score; `tiered_read` then receives candidates
    in that fused order (funnel semantics — the `len(memories) >= limit` cutoff below — otherwise
    untouched).
    """
    from ledger_memory.context_compiler import briefing_enabled, compile_briefing
    from ledger_memory.interaction_authorization import peek_current_proof
    from ledger_memory.tiered_read import tiered_read

    # the briefing is on by default; `context.briefing = off` restores the old block.
    # This function honours its caller's `limit`; the larger fetch the briefing trims is asked for by
    # build_brain_context, the entry every answer uses.
    try:
        briefing = briefing_enabled(brain_path)
    except Exception:  # review N2: a config read error must not cost the answer its memory block
        briefing = False
    effective_limit = limit

    candidates, max_variant_pool, union_size = _fused_fact_candidates(
        brain_path, query, limit=effective_limit, sidecar_path=sidecar_path,
    )

    # stage 1362a — the proof rides BESIDE the credential, never replacing it (Part B1: two
    # independent dimensions). `peek_current_proof` reads whatever `client_api`'s /api/chat handler
    # bound for THIS request (a `ContextVar`, so no new parameter had to thread through `converse.py` /
    # `build_brain_context` / `recall_about_me`, none of which has any other reason to know a proof
    # exists); it is None for every caller that does not mint one yet (chat, voice — the pinned
    # "absent proof -> legacy clamp, byte-identical" branch tiered_read itself implements).
    interaction_proof = peek_current_proof()

    # tiered_read writes exactly one audit row here, empty candidates included (a zero-hit query is
    # still a read that happened) — this call is this recall's ENTIRE audit trail (§2.4).
    records = tiered_read(
        context, candidates, database_path=brain_path, surface=surface, interaction_proof=interaction_proof,
    )

    if not records:
        return AboutMeContext(found=False, available=True, context_block=NO_MEMORY_LINE)

    memories: list[Memory] = []
    for record in records:
        statement = (record.content or "").strip()
        if not statement:
            continue
        memories.append(Memory(record_id=record.record_id, statement=statement,
                               known_at=record.known_at, source=record.source_label,
                               current=record.current, source_file=None,
                               sensitivity_tier=record.sensitivity_tier, provenance=record.provenance))
        if len(memories) >= effective_limit:
            break

    if not memories:
        return AboutMeContext(found=False, available=True, context_block=NO_MEMORY_LINE)

    body, manifest = _render_indexed_block(memories)
    # the honest-omission line: real numbers on this path (`total_pool` is measured
    # from `fact_candidates_for_query`'s own rrf_scores, never estimated).
    pool_lower_bound = max(max_variant_pool, union_size)
    if briefing:
        try:
            block, briefing_manifest = compile_briefing(
                context, brain_path, query, surface=surface, header=_MEMORY_HEADER,
                fact_lines=body, fact_rows=list(manifest.rows), shipped_facts=memories,
                pool_lower_bound=pool_lower_bound, exact_pool=not _keywords(query),
            )
        except Exception:
            # Review B1: never lose the facts. A briefing that fails falls through to today's plain fact block below,
            # at today's size (the briefing's larger fetch is for the budget to trim, not for the plain block).
            briefing = False
            memories = memories[:min(limit, _DEFAULT_LIMIT)]
            body, manifest = _render_indexed_block(memories)
    if briefing:
        shipped_ids = {row.record_id for row in briefing_manifest.rows if row.record_type == "fact"}
        return AboutMeContext(found=True, available=True,
                              memories=[m for m in memories if m.record_id in shipped_ids],
                              context_block=block, manifest=briefing_manifest)
    lines = [_MEMORY_HEADER, *body]
    omission = _omission_line(pool_lower_bound, len(memories), exact=not _keywords(query))
    if omission:
        lines.append(omission)

    return AboutMeContext(found=True, available=True, memories=memories,
                          context_block="\n".join(lines), manifest=manifest)


def _recall_legacy_unclamped(brain_path: Path, query: str, *, limit: int = _DEFAULT_LIMIT,
                             sidecar_path: Path | None = None,
                             surface: str | None = None) -> AboutMeContext:
    try:
        return _recall_legacy_materialized(
            brain_path, query, limit=limit, sidecar_path=sidecar_path, surface=surface,
        )
    except BaseException as error:
        # The legacy audit happens after assembly. Sever completed frames on any failure,
        # including helpers, before stored attribution or partial memories can escape.
        traceback.clear_frames(error.__traceback__)
        raise


def _recall_legacy_materialized(brain_path: Path, query: str, *, limit: int,
                                sidecar_path: Path | None, surface: str | None) -> AboutMeContext:
    """an earlier fix's original implementation, kept as the C2 degraded-mode fallback (unchanged apart from the
    an earlier fix provenance envelope it now carries): no
    runtime credential exists, so recall is unclamped by tier/presence and self-audits with a
    NULL device id — the funnel's principal is a channel, not a paired device, on this path.

    every invocation audits itself into the SAME database it reads — one
    `read_audit_entries` row, committed before any content is returned, including zero-hit reads
    (a query that found nothing is still a read that happened). The audit is capability-gated on
    the schema (the baseline schema's), NOT on a caller-supplied path — an audit a caller can decline by
    omitting a parameter is an audit that does not exist.

    Args:
        surface: where this read originated ('api-chat', 'telegram', 'voice', 'v1-context').
                 None records 'unknown' — a valid value, never an error.
    """
    audit_capable = read_audit_capable(brain_path)

    # an earlier fix / R2.0 C3 (closes Phase A finding B1 on this path too): the legacy path has no
    # per-caller ceiling — it is the C2 UNCLAMPED fallback by design (the function's own name says
    # so). It is not, however, a path with NO ceiling concept at all: `0` is the literal value its
    # own audit row already records below (the "unclamped legacy default" the spec names), so the
    # `conversation_title` gate uses that same value rather than inventing a new one. `may_see(tier,
    # 0)` excludes only T0 — every `imported_sources` row minted so far sits at the fail-closed
    # DEFAULT tier of 1, so this closes the literal "no tier predicate at all" leak B1 found without
    # narrowing today's recall (a T0-classified source is the only row this newly withholds).
    _LEGACY_CEILING = 0

    def _audit(record_count: int) -> None:
        # Fail closed: any failure here propagates, so no content escapes unaudited (C1). A missing
        # 048 schema is the ONE sanctioned skip (C2), decided by the probe above — never by
        # catching an exception, because a locked database must stay a failure rather than
        # impersonate a missing table.
        if audit_capable:
            record_read_audit(brain_path, device_id=None, record_count=record_count,
                              ceiling=_LEGACY_CEILING, surface=surface or "unknown")

    fact_ids: list[int] = []
    for q in [query, *_keywords(query)]:
        for h in unified_search(brain_path, q, limit=limit * 2, sidecar_path=sidecar_path):
            if h.record_type == "fact" and h.record_id not in fact_ids:
                fact_ids.append(h.record_id)
    if not fact_ids:
        _audit(0)
        return AboutMeContext(found=False, available=True, context_block=NO_MEMORY_LINE)

    placeholders = ",".join("?" * len(fact_ids))
    with connect_readonly(brain_path) as conn:
        rows = conn.execute(
            f"""SELECT f.id, f.statement, f.known_at, f.superseded_by_fact_id,
                       f.authority_class, f.materialization, f.asserted_by_actor_id,
                       f.content_author, f.confidence, f.valid_from, f.valid_to, f.source_record_id,
                       f.origin, f.source_db, f.sensitivity_tier AS fact_tier,
                       s.id AS matched_source_id,
                       s.conversation_title AS conversation_title,
                       s.sensitivity_tier AS source_tier,
                       s.source_file AS source_file
                FROM facts f
                LEFT JOIN imported_sources s
                       ON s.id = f.source_record_id AND s.import_origin = f.origin
                WHERE f.id IN ({placeholders})""",
            fact_ids,
        ).fetchall()
        actors, evidence = _provenance_inputs(conn, rows)

    def _legacy_source_label(row) -> str | None:
        # B1's exact predicate gap, closed: `conversation_title` crosses the same table boundary here
        # as it does at the chokepoint (tiered_read._materialize), and gets the SAME may_see gate —
        # never a fallback to the (already-visible) source_db/origin on withhold, which would leak a
        # proxy signal about the very thing being withheld.
        title = row["conversation_title"]
        if title:
            source_tier = row["source_tier"]
            if source_tier is not None and may_see(source_tier, _LEGACY_CEILING):
                return title
            return SOURCE_WITHHELD
        return row["source_db"] or row["origin"]

    def _legacy_source_file(row) -> str | None:
        # the SAME may_see gate as the title above, applied to
        # `source_file` — a failing row ships no file fragment at all (not a placeholder; the
        # title's SOURCE_WITHHELD already signals withholding once). When it passes, only the
        # basename ever reaches model/client context (R2.0 invariant 4) — never the raw path,
        # which can carry directory structure (people, diagnoses, project names).
        raw = row["source_file"]
        if not raw:
            return None
        source_tier = row["source_tier"]
        if source_tier is None or not may_see(source_tier, _LEGACY_CEILING):
            return None
        return _one_line(Path(raw).name, _SOURCE_CAP)

    by_id = {r["id"]: r for r in rows}
    ordered = [by_id[i] for i in fact_ids if i in by_id]

    # Supersession-aware assembly: an explicit superseded_by pointer → history; exact-duplicate statements collapse
    # to the newest ACTIVE copy (older/superseded copies → dated history). Never silently dropped (§4.3).
    # known_at arrives in two formats (native `YYYY-MM-DD HH:MM:SS` vs imported
    # `YYYY-MM-DDT…+00:00`) — raw string comparison makes `T` beat `space`; normalize before comparing (both UTC).
    def _norm_ts(ka: str) -> str:
        return ka.replace("T", " ")[:19]

    # only NON-superseded copies compete for "current" — a superseded copy holding the newest
    # timestamp must not demote the genuinely-active duplicate to history.
    newest_for: dict[str, str] = {}
    for r in ordered:
        key = (r["statement"] or "").strip()
        ka = _norm_ts(r["known_at"] or "")
        if key and r["superseded_by_fact_id"] is None and ka > newest_for.get(key, ""):
            newest_for[key] = ka
    memories: list[Memory] = []
    seen_statements: set[tuple[str, bool]] = set()
    for r in ordered:
        key = (r["statement"] or "").strip()
        if not key:
            continue
        current = (r["superseded_by_fact_id"] is None
                   and _norm_ts(r["known_at"] or "") == newest_for.get(key, "\x00"))
        if (key, current) in seen_statements:
            continue  # one current + at most one history line per statement
        seen_statements.add((key, current))
        memories.append(Memory(record_id=r["id"], statement=key, known_at=r["known_at"],
                               source=_legacy_source_label(r), current=current,
                               source_file=_legacy_source_file(r), sensitivity_tier=r["fact_tier"],
                               provenance=_provenance_from_row(
                                   r, actors, evidence, withheld=r["matched_source_id"] is not None
                                   and not may_see(r["source_tier"], _LEGACY_CEILING),
                               )))
        if len(memories) >= limit:
            break

    body, manifest = _render_indexed_block(memories)
    lines = [_MEMORY_HEADER, *body]
    # this path's own `unified_search` fusion is PROTECTED (never edited); it does
    # not expose a pre-truncation pool size the way `fact_candidates_for_query` does, so this path
    # cannot measure real omission and never appends the R3.3 line — a documented undercount (the
    # legacy/unpaired path silently under-reports "how much was omitted" as zero) rather than a
    # fabricated or guessed figure.

    _audit(len(memories))
    return AboutMeContext(found=True, available=True, memories=memories,
                          context_block="\n".join(lines), manifest=manifest)


# the model-callable context tool's settings (store config; absent = off) and its ledger authorizer.
TOOL_READS_KEY = "context.tool_reads"
TOOL_READS_MAX_KEY = "context.tool_reads_max"
TOOL_READS_OUTWARD_KEY = "context.tool_reads_allow_with_outward"
TOOL_READS_T1_KEY = "context.tool_reads_t1"
TOOL_READ_TIER_FLOOR = None   # the single local reader sees every tier; a stale presence still clamps (an allowance lifts it)


@dataclass(frozen=True)
class ToolReadSettings:
    mode: str
    max_reads: int
    allow_with_outward: bool


def tool_read_settings(database_path) -> ToolReadSettings:
    """The tool's settings from the BRAIN's config. Absent store or any error = off. Never raises."""
    off = ToolReadSettings(OFF, DEFAULT_MAX_READS, False)
    try:
        resolved = resolve_brain_paths(Path(database_path))
        if resolved is None:
            return off
        store_db = resolved[0]
        raw_mode = get_config(store_db, TOOL_READS_KEY, None)
        raw_max = get_config(store_db, TOOL_READS_MAX_KEY, None)
        raw_outward = get_config(store_db, TOOL_READS_OUTWARD_KEY, None)
        return ToolReadSettings(parse_mode(raw_mode), clamp_max_reads(raw_max), raw_outward == "yes")
    except Exception:
        return off


def tool_read_t1_enabled(database_path) -> bool:
    """the owner's switch for T1 tool reads. True only when the tool is `bounded` AND the store
    config value of `context.tool_reads_t1` is exactly `on`. Any error = False. Never raises."""
    try:
        if tool_read_settings(database_path).mode != BOUNDED:
            return False
        resolved = resolve_brain_paths(Path(database_path))
        if resolved is None:
            return False
        return get_config(resolved[0], TOOL_READS_T1_KEY, None) == "on"
    except Exception:
        return False


class _ToolReadAuthorizer:
    """ledger's ToolReadAuthorizer: identity, tier ceiling, presence and audit come from `tiered_read` under the
    paired runtime credential. Never falls back to the legacy unclamped path. Never peeks, mints or consumes a
    proof (interaction_proof=None)."""

    def __init__(self, database_path, *, surface: str):
        self._database_path = Path(database_path)
        self._surface = surface

    def authorize(self, request: ToolReadRequest) -> ToolReadResult:
        try:
            resolved = resolve_brain_paths(self._database_path)
            if resolved is None:
                return ToolReadResult((), (), refused_reason="no store available")
            store_db, sidecar = resolved
            context = _resolve_runtime_context(store_db)
            if context is None:
                return ToolReadResult((), (), refused_reason="no authenticated reader")

            from ledger_memory.tiered_read import tiered_read

            # the same multi-variant fusion the chokepoint uses, so a multi-word model query
            # gets the per-keyword fan-out a raw phrase-literal lane cannot provide.
            candidates, _, _ = _fused_fact_candidates(
                store_db, request.query[:MAX_QUERY_CHARS], limit=MAX_RECORDS_PER_READ, sidecar_path=sidecar,
            )
            candidates = list(candidates)[:MAX_RECORDS_PER_READ]

            # a live per-reply allowance (minted by the ingress that verified the owner) lifts the floor
            # for THIS read only; the authorizer never mints, binds, consumes or evicts anything itself.
            allowance = peek_tool_allowance()
            if allowance is not None and tool_read_t1_enabled(self._database_path):
                records = tiered_read(
                    context, candidates, database_path=store_db, surface=f"{self._surface}:tool",
                    interaction_proof=allowance, proof_purpose=PURPOSE_TOOL_READ_T1, tier_floor=None,
                )
            else:
                records = tiered_read(
                    context, candidates, database_path=store_db, surface=f"{self._surface}:tool",
                    interaction_proof=None, tier_floor=TOOL_READ_TIER_FLOOR,
                )

            memories: list[Memory] = []
            for record in records:
                statement = (record.content or "").strip()
                if not statement:
                    continue
                memories.append(Memory(record_id=record.record_id, statement=statement,
                                       known_at=record.known_at, source=record.source_label,
                                       current=record.current, source_file=None,
                                       sensitivity_tier=record.sensitivity_tier, provenance=record.provenance))
                if len(memories) >= MAX_RECORDS_PER_READ:
                    break

            # Pair each shipped line with its manifest row by the row's own index. Header and non-indexed
            # lines are never returned; an ambiguous or missing pairing refuses the whole read.
            block_lines, manifest = _render_indexed_block(memories)
            lines: list[str] = []
            entries: list[ManifestEntry] = []
            for row in manifest.rows:
                prefix = f"- [{row.index}] "
                matches = [line for line in block_lines if line.startswith(prefix)]
                if len(matches) != 1:
                    raise ValueError("manifest row could not be paired with exactly one rendered line")
                lines.append(matches[0][len(prefix):])
                entries.append(ManifestEntry(index="r?", record_type="fact", record_id=row.record_id, payload=row))

            withheld_private = len(records) < len(candidates)
            return ToolReadResult(tuple(lines), tuple(entries), withheld_private=withheld_private)
        except Exception:
            # Never put content (or the exception text) into the refusal reason.
            return ToolReadResult((), (), refused_reason="the read was refused")


def tool_read_authorizer(database_path, *, surface: str) -> _ToolReadAuthorizer:
    return _ToolReadAuthorizer(database_path, surface=surface)


def build_brain_context(database_path: Path, message: str, *, limit: int | None = None,
                        surface: str | None = None) -> AboutMeContext:
    """The converse-facing mandatory pre-step. ALWAYS returns an explicit context block: memories, an explicit
    no-memory line, or an explicit store-unavailable line — never a silent skip (the store-first-answer rule).

    Args:
        database_path: Path to the runtime database (for audit logging).
        message: The search query/message.
        limit: Maximum number of memories to return; None (the default) means the briefing's larger fetch
               (FACT_FETCH_LIMIT, trimmed by its budget) when the briefing is on, else 8.
        surface: The surface/origin label (e.g., 'api-chat', 'telegram', 'voice', 'v1-context').
    """
    resolved = resolve_brain_paths(Path(database_path))
    if resolved is None:
        return AboutMeContext(found=False, available=False, context_block=UNAVAILABLE_LINE)
    store_db, sidecar = resolved
    # limit None lets recall_about_me pick the briefing's larger fetch on the authenticated path.
    try:
        return recall_about_me(store_db, message, limit=limit, sidecar_path=sidecar, surface=surface)
    except Exception as exc:
        # Fail EXPLICIT to the model AND to the operator: the unavailable line keeps the answer
        # path alive, while a runtime-DB event + stderr line make outages/corruption/regressions visible instead
        # of masquerading as "no store configured". Telemetry itself is best-effort — never break the answer over it.
        import sys as _sys
        detail = f"{type(exc).__name__}: {exc}"[:200]
        print(f"about_me_recall: store recall FAILED — {detail}", file=_sys.stderr)
        try:
            from ledger_memory.db import connect as _connect
            from ledger_memory.emission import emit_event_only as _emit
            with _connect(Path(database_path)) as connection:
                _emit(connection, actor_id=1, event_type="brain_recall.error",
                      summary=f"store-first recall failed; converse got the unavailable line ({detail})")
        except Exception:
            pass
        return AboutMeContext(found=False, available=False, context_block=UNAVAILABLE_LINE)
