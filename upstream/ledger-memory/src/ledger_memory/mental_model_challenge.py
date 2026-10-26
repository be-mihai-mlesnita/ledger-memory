from __future__ import annotations

"""blind-first challenge and refresh.

Keeps mental models honest over time WITHOUT letting the previous answer anchor the new one:

- **Blind-first, mechanically.** The reconsideration is a fresh an earlier fix compile — its prompt is
  assembled from evidence only, and a test SPIES the prompt to assert no prior claim text reaches
  it. The seeded-anchoring regression (`_anchored_claims`, test-only) exists to prove the harness
  can catch the leak: an anchored pass must measurably diverge from the blind pass.
- **Delta classification.** Every old claim is labelled exactly one of
  `reaffirm · revise · dispute · coexist · retire · abstain`, deterministically (evidence-overlap +
  contradiction polarity + content-word similarity — model-free, so the classifier itself cannot
  anchor). Labels append as structured evaluations on the NEW version; the OLD version transitions
  to `stale` with the delta counts in its reason — history only ever appends.
- **Refresh triggers from the substrate's own §3b signal**: input content drift, input epistemic
  status drift (retraction/dispute/supersession/reclassification — byte-identical text included),
  AND compiler-recipe drift (model identity, prompt hash, compiler version from the immutable
  receipt) — the third is the one usually forgotten.
- **Demote-only transitions.** This module CAN move a version to `stale`/`retired` (that IS the
  refresh outcome) but activation stays unreachable: every transition goes through
  `_demote_only_transition`, which refuses `active`/`proposed` fail-closed, and the module's AST
  test asserts no other path to `transition_version` exists.
"""

import json
import re
from dataclasses import dataclass
from pathlib import Path

from ledger_memory.mental_models import (
    MentalModelError,
    add_evaluation,
    get_version,
    staleness_check,
    transition_version,
)

DELTA_LABELS = ("reaffirm", "revise", "dispute", "coexist", "retire", "abstain")


def _demote_only_transition(database_path: Path, *, version_id: int, new_status: str,
                            reason: str, actor_id: int | None) -> None:
    """The ONLY door this module has to version transitions — and it only points DOWN. Activation
    (`active`) and un-retiring (`proposed`) are refused fail-closed; an earlier fix owns activation."""
    if new_status not in ("stale", "retired"):
        raise MentalModelError(
            f"challenge surface refuses transition to {new_status!r} — it may only demote "
            "(stale/retired); activation belongs to a later, separate step"
        )
    transition_version(database_path, version_id=version_id, new_status=new_status,
                       reason=reason, actor_id=actor_id)


# ------------------------------------------------------------------------------------------------
# Refresh triggers — §3.4
# ------------------------------------------------------------------------------------------------


@dataclass(frozen=True, kw_only=True)
class RefreshTrigger:
    kind: str        # input_content | input_status | recipe
    detail: str


def refresh_triggers(database_path: Path, *, version_id: int) -> tuple[RefreshTrigger, ...]:
    """Every reason this version deserves a blind re-derivation, from the substrate's own signals.
    Returns () for a version whose inputs AND recipe are explicitly unchanged — freshness is
    asserted, never assumed by silence (the ledger-row-247 discipline, extended to the recipe)."""
    from ledger_memory.mental_model_compiler import COMPILER_VERSION, _PROMPT_TEMPLATE
    import hashlib

    triggers: list[RefreshTrigger] = []
    for drift in staleness_check(database_path, version_id=version_id):
        kind = "input_status" if drift.field in ("epistemic_status", "authority_class", "existence") \
            else "input_content"
        triggers.append(RefreshTrigger(
            kind=kind,
            detail=f"{drift.record_type} {drift.record_id}: {drift.field} "
                   f"{drift.recorded!r} -> {drift.current!r}",
        ))
    v = get_version(database_path, version_id)["version"]
    current_recipe = hashlib.sha256((_PROMPT_TEMPLATE + COMPILER_VERSION).encode("utf-8")).hexdigest()
    if v["recipe_sha256"] != current_recipe:
        triggers.append(RefreshTrigger(
            kind="recipe", detail=f"recipe {v['recipe_sha256'][:12]} -> {current_recipe[:12]}"))
    if v["compiler_version"] != COMPILER_VERSION:
        triggers.append(RefreshTrigger(
            kind="recipe", detail=f"compiler {v['compiler_version']} -> {COMPILER_VERSION}"))
    return tuple(triggers)


# ------------------------------------------------------------------------------------------------
# Delta classification — deterministic, model-free
# ------------------------------------------------------------------------------------------------


def _content_words(text: str) -> set[str]:
    return {w for w in re.findall(r"[^\W\d_]{4,}", text.lower())}


