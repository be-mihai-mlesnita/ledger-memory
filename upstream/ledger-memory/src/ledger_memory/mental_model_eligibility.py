from __future__ import annotations

"""the mental-model eligibility primitive and activation ceremony.

THE one door between the substrate and any surface that could show a claim to the owner:

- **`eligible_claims` is the only reader.** Every surface — the inspect CLI, the morning brief,
  and any future answer path — gets claims exclusively through this function. The call-tree guard
  (`substrate_read_inventory`) proves no other module reads mental-model rows, the
  `fact_writer_inventory` discipline applied to READS.
- **Five-way per-claim refusal, fail-closed** (spec §3.1): stale, hidden-evidence, under-covered,
  contradicted, out-of-scope. A refused claim returns as a verdict with named reasons — never as
  bare text, never silently dropped.
- **Activation is a ceremony, never a side effect**: adjudication-clean only, the
  config thresholds satisfied, the ruling-5 benchmark check clean (or a recorded, ACCEPTED owner
  override decision naming the (version, dimension)), and an explicit owner act. The "only door"
  claim is STRUCTURAL, not prose (review blocker 1): `transition_version` refuses `active`
  outright, so the one production write of `status = 'active'` is `activate_version`'s own UPDATE
  below — and the SQL inventory scan flags any substrate SQL outside the family, writes included.
- **Evidence-only fallback** (spec §3.2): when every claim is refused, callers render exactly what
  they render today. The feature degrades to the status quo, never to silence, never to an
  ineligible claim.
"""

import json
import re
from dataclasses import dataclass
from pathlib import Path

from ledger_memory.db import connect_readonly
from ledger_memory.mental_models import MentalModelError, get_version

# The gating benchmark dimensions: a reported ledger loss on any of
# these in the LATEST committed benchmark summary blocks activation absent a recorded override.
GATING_DIMENSIONS = ("mental_models", "temporal_updates", "long_horizon_recall")

# Decision 270's conservative spec-pinned defaults; each is owner-tunable via set-config (audited).
THRESHOLD_DEFAULTS = {
    "mental_models.min_evidence": "2",
    "mental_models.min_adjudication_support": "1.0",
    # authority floor: refuse claims whose EVERY citation is model_inferred
    "mental_models.authority_floor": "not_all_model_inferred",
}

REFUSAL_KINDS = ("stale", "hidden_evidence", "under_covered", "contradicted",
                 "out_of_scope", "basis_unavailable", "needs_review")


@dataclass(frozen=True, kw_only=True)
class DerivationAssessment:
    state: str
    refusal_kinds: tuple[str, ...]
    profile: object | None = None
    authorized_claim_count: int = 0
    run: dict | None = None


