"""
Context briefing compiler. Implements the briefing assembly per decisions 321, 327, 328, 330.
Safety rules: every record comes through authorised read (tiered_read) or eligibility check; nothing is written.
Non-obvious rules: link-lane ceiling uses min tier among shipped facts (lower number = more private); conflict.detail
is never used (only kind); header, first fact, and newest-date line are reserved; sections degrade gracefully (skip
lines that don't fit, continue to next section); omission line appended last if it fits.
"""

from dataclasses import dataclass

from ledger_memory.claim_envelope import EvidenceManifest, ManifestRow, content_sha256
from ledger_memory.config import get_config
from ledger_memory.token_budget import _estimate_text_tokens
from ledger_memory.tiered_read import tiered_read, _one_line, _STATEMENT_CAP
from ledger_memory.candidates import decision_candidates_for_facts, decision_candidates_for_query
from ledger_memory.temporal_view import conflicts
from ledger_memory.tiers import may_see
from ledger_memory.mental_model_eligibility import eligible_claims, has_active_versions
from ledger_memory.mental_model_provenance import format_basis_profile  # review B1: was called but never imported

SWITCH_KEY = "context.briefing"
BUDGET_KEY = "context.briefing_budget_tokens"
DEFAULT_BUDGET = 1500
MIN_BUDGET = 200
FACT_FETCH_LIMIT = 24
DECISION_CAP = 12
DECISIONS_HEADER = "DECISIONS (the owner's recorded decisions; cite [dN]):"
CONFLICTS_HEADER = "CONTRADICTIONS (cite both indices; neither is settled):"
INTERPRETATIONS_HEADER = "INTERPRETATIONS (model inference, never fact; cite [iN]):"
GAPS_HEADER = "GAPS:"


def briefing_enabled(database_path) -> bool:
    return (get_config(database_path, SWITCH_KEY, "on") or "on").strip().lower() != "off"


def briefing_budget(database_path) -> int:
    val = get_config(database_path, BUDGET_KEY, None)
    try:
        budget = int(val) if val is not None else DEFAULT_BUDGET
    except (ValueError, TypeError):
        budget = DEFAULT_BUDGET
    return max(budget, MIN_BUDGET)


def render_decision_line(n: int, record) -> str:
    date = (record.known_at or "undated")[:10]
    text = _one_line(record.content, _STATEMENT_CAP)
    if record.superseded:
        state = "superseded"
    elif record.current:
        state = "in force"
    else:
        state = "rejected"
    return f"- [d{n}] [{date}] {text} — {state}"


def render_conflict_line(i: str, j: str, kind: str) -> str:
    return f"- facts [{i}] and [{j}] disagree ({_one_line(kind, 40)})"


def render_interpretation_line(n: int, verdict) -> str:
    text = _one_line(verdict.text or "", _STATEMENT_CAP)
    if verdict.basis_profile is not None:
        suffix = format_basis_profile(verdict.basis_profile)
    else:
        suffix = "{model inference}"
    return f"- [i{n}] {text} {suffix}"


def render_newest_date_line(facts) -> str | None:
    dates = [(f.known_at or "")[:10] for f in facts if f.known_at]
    if not dates:
        return None
    newest = max(dates)
    return f"- the newest record shown is from {newest}"