def _evidence_ids(version: dict, claim_id: int) -> set[int]:
    return {int(e["record_id"]) for e in version["evidence"] if e["claim_id"] == claim_id}


def classify_deltas(old: dict, new: dict, *, stale_input_ids: set[int]) -> list[dict]:
    """One label per OLD claim (spec §3.3), by fixed rule order: dispute > reaffirm > revise >
    coexist > abstain > retire. `coexist` and `abstain` are the honest outcomes a binary keep/drop
    would erase: coexist = topical counterpart on DISJOINT evidence with no contradiction; abstain
    = no counterpart AND the old claim's evidence itself drifted (current evidence no longer
    decides)."""
    from ledger_memory.mental_model_compiler import find_contradictions
    from ledger_memory.mental_models import DraftClaim

    deltas: list[dict] = []
    for old_claim in old["claims"]:
        old_ev = _evidence_ids(old, old_claim["id"])
        old_words = _content_words(old_claim["text"])
        best = None
        best_score = 0.0
        for new_claim in new["claims"]:
            new_ev = _evidence_ids(new, new_claim["id"])
            ev_overlap = len(old_ev & new_ev) / len(old_ev | new_ev) if (old_ev | new_ev) else 0.0
            word_overlap = (len(old_words & _content_words(new_claim["text"]))
                            / max(1, len(old_words)))
            score = 0.6 * ev_overlap + 0.4 * word_overlap
            if score > best_score:
                best, best_score = new_claim, score

        label = None
        basis = ""
        if best is not None and best_score > 0.15:
            pair = [
                DraftClaim(text=old_claim["text"], kind=old_claim["kind"],
                           confidence=float(old_claim["confidence"]), evidence_input_indices=(0,)),
                DraftClaim(text=best["text"], kind=best["kind"],
                           confidence=float(best["confidence"]), evidence_input_indices=(0,)),
            ]
            if find_contradictions(pair):
                label = "dispute"
                basis = "polarity conflict with the reconsidered counterpart"
        if label is None and best is not None and best_score > 0.15:
            new_ev = _evidence_ids(new, best["id"])
            ev_jaccard = len(old_ev & new_ev) / len(old_ev | new_ev) if (old_ev | new_ev) else 0.0
            word_sim = (len(old_words & _content_words(best["text"])) / max(1, len(old_words)))
            if ev_jaccard >= 0.5 and word_sim >= 0.6:
                label, basis = "reaffirm", f"counterpart shares evidence (j={ev_jaccard:.2f}) and text"
            elif ev_jaccard > 0.0:
                label, basis = "revise", f"shared evidence (j={ev_jaccard:.2f}), text moved"
            else:
                label, basis = "coexist", "topical counterpart on disjoint evidence, no conflict"
        if label is None:
            if old_ev & stale_input_ids:
                label, basis = "abstain", "no counterpart and the claim's own evidence drifted"
            else:
                label, basis = "retire", "evidence unchanged yet the blind pass no longer derives it"
        deltas.append({
            "old_claim_id": int(old_claim["id"]),
            "new_claim_id": int(best["id"]) if best is not None and best_score > 0.15 else None,
            "label": label,
            "basis": basis,
        })
    return deltas


# ------------------------------------------------------------------------------------------------
# The challenge — replay context, blind reconsideration, reveal, classify, append
# ------------------------------------------------------------------------------------------------


@dataclass(frozen=True, kw_only=True)
class ChallengeResult:
    old_version_id: int
    new_version_id: int | None
    triggers: tuple[RefreshTrigger, ...]
    deltas: tuple[dict, ...]
    replay_intact_inputs: int      # receipted inputs whose content still matches the receipt
    replay_drifted_inputs: int     # the §3.1 split: world-changed vs model-changed