def assess_derivation_basis(database_path: Path, *, version_id: int, context=None,
                             surface: str | None = None) -> DerivationAssessment:
    """Structurally validate a receipt and, when a reader is supplied, reauthorize every input.

    Definition authorization precedes loading the version's claim text. The no-context path is
    reserved for activation diagnostics and checks only stored structure/freshness, never serving.
    """
    from ledger_memory.mental_model_provenance import (
        BASIS_DIMENSION, canonical_sha256,
        latest_applicable_adjudication_run, make_provenance_profile, parse_derivation_basis,
    )
    from ledger_memory.mental_models import (
        MentalModelError, VersionInput, get_version,
        get_version_model_id, snapshot_fact_input, staleness_check,
    )

    definition = None
    authorized_records = None
    if context is not None:
        try:
            model_id = get_version_model_id(database_path, version_id)
            from ledger_memory.mental_models import authorized_model_definition
            definition = authorized_model_definition(database_path, context, model_id)
            # Permission precedence is independent of receipt validity: establish the complete
            # stored dependency set before any malformed/missing/stale-basis return can expose
            # version or claim metadata. This reads only input references before authorization.
            with connect_readonly(database_path) as connection:
                refs = connection.execute(
                    "SELECT i.record_type, i.record_id, f.quarantined "
                    "FROM mental_model_version_inputs i LEFT JOIN facts f "
                    "ON i.record_type='fact' AND f.id=i.record_id "
                    "WHERE i.version_id=? ORDER BY i.id", (version_id,),
                ).fetchall()
                inputs_quarantined = any(bool(ref["quarantined"]) for ref in refs)
            if not refs or len(refs) > 500 or inputs_quarantined or any(
                    str(ref["record_type"]) != "fact" for ref in refs):
                return DerivationAssessment(state="unavailable", refusal_kinds=("hidden_evidence",))
            from ledger_memory.candidates import Candidate
            from ledger_memory.tiered_read import tiered_read
            ids = [int(ref["record_id"]) for ref in refs]
            records = tiered_read(context, [Candidate(record_type="fact", record_id=rid,
                score=0.0, lane="basis-permission") for rid in ids], database_path=database_path,
                surface=surface)  # the asking surface, suffixed; never a proof surface
            authorized_records = {record.record_id: record for record in records}
            if set(authorized_records) != set(ids):
                return DerivationAssessment(state="unavailable", refusal_kinds=("hidden_evidence",))
        except Exception:
            return DerivationAssessment(state="unavailable", refusal_kinds=("hidden_evidence",))
    try:
        version = get_version(database_path, version_id)
    except MentalModelError:
        return DerivationAssessment(state="needs_review", refusal_kinds=("basis_unavailable",))
    rows = [row for row in version["evaluations"] if row["dimension"] == BASIS_DIMENSION]
    if len(rows) != 1:
        return DerivationAssessment(state="needs_review", refusal_kinds=("basis_unavailable",))
    try:
        basis = parse_derivation_basis(rows[0]["rationale"])
    except (TypeError, ValueError):
        return DerivationAssessment(state="needs_review", refusal_kinds=("basis_unavailable",))
    if basis.recipe_sha256 != version["version"]["recipe_sha256"]:
        return DerivationAssessment(state="needs_review", refusal_kinds=("stale",))

    try:
        with connect_readonly(database_path) as connection:
            model = connection.execute(
                "SELECT id, subject, scope, purpose, subject_actor_id, sensitivity_tier "
                "FROM mental_models WHERE id = ?", (version["version"]["mental_model_id"],),
            ).fetchone()
        if model is None:
            raise ValueError("definition missing")
        current_definition = {"id": int(model["id"]), "subject": model["subject"],
            "scope": model["scope"], "purpose": model["purpose"],
            "subject_actor_id": int(model["subject_actor_id"]),
            "sensitivity_tier": int(model["sensitivity_tier"])}
        expected_definition = {"id": current_definition["id"],
            "fingerprint": canonical_sha256(current_definition),
            "sensitivity_tier": current_definition["sensitivity_tier"]}
        if dict(basis.definition) != expected_definition:
            return DerivationAssessment(state="needs_review", refusal_kinds=("stale",))
        stored_inputs = version["inputs"]
        if len(stored_inputs) != len(basis.inputs):
            raise ValueError("input count mismatch")
        inputs = []
        for stored, entry in zip(stored_inputs, basis.inputs):
            snapshot = VersionInput(record_type=str(stored["record_type"]),
                record_id=int(stored["record_id"]), content_sha256=str(stored["content_sha256"]),
                authority_class=str(stored["authority_class"]),
                epistemic_status=str(stored["epistemic_status"]),
                sensitivity_tier=int(stored["input_sensitivity_tier"]))
            if (entry["record_type"] != snapshot.record_type
                    or int(entry["record_id"]) != snapshot.record_id
                    or entry["content_sha256"] != snapshot.content_sha256
                    or entry["authority_class"] != snapshot.authority_class
                    or entry["epistemic_status"] != snapshot.epistemic_status
                    or int(entry["sensitivity_tier"]) != snapshot.sensitivity_tier):
                raise ValueError("basis does not match stored inputs")
            inputs.append(snapshot)
        claims = version["claims"]
        if len(claims) != len(basis.claims):
            raise ValueError("claim count mismatch")
        for claim_row, entry in zip(claims, basis.claims):
            if int(claim_row["claim_index"]) != int(entry["claim_index"]):
                raise ValueError("claim index mismatch")
            cited_ids = sorted(int(row["record_id"]) for row in version["evidence"]
                               if int(row["claim_id"]) == int(claim_row["id"]))
            expected_ids = sorted(int(basis.inputs[index]["record_id"])
                                  for index in entry["cited_input_indices"])
            if cited_ids != expected_ids:
                raise ValueError("claim evidence mapping mismatch")
        if staleness_check(database_path, version_id=version_id):
            return DerivationAssessment(state="needs_review", refusal_kinds=("stale",))
        if context is None:
            # Activation diagnostics have no reader credential: compare the full consumed
            # projection through the content-free integrity helper, never synthesize authority.
            from ledger_memory.tiered_read import projection_matches_current_for_diagnostics
            for entry, snapshot in zip(basis.inputs, inputs):
                if not projection_matches_current_for_diagnostics(database_path, snapshot, entry):
                    return DerivationAssessment(state="needs_review", refusal_kinds=("stale",))
    except (KeyError, TypeError, ValueError, MentalModelError):
        return DerivationAssessment(state="needs_review", refusal_kinds=("basis_unavailable",))

    if context is not None:
        try:
            records = authorized_records or {}
            if set(records) != {item.record_id for item in inputs}:
                return DerivationAssessment(state="unavailable", refusal_kinds=("hidden_evidence",))
            from ledger_memory.mental_model_provenance import input_entry_from_record
            for entry, snapshot in zip(basis.inputs, inputs):
                current = snapshot_fact_input(database_path, snapshot.record_id)
                if current != snapshot or dict(entry) != input_entry_from_record(
                        records[snapshot.record_id], current):
                    return DerivationAssessment(state="needs_review", refusal_kinds=("stale",))
        except Exception:
            return DerivationAssessment(state="unavailable", refusal_kinds=("hidden_evidence",))

    try:
        from ledger_memory.mental_model_compiler import adjudicator_recipe_sha256
        run = latest_applicable_adjudication_run(
            version, judge_recipe_sha256=adjudicator_recipe_sha256())
    except (TypeError, ValueError):
        return DerivationAssessment(state="needs_review", refusal_kinds=("needs_review",))
    if run is None:
        return DerivationAssessment(state="needs_review", refusal_kinds=("needs_review",))
    if run["status"] != "completed":
        return DerivationAssessment(state="needs_review", refusal_kinds=("needs_review",))
    if (any(pair["result"] != "SUPPORTS" for pair in run["pairs"])
            or any(claim["whole_set"] != "supported_as_phrased" for claim in run["claims"])):
        return DerivationAssessment(state="needs_review", refusal_kinds=("needs_review",), run=run)

    profile = make_provenance_profile(
        authority_classes=tuple(entry["authority_class"] for entry in basis.inputs),
        attestation_states=tuple(entry["provenance"]["attestation"] for entry in basis.inputs),
        materializations=tuple(entry["provenance"]["materialization"] or "materialization_unclassified"
                               for entry in basis.inputs),
        original_availability=tuple(entry["original_availability"] for entry in basis.inputs),
        permission="permitted" if context is not None else "denied")
    return DerivationAssessment(state="qualified_inference", refusal_kinds=(), profile=profile,
                                authorized_claim_count=len(basis.claims), run=run)


