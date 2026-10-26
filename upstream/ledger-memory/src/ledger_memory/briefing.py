"""The budgeted briefing behind the `brief` tool.

One user question in, one structured briefing out: the facts that best answer it (found by the whole-question search
plus one search per keyword, fused), the decisions linked to them (title and summary only, never the rationale), any
contradiction between two facts that are both shown, the interpretations that are eligible, the date of the newest
record shown, and the gaps (what was left out and why). Every record is read through `tiered_read` under the local
reader; the decisions, contradictions and interpretations come from the shared `briefing_sections` builder, so this
surface and any other apply identical rules.

Cost accounting: items are first chosen greedily (each costs the shared token estimator's count + 1), then the
WHOLE returned JSON payload is estimated with the same estimator and trimmed, lowest-priority items first
(interpretations, contradictions, decisions, then trailing facts), until it fits the budget. `tokens_estimated` is that
whole-payload estimate. Budgets below MIN_BUDGET are raised to MIN_BUDGET by the caller.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from ledger_memory.about_me_recall import _fused_fact_candidates
from ledger_memory.context_compiler import FACT_FETCH_LIMIT, briefing_sections
from ledger_memory.instruction_shape import delimit
from ledger_memory.mental_model_provenance import format_basis_profile
from ledger_memory.tiered_read import tiered_read
from ledger_memory.token_budget import _estimate_text_tokens

SURFACE = "mcp"
MIN_BUDGET = 200
MAX_BUDGET = 20000


def _cost(text: str) -> int:
    return _estimate_text_tokens(text) + 1


def _flagged(database_path: Path, fact_id: int) -> bool:
    con = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True)
    try:
        row = con.execute("SELECT instruction_shape FROM facts WHERE id = ?", (fact_id,)).fetchone()
    finally:
        con.close()
    return bool(row and row[0])


def _payload_cost(out: dict[str, Any]) -> int:
    return _estimate_text_tokens(json.dumps(out, ensure_ascii=False, default=str))


def _fit(out: dict[str, Any], budget: int) -> None:
    """Trim `out` in place until its whole JSON estimate (including `tokens_estimated` itself) is within budget.

    Review fix (0.3.0-rc2): `newest_known_at` is recomputed from the facts that remain, so it never names a fact
    the caller was not shown."""
    noted = False
    while True:
        out["tokens_estimated"] = 0
        for _ in range(5):
            cost = _payload_cost(out)
            if out["tokens_estimated"] == cost:
                break
            out["tokens_estimated"] = cost
        if out["tokens_estimated"] <= budget:
            break
        for key, keep in (("interpretations", 0), ("contradictions", 0), ("decisions", 0), ("facts", 0)):
            if len(out[key]) > keep:
                out[key].pop()
                # recompute newest_known_at after popping a fact
                if key == "facts" and out["facts"]:
                    known_ats = [f.get("known_at") for f in out["facts"] if f.get("known_at")]
                    out["newest_known_at"] = max(known_ats) if known_ats else None
                elif key == "facts":
                    out["newest_known_at"] = None
                break
        else:
            # Keep a degradation notice (semantic_degradation) through the collapse; it says why facts may be missing.
            short = [g for g in out["gaps"] if g.startswith(DEGRADED_PREFIX)] + ["items left out to fit the budget"]
            if out["gaps"] != short:
                out["gaps"] = short
            elif "reply_window" in out:
                del out["reply_window"]
            elif "budget_tokens" in out:
                del out["budget_tokens"]
            else:
                # nothing left to trim; ensure newest_known_at is correct
                if out["facts"]:
                    known_ats = [f.get("known_at") for f in out["facts"] if f.get("known_at")]
                    out["newest_known_at"] = max(known_ats) if known_ats else None
                else:
                    out["newest_known_at"] = None
                return
            noted = True
            continue
        if not noted:
            out["gaps"].append("items left out to fit the budget")
            noted = True
    # final recompute after loop exits via budget satisfied
    if out["facts"]:
        known_ats = [f.get("known_at") for f in out["facts"] if f.get("known_at")]
        out["newest_known_at"] = max(known_ats) if known_ats else None
    else:
        out["newest_known_at"] = None


DEGRADED_PREFIX = "meaning-based search unavailable"


def semantic_degradation(database_path: str | Path, vectors_path: str | Path) -> str | None:
    """Review fix (0.3.0-rc2): when meaning-based search is configured but its lane is not open, say so in the
    briefing's gaps instead of silently returning keyword-only results. None when not configured or ready. Never raises."""
    try:
        from ledger_memory.embeddings import resolve_embedding_backend_name
        backend = resolve_embedding_backend_name(database_path)
        if backend in ("none", "", None):
            return None
        from ledger_memory.semantic_readiness import readiness
        report = readiness(database_path, vectors_path)
        if report.lane_open:
            return None
        return f"{DEGRADED_PREFIX} ({report.reason or report.state}); facts below were found by keyword"
    except Exception:
        return f"{DEGRADED_PREFIX} (status unknown); facts below were found by keyword"