def assemble_briefing(
    *,
    header,
    fact_lines,
    fact_rows,
    shipped_facts,
    decisions,
    conflict_pairs,
    claims,
    pool_lower_bound,
    exact_pool,
    budget_tokens,
    degraded=(),
) -> tuple[str, EvidenceManifest]:
    # Reserved lines (owner, 2026-10-19: the newest date ALWAYS shows): the header, the first fact line and, when any
    # fact has a date, the GAPS header + newest-date line. Their cost is set aside FIRST; everything else is fitted into
    # what remains, so no later line can push a reserved one out. The date line has a fixed shape, so its cost is known
    # before the shipped set is final.
    def line_cost(line):
        return _estimate_text_tokens(line) + 1

    lines = [header]
    used = line_cost(header)
    shipped_fact_indices = []
    shipped_fact_rows = []
    shipped_fact_objects = []
    if fact_lines:
        lines.append(fact_lines[0])
        used += line_cost(fact_lines[0])
        shipped_fact_indices.append(0)
        shipped_fact_rows.append(fact_rows[0])
        shipped_fact_objects.append(shipped_facts[0])
    gaps_reserved = 0
    if any(f.known_at for f in shipped_facts):
        gaps_reserved = line_cost(GAPS_HEADER) + line_cost("- the newest record shown is from 0000-00-00")
    limit = budget_tokens - gaps_reserved

    # Remaining facts, in rank order, while they fit.
    for idx in range(1, len(fact_lines)):
        line = fact_lines[idx]
        if used + line_cost(line) <= limit:
            lines.append(line)
            used += line_cost(line)
            shipped_fact_indices.append(idx)
            shipped_fact_rows.append(fact_rows[idx])
            shipped_fact_objects.append(shipped_facts[idx])
        else:
            break

    newest_date_line = render_newest_date_line(shipped_fact_objects)

    # Build shipped fact id -> index mapping
    shipped_fact_id_to_index = {f.record_id: fact_rows[i].index for i, f in zip(shipped_fact_indices, shipped_fact_objects)}

    manifest_rows = list(shipped_fact_rows)

    # Decisions section
    decision_lines = []
    for n, rec in enumerate(decisions):
        line = render_decision_line(n, rec)
        decision_lines.append((line, rec))

    if decision_lines:
        header_cost = _estimate_text_tokens(DECISIONS_HEADER) + 1
        if used + header_cost + sum(_estimate_text_tokens(l) + 1 for l, _ in decision_lines[:1]) <= limit:
            lines.append(DECISIONS_HEADER)
            used += header_cost
            for shipped_n, (line, rec) in enumerate(decision_lines):
                cost = _estimate_text_tokens(line) + 1
                if used + cost <= limit:
                    lines.append(line)
                    used += cost
                    # Add to manifest
                    content_for_hash = _one_line(rec.content, _STATEMENT_CAP)
                    manifest_rows.append(ManifestRow(
                        index=f"d{shipped_n}",
                        record_id=rec.record_id,
                        sensitivity_tier=rec.sensitivity_tier,
                        known_at=rec.known_at,
                        source_label=None,
                        superseded=rec.superseded,
                        content_sha256=content_sha256(content_for_hash),
                        record_type="decision"
                    ))
                else:
                    break

    # Conflicts section
    conflict_lines = []
    for fact_a_id, fact_b_id, kind in conflict_pairs:
        if fact_a_id in shipped_fact_id_to_index and fact_b_id in shipped_fact_id_to_index:
            i = shipped_fact_id_to_index[fact_a_id]
            j = shipped_fact_id_to_index[fact_b_id]
            line = render_conflict_line(i, j, kind)
            conflict_lines.append(line)

    if conflict_lines:
        header_cost = _estimate_text_tokens(CONFLICTS_HEADER) + 1
        if used + header_cost + sum(_estimate_text_tokens(l) + 1 for l in conflict_lines[:1]) <= limit:
            lines.append(CONFLICTS_HEADER)
            used += header_cost
            for line in conflict_lines:
                cost = _estimate_text_tokens(line) + 1
                if used + cost <= limit:
                    lines.append(line)
                    used += cost
                else:
                    break

    # Interpretations section. Review B1: rendering a claim must never take the briefing down; a claim that
    # cannot be rendered is skipped and the section is named under GAPS.
    interpretation_lines = []
    for verdict in claims:
        try:
            line = render_interpretation_line(len(interpretation_lines), verdict)
        except Exception as exc:
            degraded = tuple(degraded) + (("interpretations", type(exc).__name__),)
            continue
        interpretation_lines.append((line, verdict))

    if interpretation_lines:
        header_cost = _estimate_text_tokens(INTERPRETATIONS_HEADER) + 1
        if used + header_cost + sum(_estimate_text_tokens(l) + 1 for l, _ in interpretation_lines[:1]) <= limit:
            lines.append(INTERPRETATIONS_HEADER)
            used += header_cost
            for shipped_n, (line, verdict) in enumerate(interpretation_lines):
                cost = _estimate_text_tokens(line) + 1
                if used + cost <= limit:
                    lines.append(line)
                    used += cost
                    # Add to manifest
                    content_for_hash = _one_line(verdict.text or "", _STATEMENT_CAP)
                    manifest_rows.append(ManifestRow(
                        index=f"i{shipped_n}",
                        record_id=int(verdict.claim_id),
                        sensitivity_tier=verdict.sensitivity_tier,
                        known_at=None,
                        source_label=None,
                        superseded=False,
                        content_sha256=content_sha256(content_for_hash),
                        record_type="interpretation"
                    ))
                else:
                    break

    # Reserved: always shipped when a shipped fact has a date (its cost was set aside above).
    gap_lines = [newest_date_line] if newest_date_line else []
    header_paid = bool(newest_date_line)  # the date line's reservation already paid for GAPS_HEADER
    for section, error in dict.fromkeys(degraded):  # degraded sections are named (visible, never silent) when they fit
        line = f"- {section} unavailable this time ({_one_line(error, 40)})"
        extra = 0 if header_paid else line_cost(GAPS_HEADER)  # review N1: count the header once, when first needed
        if used + extra + line_cost(line) <= limit:
            gap_lines.append(line)
            used += extra + line_cost(line)
            header_paid = True
    if gap_lines:
        lines.append(GAPS_HEADER)
        lines.extend(gap_lines)

    # Omission line
    shipped_fact_count = len(shipped_fact_objects)
    omitted = max(pool_lower_bound, len(fact_lines)) - shipped_fact_count
    if omitted > 0:
        prefix = "" if exact_pool else "at least "
        omission_line = f"({prefix}{omitted} additional retrieved candidates were omitted)"
        if used + line_cost(omission_line) <= limit:
            lines.append(omission_line)

    return "\n".join(lines), EvidenceManifest(tuple(manifest_rows))