@dataclass(frozen=True, kw_only=True)
class ClaimVerdict:
    claim_id: int | None
    version_id: int | None
    mental_model_id: int | None
    text: str | None            # None when refused — a refused claim never ships its text
    kind: str | None
    sensitivity_tier: int | None
    eligible: bool
    refusals: tuple[str, ...]   # empty iff eligible
    receipts: dict              # evidence record ids + adjudication support + trust summary
    basis_profile: object | None = None


def _threshold(database_path: Path, key: str) -> str:
    from ledger_memory.config import get_config

    return (get_config(database_path, key, None) or THRESHOLD_DEFAULTS[key]).strip()


# ------------------------------------------------------------------------------------------------
# The primitive
# ------------------------------------------------------------------------------------------------


def has_active_versions(database_path: Path) -> bool:
    """Content-free presence probe for surfaces deciding whether to render a section at all —
    lives HERE so no consumer (morning brief) ever writes substrate SQL of its own."""
    with connect_readonly(database_path) as connection:
        return connection.execute(
            "SELECT 1 FROM mental_model_versions WHERE status = 'active' LIMIT 1"
        ).fetchone() is not None


def eligible_claims(
    database_path: Path, context, *, mental_model_id: int | None = None,
    query_scope: str | None = None, surface: str | None = None,
) -> tuple[ClaimVerdict, ...]:
    """Every claim of every ACTIVE version (optionally one model's), each with its verdict.

    Fail-closed at every step: versions that are not `active` are invisible here (their claims are
    `stale` by definition and only surface through the inspect path, marked so); a claim whose
    evidence no longer materializes through `tiered_read` for THIS context is refused
    hidden_evidence; thresholds come from config with the decision-270 defaults; an unresolved
    contradiction or a dispute/abstain delta refuses; a query scope that does not overlap the
    definition's subject/scope refuses out_of_scope.

    Scope semantics, disclosed (review NIT 6): `query_scope=None` means "no query exists to scope
    against" — the inspect CLI and the morning brief render a model the OWNER addressed directly,
    so out_of_scope is inapplicable there, not silently passed. Any future ANSWER surface (the
    deferred converse/telegram integration) MUST pass the resolved query scope; and the match
    itself is deliberately crude — 4+-letter bag-of-words overlap, a coarse cross-subject fence,
    not a relevance ranker. Sharpening it is that future slice's problem, named here so it is
    never mistaken for one already solved."""
    if context is None:
        return (ClaimVerdict(claim_id=None, version_id=None, mental_model_id=None, text=None,
            kind=None, sensitivity_tier=None, eligible=False, refusals=("hidden_evidence",),
            receipts={}),)

    from ledger_memory.mental_model_challenge import refresh_triggers

    min_evidence = int(_threshold(database_path, "mental_models.min_evidence"))
    min_support = float(_threshold(database_path, "mental_models.min_adjudication_support"))
    authority_floor = _threshold(database_path, "mental_models.authority_floor")

    with connect_readonly(database_path) as connection:
        version_rows = connection.execute(
            "SELECT id, mental_model_id FROM mental_model_versions WHERE status = 'active'"
            + (" AND mental_model_id = ?" if mental_model_id is not None else ""),
            (mental_model_id,) if mental_model_id is not None else (),
        ).fetchall()

    verdicts: list[ClaimVerdict] = []
    permission_refused = False
    for vrow in version_rows:
        version_id = int(vrow["id"])
        # audited as "<surface>:interpretation". The suffix keeps it OFF the interaction-proof surfaces, so
        # presence is decided exactly as before (a bare "api-chat" with no proof would clamp to T2); only the label changes.
        assessment = assess_derivation_basis(
            database_path, version_id=version_id, context=context,
            surface=f"{surface}:interpretation" if surface else None,
        )
        if assessment.state == "unavailable":
            permission_refused = True
            continue
        # assess_derivation_basis authorized the definition before reading claim-bearing rows.
        v = get_version(database_path, version_id)
        triggers = refresh_triggers(database_path, version_id=version_id)
        stale_version = bool(triggers)
        run = assessment.run
        pair_rows = run["pairs"] if run else []
        pair_by_claim: dict[int, list[str]] = {}
        for pair in pair_rows:
            pair_by_claim.setdefault(int(pair["claim_index"]), []).append(str(pair["result"]))
        whole_by_claim = {int(row["claim_index"]): str(row["whole_set"])
                          for row in (run["claims"] if run else [])}
        claim_ids = [int(c["id"]) for c in v["claims"]]
        deltas = _latest_delta_labels(database_path, claim_ids)
        contradicted_ids = _contradicted_claim_ids(database_path, version_id)
        authority_by_claim = _authority_classes(v)

        scope_text = f"{v['version']['mental_model_id']}".lower()
        with connect_readonly(database_path) as connection:
            definition_row = connection.execute(
                "SELECT subject, scope FROM mental_models WHERE id = ?",
                (v["version"]["mental_model_id"],),
            ).fetchone()
        if definition_row:
            scope_text = f"{definition_row['subject']} {definition_row['scope']}".lower()
        scope_words = {w for w in re.findall(r"[^\W\d_]{4,}", scope_text)}

        for claim in v["claims"]:
            claim_id = int(claim["id"])
            refusals: list[str] = []
            if stale_version:
                refusals.append("stale")
            cited_rows = [e for e in v["evidence"] if e["claim_id"] == claim_id]
            cited = [int(e["record_id"]) for e in cited_rows]
            if assessment.state != "qualified_inference":
                refusals.extend(assessment.refusal_kinds or ("needs_review",))
            claim_index = int(claim["claim_index"])
            pair_results = pair_by_claim.get(claim_index, [])
            support = (sum(1 for result in pair_results if result == "SUPPORTS") / len(pair_results)
                       if pair_results else None)
            if (len(cited) < min_evidence or support is None or support < min_support
                    or (authority_floor == "not_all_model_inferred"
                        and authority_by_claim.get(claim_id)
                        and authority_by_claim[claim_id] <= {"model_inferred"})):
                refusals.append("under_covered")
            if whole_by_claim.get(claim_index) != "supported_as_phrased":
                refusals.append("needs_review")
            if claim_id in contradicted_ids or deltas.get(claim_id) in ("dispute", "abstain"):
                refusals.append("contradicted")
            if query_scope is not None:
                query_words = {w for w in re.findall(r"[^\W\d_]{4,}", query_scope.lower())}
                if query_words and not (query_words & scope_words):
                    refusals.append("out_of_scope")
            eligible = not refusals
            if eligible:
                receipts = {"evidence_record_ids": cited, "adjudication_support": support,
                            "confidence": float(claim["confidence"])}
            else:
                receipts = {}
            verdicts.append(ClaimVerdict(
                claim_id=claim_id, version_id=version_id,
                mental_model_id=int(vrow["mental_model_id"]),
                text=str(claim["text"]) if eligible else None,
                kind=str(claim["kind"]), sensitivity_tier=int(claim["sensitivity_tier"]),
                eligible=eligible, refusals=tuple(refusals),
                receipts=receipts, basis_profile=assessment.profile if eligible else None,
            ))
    if permission_refused:
        return (ClaimVerdict(claim_id=None, version_id=None, mental_model_id=None, text=None,
            kind=None, sensitivity_tier=None, eligible=False, refusals=("hidden_evidence",),
            receipts={}),)
    return tuple(verdicts)