def build_brief(database_path: Path, vectors_path: Path, ctx, question: str, budget_tokens: int,
                authority_labels: dict[str, str], reply_window: dict[str, Any] | None = None) -> dict[str, Any]:
    gaps: list[str] = []
    candidates, pool, union = _fused_fact_candidates(database_path, question, limit=FACT_FETCH_LIMIT,
                                                     sidecar_path=vectors_path)
    candidates = list(candidates)[:FACT_FETCH_LIMIT * 2]
    records = [r for r in tiered_read(ctx, candidates, database_path=database_path, surface=SURFACE)
               if r.record_type == "fact" and (r.content or "").strip()]

    used = 0
    shown: list = []
    facts_out: list[dict[str, Any]] = []
    for rec in records:
        text = delimit(rec.content.strip(), flagged=_flagged(database_path, rec.record_id))
        cost = _cost(text)
        if used + cost > budget_tokens:
            break
        used += cost
        shown.append(rec)
        authority = rec.provenance.authority_class if rec.provenance is not None else ""
        item = {"id": rec.record_id, "statement": text, "known_at": (str(rec.known_at)[:10] if rec.known_at else None),
                "source": authority_labels.get(authority or "", "unclassified")}
        if not rec.current:
            item["current"] = False              # a superseded fact says so; a current one needs no flag
        facts_out.append(item)
    omitted = max(pool, union, len(records)) - len(shown)
    if omitted > 0:
        gaps.append(f"{omitted} more matching facts left out; `recall_more` can look further")
    if not shown:
        gaps.append("no matching facts were found" if not records else "budget too small for even one fact")

    newest = None
    for rec in shown:
        if rec.known_at and (newest is None or str(rec.known_at)[:10] > newest):
            newest = str(rec.known_at)[:10]

    decisions_out: list[dict[str, Any]] = []
    contradictions_out: list[dict[str, Any]] = []
    interpretations_out: list[dict[str, Any]] = []
    if shown and used < budget_tokens:
        try:
            sections = briefing_sections(ctx, database_path, question, surface=SURFACE, shipping=shown)
            for section, _exc in sections.degraded:
                gaps.append(f"{section} unavailable this time")
            for d in sections.decisions:
                text = delimit(d.content)
                cost = _cost(text)
                if used + cost > budget_tokens:
                    gaps.append("decisions omitted: budget")
                    break
                used += cost
                state = "superseded" if d.superseded else ("in force" if d.current else "rejected")
                decisions_out.append({"id": d.record_id, "decision": text, "known_at": (d.known_at or None), "state": state})
            for a, b, kind in sections.conflict_pairs:
                cost = _cost(f"{a} {b} {kind}")
                if used + cost > budget_tokens:
                    gaps.append("contradictions omitted: budget")
                    break
                used += cost
                contradictions_out.append({"fact_ids": [a, b], "kind": kind})
            for v in sections.claims:
                basis = format_basis_profile(v.basis_profile) if v.basis_profile is not None else "{model inference}"
                text = delimit(v.text)
                cost = _cost(text + " " + basis)
                if used + cost > budget_tokens:
                    gaps.append("interpretations omitted: budget")
                    break
                used += cost
                interpretations_out.append({"claim_id": v.claim_id, "text": text, "basis": basis})
        except Exception:
            gaps.append("decisions, contradictions and interpretations unavailable this time")
    elif shown:
        gaps.append("sections omitted: budget")

    out = {
        "facts": facts_out,
        "decisions": decisions_out,
        "contradictions": contradictions_out,
        "interpretations": interpretations_out,
        "newest_known_at": newest,
        "gaps": gaps,
        "budget_tokens": budget_tokens,
        "tokens_estimated": 0,
    }
    if reply_window is not None:
        out["reply_window"] = reply_window
    degraded = semantic_degradation(database_path, vectors_path)
    if degraded:
        out["gaps"].insert(0, degraded)
    _fit(out, budget_tokens)
    return out