@dataclass(frozen=True)
class BriefingSections:
    """the sections after the facts, built once for every surface (converse and `/v1`)."""

    decisions: list
    conflict_pairs: list
    claims: list
    degraded: tuple


def briefing_sections(context, database_path, query, *, surface, shipping) -> BriefingSections:
    """Decisions, contradictions and interpretations for the facts that SHIP (`shipping`: records with `record_id` and
    `sensitivity_tier`). Each section degrades to a named entry in `degraded`, never an exception. Moved verbatim out
    of `compile_briefing` so `/v1` context uses the same rules instead of a second copy."""
    degraded = []
    shipping_ids = {f.record_id for f in shipping}

    # Decisions. The link lane's ceiling is the MOST private tier among facts tiered_read already returned to this
    # reader: the reader provably may see that tier, so the lane is never more permissive than the real ceiling.
    decisions = []
    try:
        candidates = []
        tiers = [f.sensitivity_tier for f in shipping if f.sensitivity_tier is not None]
        if shipping and tiers:
            candidates += decision_candidates_for_facts(
                database_path, [f.record_id for f in shipping], ceiling=min(tiers))
        candidates += decision_candidates_for_query(database_path, query)
        seen = set()
        unique = []
        for c in candidates:
            if c.record_id not in seen:
                seen.add(c.record_id)
                unique.append(c)
        unique = unique[:DECISION_CAP]
        if unique:
            decisions = [r for r in tiered_read(context, unique, surface=surface) if r.record_type == "decision"]
    except Exception as exc:
        decisions = []
        degraded.append(("decisions", type(exc).__name__))

    # Contradictions: `conflicts` reads every fact WITHOUT tier checks and its `detail` can carry statement text, so only
    # pairs whose BOTH facts were returned to this reader are kept, and only ids and the kind are used, never `detail`.
    conflict_pairs = []
    try:
        shown_ids = shipping_ids
        for c in conflicts(database_path):
            if c.fact_a in shown_ids and c.fact_b in shown_ids:
                conflict_pairs.append((c.fact_a, c.fact_b, c.kind))
    except Exception as exc:
        conflict_pairs = []
        degraded.append(("contradictions", type(exc).__name__))

    # Claims
    claims = []
    try:
        if has_active_versions(database_path):
            all_claims = eligible_claims(database_path, context, query_scope=query, surface=surface)
            claims = [v for v in all_claims if v.eligible and v.text and v.claim_id is not None]
            # review S2: eligibility proves only that the claim's EVIDENCE is readable; the claim row has its
            # own tier. Ship a claim only when that tier is known and no more private than the most private fact this
            # reader was provably shown (the link lane's rule); otherwise drop it. Fail-closed, for chat and /v1 alike.
            shown_tiers = [f.sensitivity_tier for f in shipping if f.sensitivity_tier is not None]
            floor = min(shown_tiers) if shown_tiers else None
            claims = [v for v in claims
                      if floor is not None and v.sensitivity_tier is not None and may_see(v.sensitivity_tier, floor)]
    except Exception as exc:
        claims = []
        degraded.append(("interpretations", type(exc).__name__))

    return BriefingSections(decisions=decisions, conflict_pairs=conflict_pairs, claims=claims, degraded=tuple(degraded))


def compile_briefing(
    context,
    database_path,
    query,
    *,
    surface,
    header,
    fact_lines,
    fact_rows,
    shipped_facts,
    pool_lower_bound,
    exact_pool,
) -> tuple[str, EvidenceManifest]:
    budget = briefing_budget(database_path)

    degraded = []  # visible, never silent: a section that fails says so under GAPS, and the answer goes on

    # Review S3: the decision lanes and their ceiling use only the facts that will SHIP, so fit the facts first (an
    # assembly with no extras; the extras only ever come after the facts, so the shipped fact set cannot change).
    _text, facts_only = assemble_briefing(
        header=header, fact_lines=fact_lines, fact_rows=fact_rows, shipped_facts=shipped_facts, decisions=[],
        conflict_pairs=[], claims=[], pool_lower_bound=pool_lower_bound, exact_pool=exact_pool, budget_tokens=budget,
    )
    shipping_ids = {r.record_id for r in facts_only.rows if r.record_type == "fact"}
    shipping = [f for f in shipped_facts if f.record_id in shipping_ids]

    sections = briefing_sections(context, database_path, query, surface=surface, shipping=shipping)
    decisions, conflict_pairs, claims = sections.decisions, sections.conflict_pairs, sections.claims
    degraded.extend(sections.degraded)

    return assemble_briefing(
        header=header,
        fact_lines=fact_lines,
        fact_rows=fact_rows,
        shipped_facts=shipped_facts,
        decisions=decisions,
        conflict_pairs=conflict_pairs,
        claims=claims,
        pool_lower_bound=pool_lower_bound,
        exact_pool=exact_pool,
        budget_tokens=budget,
        degraded=tuple(degraded),
    )