def _adjudication_support(version: dict) -> dict[int, float]:
    """Per claim: supported pairs / judged pairs, from the adjudication evaluations."""
    judged: dict[int, list[float]] = {}
    for e in version["evaluations"]:
        if e["dimension"] != "adjudication":
            continue
        payload = json.loads(e["rationale"])
        judged.setdefault(int(payload["claim_id"]), []).append(float(e["score"]))
    return {cid: (sum(scores) / len(scores)) for cid, scores in judged.items() if scores}


def _latest_delta_labels(database_path: Path, claim_ids: list[int]) -> dict[int, str]:
    """The most recent challenge label per claim, latest wins. Delta evaluations land on the NEW
    (challenger) version's rows and point back via `old_claim_id` — reading only the challenged
    version's own evaluations would miss every dispute (the no-evidence abstains are the one case
    written onto the old side), so this queries across versions by the referenced claim id."""
    if not claim_ids:
        return {}
    labels: dict[int, str] = {}
    placeholders = ",".join("?" for _ in claim_ids)
    with connect_readonly(database_path) as connection:
        rows = connection.execute(
            "SELECT rationale FROM mental_model_evaluations WHERE dimension = 'delta' "
            f"AND json_extract(rationale, '$.old_claim_id') IN ({placeholders}) ORDER BY id",
            claim_ids,
        ).fetchall()
    for r in rows:
        payload = json.loads(r["rationale"])
        labels[int(payload["old_claim_id"])] = str(payload["label"])
    return labels