def challenge_version(
    database_path: Path, context, *, version_id: int,
    model_name: str | None = None, actor_id: int | None = None,
    _anchored_claims: tuple[str, ...] = (),
) -> ChallengeResult:
    """One blind-first challenge. `_anchored_claims` is the SEEDED ANCHORING REGRESSION (spec §4's
    seeded-loss discipline) — test-only, injecting prior claim text into the reconsideration so the
    suite can prove an anchored pass measurably diverges; production callers never pass it."""
    import datetime

    from ledger_memory import mental_model_compiler as mmc

    old = get_version(database_path, version_id)
    if old["version"]["status"] == "retired":
        raise MentalModelError(f"version {version_id} is retired — nothing to challenge")
    triggers = refresh_triggers(database_path, version_id=version_id)

    # §3.1 replay half: which receipted inputs still read as receipted (the world-changed vs
    # model-changed split — drifted inputs explain deltas as world-change; intact inputs make a
    # delta the MODEL's change).
    drifts = staleness_check(database_path, version_id=version_id)
    drifted_ids = {d.record_id for d in drifts}
    receipted_ids = {int(i["record_id"]) for i in old["inputs"]}
    intact = len(receipted_ids - drifted_ids)

    # Blind reconsideration: a fresh compile over CURRENT evidence, same selection strategy
    # (spec §7 non-goal), the old version never in the prompt (asserted by test spy). The seeded
    # anchoring hook appends old claim text ONLY when the regression is deliberately armed.
    # Selection FAILS CLOSED on a stale index and this challenge's own reads/audits (and whatever
    # preceded it) stale it — refresh first, the same lesson the nightly hook measured.
    from ledger_memory.search_index import rebuild_search_index

    rebuild_search_index(database_path)
    start = datetime.datetime.now().isoformat(timespec="seconds")
    if _anchored_claims:
        original_template = mmc._PROMPT_TEMPLATE
        mmc._PROMPT_TEMPLATE = original_template + "\n\nPrior conclusions (for reference):\n" + \
            "\n".join(f"- {c}" for c in _anchored_claims)
        try:
            outcome = mmc.compile_mental_model(
                database_path, context, mental_model_id=int(old["version"]["mental_model_id"]),
                model_name=model_name, actor_id=actor_id,
                window_started_at=start,
                window_ended_at=datetime.datetime.now().isoformat(timespec="seconds"))
        finally:
            mmc._PROMPT_TEMPLATE = original_template
    else:
        outcome = mmc.compile_mental_model(
            database_path, context, mental_model_id=int(old["version"]["mental_model_id"]),
            model_name=model_name, actor_id=actor_id,
            window_started_at=start,
            window_ended_at=datetime.datetime.now().isoformat(timespec="seconds"))

    if outcome.version_id is None:
        # No current evidence at all: every old claim abstains; the old version goes stale.
        # Review blocker 2: the abstains persist as evaluation rows on the OLD version — a delta
        # that lives only in a return value (or a bare count in a reason string) vanishes from
        # durable history, and §3.3/§3.6 demand every outcome appended with its what/who/why/when.
        deltas = tuple({"old_claim_id": int(c["id"]), "new_claim_id": None,
                        "label": "abstain", "basis": "no current evidence selects"}
                       for c in old["claims"])
        for delta in deltas:
            add_evaluation(
                database_path, version_id=version_id, dimension="delta",
                score=float(DELTA_LABELS.index("abstain")),
                rationale=json.dumps({**delta, "challenged_version_id": version_id},
                                     sort_keys=True),
                actor_id=actor_id,
            )
        if old["version"]["status"] != "stale":
            # Same guard as the found-evidence branch (delta review): re-challenging an
            # already-stale zero-evidence version must return cleanly, not throw on a no-op.
            _demote_only_transition(
                database_path, version_id=version_id, new_status="stale",
                reason=f"challenge found no current evidence ({len(deltas)} claim(s) abstain)",
                actor_id=actor_id)
        return ChallengeResult(old_version_id=version_id, new_version_id=None,
                               triggers=triggers, deltas=deltas,
                               replay_intact_inputs=intact,
                               replay_drifted_inputs=len(receipted_ids & drifted_ids))

    new = get_version(database_path, outcome.version_id)
    deltas = classify_deltas(old, new, stale_input_ids=drifted_ids)
    for delta in deltas:
        add_evaluation(
            database_path, version_id=outcome.version_id, dimension="delta",
            score=float(DELTA_LABELS.index(delta["label"])),
            rationale=json.dumps({**delta, "challenged_version_id": version_id}, sort_keys=True),
            actor_id=actor_id,
        )
    counts = {label: sum(1 for d in deltas if d["label"] == label) for label in DELTA_LABELS}
    summary = ", ".join(f"{k}={v}" for k, v in counts.items() if v)
    if old["version"]["status"] != "stale":
        _demote_only_transition(
            database_path, version_id=version_id, new_status="stale",
            reason=f"challenged by version {outcome.version_id}: {summary or 'no deltas'}",
            actor_id=actor_id)
    return ChallengeResult(
        old_version_id=version_id, new_version_id=outcome.version_id,
        triggers=triggers, deltas=tuple(deltas),
        replay_intact_inputs=intact,
        replay_drifted_inputs=len(receipted_ids & drifted_ids),
    )


__all__ = [
    "DELTA_LABELS",
    "ChallengeResult",
    "RefreshTrigger",
    "challenge_version",
    "classify_deltas",
    "refresh_triggers",
]