def _contradicted_claim_ids(database_path: Path, version_id: int) -> set[int]:
    with connect_readonly(database_path) as connection:
        rows = connection.execute(
            "SELECT claim_id, contradicts_claim_id FROM mental_model_contradictions "
            "WHERE claim_id IN (SELECT id FROM mental_model_claims WHERE version_id = ?) "
            "OR contradicts_claim_id IN (SELECT id FROM mental_model_claims WHERE version_id = ?)",
            (version_id, version_id),
        ).fetchall()
    out: set[int] = set()
    for r in rows:
        out.add(int(r["claim_id"]))
        out.add(int(r["contradicts_claim_id"]))
    return out


def _authority_classes(version: dict) -> dict[int, set[str]]:
    by_input = {int(i["record_id"]): str(i["authority_class"]) for i in version["inputs"]}
    out: dict[int, set[str]] = {}
    for e in version["evidence"]:
        out.setdefault(int(e["claim_id"]), set()).add(
            by_input.get(int(e["record_id"]), "legacy_unclassified"))
    return out


# ------------------------------------------------------------------------------------------------
# The activation ceremony
# ------------------------------------------------------------------------------------------------


@dataclass(frozen=True, kw_only=True)
class ActivationReport:
    version_id: int
    adjudication_clean: bool
    refuted_claims: int
    thresholds_ok: bool
    threshold_failures: tuple[str, ...]
    ruling5_losses: tuple[str, ...]     # gating dimensions the latest committed run reports behind
    ruling5_overridden: tuple[str, ...]
    activatable: bool


def activation_check(database_path: Path, *, version_id: int,
                     overrides: dict[str, int] | None = None,
                     runs_dir: Path | None = None) -> ActivationReport:
    """Decision 270's admission rules, mechanically. `overrides` maps a gating dimension to the id
    of a RECORDED decision accepting that loss (per-(version, dimension) — the decision must
    exist; the ceremony records which override covered which loss)."""
    overrides = overrides or {}
    v = get_version(database_path, version_id)
    if v["version"]["status"] != "proposed":
        raise MentalModelError(
            f"version {version_id} is {v['version']['status']!r} — only a proposed version activates")

    basis_assessment = assess_derivation_basis(database_path, version_id=version_id)
    support = {}
    if basis_assessment.run is not None:
        claim_ids_by_index = {int(claim["claim_index"]): int(claim["id"])
                              for claim in v["claims"]}
        pairs_by_index: dict[int, list[str]] = {}
        for pair in basis_assessment.run["pairs"]:
            pairs_by_index.setdefault(int(pair["claim_index"]), []).append(str(pair["result"]))
        for index, results in pairs_by_index.items():
            if results:
                support[claim_ids_by_index[index]] = sum(
                    result == "SUPPORTS" for result in results) / len(results)
    claim_ids = [int(c["id"]) for c in v["claims"]]
    unjudged = [cid for cid in claim_ids if cid not in support]
    refuted = [cid for cid, s in support.items() if s < 1.0]
    adjudication_clean = (basis_assessment.state == "qualified_inference"
                          and not unjudged and not refuted)

    min_evidence = int(_threshold(database_path, "mental_models.min_evidence"))
    min_support = float(_threshold(database_path, "mental_models.min_adjudication_support"))
    authority_floor = _threshold(database_path, "mental_models.authority_floor")
    authority_by_claim = _authority_classes(v)
    failures: list[str] = []
    if basis_assessment.state != "qualified_inference":
        failures.append("derivation basis or complete current adjudication requires review")
    for claim in v["claims"]:
        cid = int(claim["id"])
        cited = [e for e in v["evidence"] if e["claim_id"] == cid]
        if len(cited) < min_evidence:
            failures.append(f"claim {cid}: {len(cited)} evidence < min {min_evidence}")
        # Adjudication-clean (support 1.0 everywhere) always satisfies min_support ≤ 1.0, so this
        # is redundant TODAY — it is read here anyway so the documented owner-tunable knob is
        # mechanically honored at both sites, never a dead config (review NIT 7).
        if support.get(cid) is not None and support[cid] < min_support:
            failures.append(f"claim {cid}: adjudication support {support[cid]:.2f} < "
                            f"min {min_support}")
        if (authority_floor == "not_all_model_inferred"
                and authority_by_claim.get(cid)
                and authority_by_claim[cid] <= {"model_inferred"}):
            failures.append(f"claim {cid}: every citation is model_inferred")

    losses = _latest_benchmark_losses(runs_dir)
    overridden = []
    for dim in losses:
        if dim in overrides and _override_decision_covers(
                database_path, overrides[dim], dimension=dim, version_id=version_id):
            overridden.append(dim)
    outstanding = [d for d in losses if d not in overridden]

    return ActivationReport(
        version_id=version_id,
        adjudication_clean=adjudication_clean,
        refuted_claims=len(refuted) + len(unjudged),
        thresholds_ok=not failures,
        threshold_failures=tuple(failures),
        ruling5_losses=tuple(losses),
        ruling5_overridden=tuple(overridden),
        activatable=adjudication_clean and not failures and not outstanding,
    )


def _latest_benchmark_losses(runs_dir: Path | None = None) -> tuple[str, ...]:
    """ledger's behind_on ∩ the gating dimensions, from the LATEST committed benchmark summary.
    No summary at all = no measurement = fail closed (every gating dimension counts as a loss:
    an unmeasured activation is exactly what ruling 5 exists to prevent)."""
    if runs_dir is None:
        runs_dir = Path(__file__).resolve().parent.parent.parent / "benchmarks" / "runs"
    summaries = sorted(runs_dir.glob("run-*.json"))
    if not summaries:
        return GATING_DIMENSIONS
    payload = json.loads(summaries[-1].read_text(encoding="utf-8"))
    adapter = next((a for a in payload.get("adapters", []) if a.get("name") == "ledger"), None)
    if adapter is None:
        return GATING_DIMENSIONS
    behind = set(adapter.get("behind_on", []))
    return tuple(d for d in GATING_DIMENSIONS if d in behind)


def _override_decision_covers(database_path: Path, decision_id: int, *,
                              dimension: str, version_id: int) -> bool:
    """A ruling-5 override must be a real, ACCEPTED decision whose own text names the dimension
    AND this version — review major 4: existence alone let ANY decision id (about anything, once)
    launder every loss on every future activation. Topicality is mechanical: the owner writes
    "… mental_models … version 7 …" in the decision they record, which is exactly the
    per-(version, dimension) granularity fork 4 ruled. Not matching is a refusal, never a warning."""
    with connect_readonly(database_path) as connection:
        row = connection.execute(
            "SELECT title, summary, rationale, status FROM decisions WHERE id = ?",
            (decision_id,),
        ).fetchone()
    if row is None or str(row["status"]) != "accepted":
        return False
    text = f"{row['title']} {row['summary']} {row['rationale']}".lower()
    return dimension.lower() in text and f"version {version_id}" in text


def activate_version(
    database_path: Path, *, version_id: int, actor_id: int,
    overrides: dict[str, int] | None = None, runs_dir: Path | None = None,
) -> ActivationReport:
    """The ONLY `proposed → active` door in the codebase. Explicit owner act (a named actor is
    REQUIRED, never None), every decision-270 admission rule checked, every ruling-5 loss covered
    by a recorded override decision or the activation refuses. Never automatic, never scheduled,
    never model-initiated — there is deliberately no code path from any tick, digest, compiler or
    challenge surface to this function (the call-tree guard proves it)."""
    if actor_id is None:
        raise MentalModelError("activation requires a named owner actor — never anonymous")
    report = activation_check(database_path, version_id=version_id, overrides=overrides,
                              runs_dir=runs_dir)
    if not report.activatable:
        reasons = []
        if not report.adjudication_clean:
            reasons.append(f"{report.refuted_claims} claim(s) unjudged or refuted (admission is "
                           "adjudication-clean only — recompile and adjudicate first)")
        reasons.extend(report.threshold_failures)
        outstanding = [d for d in report.ruling5_losses if d not in report.ruling5_overridden]
        if outstanding:
            reasons.append(
                f"ruling-5 losses without a recorded override decision: {', '.join(outstanding)}")
        raise MentalModelError("activation refused: " + "; ".join(reasons))
    override_note = (
        "; overrides: " + ", ".join(f"{d}=decision {i}" for d, i in (overrides or {}).items())
        if overrides else ""
    )
    reason = (f"owner activation (decision-270 ceremony: adjudication-clean, thresholds ok, "
              f"ruling-5 {'clean' if not report.ruling5_losses else 'covered'}{override_note})")
    # The ONE production write of status='active' (review blocker 1: `transition_version` now
    # refuses 'active' structurally, so the door is this UPDATE — same transaction shape and
    # event vocabulary as transition_version, atomic with its emission).
    from ledger_memory.db import connect
    from ledger_memory.emission import emit_audit_and_event

    with connect(database_path) as connection:
        updated = connection.execute(
            "UPDATE mental_model_versions SET status = 'active' "
            "WHERE id = ? AND status = 'proposed'",
            (version_id,),
        ).rowcount
        if updated != 1:
            raise MentalModelError(
                f"activation lost the race: version {version_id} is no longer proposed")
        emit_audit_and_event(
            connection,
            action_type="mental_model.version_transitioned",
            target_type="mental_model_version",
            target_id=version_id,
            actor_id=actor_id,
            reason=reason,
            event_type="mental_model.version_transitioned",
            summary=f"Version {version_id} proposed→active (ceremony receipt in audit)",  # an earlier fix
            metadata=json.dumps({"from": "proposed", "to": "active", "reason": reason,
                                 "overrides": overrides or {}}, sort_keys=True),
        )
    return report


# ------------------------------------------------------------------------------------------------
# The call-tree guard — reads of substrate rows outside the family are violations
# ------------------------------------------------------------------------------------------------

SUBSTRATE_TABLES = (
    "mental_models", "mental_model_versions", "mental_model_version_inputs",
    "mental_model_claims", "mental_model_claim_evidence", "mental_model_contradictions",
    "mental_model_evaluations",
)

# The family allowed to read substrate rows directly, each with its reason on the record.
READ_ALLOWLIST = {
    "mental_models.py": "the substrate's own API (write-and-inspect)",
    "mental_model_compiler.py": "compiles INTO the substrate; reads its own writes",
    "mental_model_challenge.py": "challenges versions; reads receipts and claims",
    "mental_model_eligibility.py": "THE eligibility primitive itself",
    "integrity.py": "the polymorphic-orphan and export-closure scans",
}


def substrate_read_inventory(src_dir: Path | None = None) -> list[tuple[str, int, str]]:
    """(relpath, lineno, sql-ish fragment) for every literal substrate-table SQL OUTSIDE the
    allowlist — the call-tree obligation in the `fact_writer_inventory` shape.

    What it checks, precisely (review major 3 — the first build's docstring claimed more than the
    scan did): EVERY string constant anywhere in the first-argument expression of any
    execute/executemany/executescript call (plain literals, f-string parts, and inline
    concatenation), flagged when it names a substrate table. What it structurally CANNOT check:
    SQL assembled through variables defined elsewhere — statically unresolvable; the codebase's
    execute discipline is literal-first, and the companion `content_reader_inventory` closes the
    louder gap (a rogue caller skipping SQL entirely by importing `get_version`)."""
    import ast

    src_dir = src_dir or Path(__file__).resolve().parent
    violations: list[tuple[str, int, str]] = []
    for path in sorted(src_dir.glob("*.py")):
        if path.name in READ_ALLOWLIST:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr in ("execute", "executemany", "executescript")
                    and node.args):
                continue
            texts = [c.value for c in ast.walk(node.args[0])
                     if isinstance(c, ast.Constant) and isinstance(c.value, str)]
            for text in texts:
                lowered = text.lower()
                if any(t in lowered for t in SUBSTRATE_TABLES):
                    violations.append((path.name, node.lineno, text.strip()[:80]))
    return violations


def content_reader_inventory(src_dir: Path | None = None) -> list[tuple[str, int, str]]:
    """Modules outside the family that reference the substrate's content-bearing readers by NAME
    (`get_version` — full claim text for ANY status, no eligibility check). Review major 3: the
    SQL scan cannot see a caller that never writes SQL — the reviewer's rogue was two lines,
    `from ledger_memory.mental_models import get_version; get_version(db, 1)["claims"][0]["text"]`. This
    scan flags the import AND any attribute access of the name, fail-closed on false positives
    (an unused import counts — it is the seam, remove it)."""
    import ast

    watched = {"get_version"}
    src_dir = src_dir or Path(__file__).resolve().parent
    violations: list[tuple[str, int, str]] = []
    for path in sorted(src_dir.glob("*.py")):
        if path.name in READ_ALLOWLIST:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "ledger_memory.mental_models":
                for alias in node.names:
                    if alias.name in watched:
                        violations.append((path.name, node.lineno, f"import {alias.name}"))
            elif isinstance(node, ast.Attribute) and node.attr in watched:
                violations.append((path.name, node.lineno, f"attribute .{node.attr}"))
    return violations


__all__ = [
    "ActivationReport",
    "ClaimVerdict",
    "GATING_DIMENSIONS",
    "READ_ALLOWLIST",
    "SUBSTRATE_TABLES",
    "THRESHOLD_DEFAULTS",
    "activate_version",
    "activation_check",
    "assess_derivation_basis",
    "content_reader_inventory",
    "eligible_claims",
    "has_active_versions",
    "substrate_read_inventory",
]
