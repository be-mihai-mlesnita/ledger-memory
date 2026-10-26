from __future__ import annotations

"""the mental-model record substrate.

A mental model is a GENERATED artifact: a model reading the subject's records and concluding
something they never said. This module is where an earlier fix's epistemic contract becomes structure:
inferred claims live in their own tables with their own authority space, and there is NO path from
a claim to `facts` — not a projection, not a supersession, not a summary field. The compiler that
will write here is an earlier fix; the refresh logic that consumes the staleness signal is an earlier fix.
This substrate is write-and-inspect only.

The parts that must not be softened:
- **The authority rule (spec §3a):** nothing in this module writes to `facts`. Enforced three ways:
  the tests assert `facts` row-count invariance across a full lifecycle, `fact_writer_inventory`'s
  AST scan proves this file opens no door into `facts`, and there is simply no function here that
  takes fact text to store.
- **Tier derivation (spec §3c, amendment v1.1):** claim tier = min(tier of its evidence inputs) —
  the MOST RESTRICTIVE under `tiers.py`'s lattice (lower number = more sensitive; `max` would
  label a {T1,T3}-derived claim shareable, which is the inversion the amendment fixed). T0 inputs
  are refused outright: `may_see` makes T0 unreachable, so a T0-derived claim would be a row
  nothing can read — a leak surface with no reader.
- **Immutability:** a version is never edited; a new conclusion appends a new version. Status
  changes go through `transition_version`, which mutates ONLY the status column and appends the
  what/who/why/when to events+audit in the same transaction — the `update_task_status` pattern.
- **§3b invalidation keys:** every version snapshots, per input: content hash, authority class,
  epistemic status, and tier. `staleness_check` compares those against current values — so a fact
  that is superseded, disputed, or reclassified stales the version even when its text is
  byte-identical.
"""

import json
from dataclasses import dataclass
from pathlib import Path

from ledger_memory.claim_envelope import VALID_KINDS, content_sha256
from ledger_memory.db import connect, connect_readonly
from ledger_memory.emission import emit_audit_and_event
from ledger_memory.tiers import TIER_SECRET

VERSION_STATES = ("proposed", "active", "stale", "retired")

# The only input record type the substrate resolves today. Fail-closed: an earlier fix widens this
# deliberately, per type, with the staleness derivation for that type — an unknown type must never
# silently snapshot nothing and then never stale.
SUPPORTED_INPUT_TYPES = ("fact",)

# note (review NIT 8): quarantined facts (the baseline schema's) are not yet an admission or
# staleness axis here — `quarantined` is set once at INSERT and never mutated today, so there is no
# drift to miss. The COMPILER must still filter `quarantined = 0` at selection time, matching the
# read chokepoint's own predicate.


class MentalModelError(ValueError):
    """A refused substrate operation — the message names the exact reason."""


@dataclass(frozen=True, kw_only=True)
class VersionInput:
    """One §3b invalidation-key snapshot: the record as the compiler saw it."""

    record_type: str
    record_id: int
    content_sha256: str
    authority_class: str
    epistemic_status: str
    sensitivity_tier: int


@dataclass(frozen=True, kw_only=True)
class DraftClaim:
    """An atomic claim as handed to `record_version`: text, kind (claim-envelope vocabulary),
    confidence, and the indices (into the version's input list) of its evidence."""

    text: str
    kind: str
    confidence: float
    evidence_input_indices: tuple[int, ...]
    semantic_scope: str = "unspecified"


@dataclass(frozen=True, kw_only=True)
class DraftContradiction:
    """A first-class contradiction between two claims of the same version, by claim index."""

    claim_index: int
    contradicts_claim_index: int
    rationale: str


@dataclass(frozen=True, kw_only=True)
class StaleInput:
    """One drifted input: what the receipt recorded vs what is true now."""

    record_type: str
    record_id: int
    field: str
    recorded: str
    current: str


def snapshot_fact_input(database_path: Path, fact_id: int) -> VersionInput:
    """Build the §3b snapshot for a fact AS IT IS NOW — the compiler calls this at read time so the
    receipt records exactly what it consumed. Refuses a missing fact."""
    with connect_readonly(database_path) as connection:
        return _derive_fact_input(connection, fact_id)


def _derive_fact_input(connection, fact_id: int) -> VersionInput:
    """THE one derivation of a fact's §3b snapshot, from a live row on the caller's connection.
    `record_version` re-runs this INSIDE its write transaction and refuses any mismatch against the
    caller's claimed input — a receipt that can be fabricated is not a receipt (review blocker 1:
    the first draft trusted the caller's declared tier, so a lying or TOCTOU-stale VersionInput
    could launder a T0 fact into a shareable claim)."""
    row = connection.execute(
        "SELECT id, statement, status, superseded_by_fact_id, sensitivity_tier, "
        "COALESCE(authority_class, 'legacy_unclassified') AS authority_class "
        "FROM facts WHERE id = ?",
        (fact_id,),
    ).fetchone()
    if row is None:
        raise MentalModelError(f"input fact not found: {fact_id}")
    return VersionInput(
        record_type="fact",
        record_id=fact_id,
        content_sha256=content_sha256(str(row["statement"])),
        authority_class=str(row["authority_class"]),
        epistemic_status=_fact_epistemic_status(connection, row),
        sensitivity_tier=int(row["sensitivity_tier"]),
    )


def _fact_epistemic_status(connection, row) -> str:
    """supersession > dispute > stored status. Dispute state is DERIVED from events (facts carry no
    disputed column by design — an earlier fix laundering path 3), so it is derived here too: a
    predicate reading only `facts` rows cannot see it, and the staleness signal must."""
    if row["superseded_by_fact_id"] is not None:
        return "superseded"
    disputed = connection.execute(
        "SELECT 1 FROM events WHERE event_type = 'fact.disputed' "
        "AND json_extract(metadata, '$.fact_id') = ? LIMIT 1",
        (row["id"],),
    ).fetchone()
    if disputed is not None:
        return "disputed"
    return str(row["status"])


def create_mental_model(
    database_path: Path, *, subject: str, scope: str, purpose: str,
    subject_actor_id: int, sensitivity_tier: int = 1, actor_id: int | None = None,
) -> int:
    """A stable definition. `subject_actor_id` is REQUIRED and explicit — no durable schema may key
    on 'the owner' as a singleton."""
    if not subject.strip() or not scope.strip() or not purpose.strip():
        raise MentalModelError("subject, scope and purpose must all be non-empty")
    if sensitivity_tier == TIER_SECRET or sensitivity_tier not in (1, 2, 3):
        raise MentalModelError(f"definition tier must be 1-3, got {sensitivity_tier}")
    with connect(database_path) as connection:
        actor = connection.execute(
            "SELECT 1 FROM actors WHERE id = ?", (subject_actor_id,)
        ).fetchone()
        if actor is None:
            raise MentalModelError(f"subject actor not found: {subject_actor_id}")
        try:
            cursor = connection.execute(
                "INSERT INTO mental_models (subject, scope, purpose, subject_actor_id, "
                "sensitivity_tier, created_by_actor_id) VALUES (?, ?, ?, ?, ?, ?)",
                (subject, scope, purpose, subject_actor_id, sensitivity_tier, actor_id),
            )
        except Exception as exc:
            raise MentalModelError(
                f"definition refused (duplicate (subject_actor, subject, scope)?): {exc}"
            ) from exc
        model_id = int(cursor.lastrowid)
        emit_audit_and_event(
            connection,
            action_type="mental_model.created",
            target_type="mental_model",
            target_id=model_id,
            actor_id=actor_id,
            reason=None,
            event_type="mental_model.created",
            summary=f"mental model {model_id} defined: {subject} / {scope}",
        )
    return model_id


def authorized_model_definition(database_path: Path, context, mental_model_id: int) -> dict:
    """Read a definition only after the shared credential/ceiling decision and audit."""
    from ledger_memory.client_auth import verify_device_token
    from ledger_memory.tiered_read import record_read_audit, resolve_read_authority
    from ledger_memory.tiers import may_see

    try:
        device_id, ceiling = resolve_read_authority(
            context, database_path=database_path, surface="mental-model-definition",
        )
        with connect_readonly(database_path) as connection:
            row = connection.execute(
                "SELECT sensitivity_tier FROM mental_models WHERE id = ?", (mental_model_id,),
            ).fetchone()
        allowed = row is not None and may_see(int(row["sensitivity_tier"]), ceiling)
        record_read_audit(database_path, device_id=device_id, record_count=int(allowed),
                          ceiling=ceiling, surface="mental-model-definition")
        if not allowed:
            raise MentalModelError("definition or dependencies not readable")
        if verify_device_token(Path(database_path), getattr(context, "token", "") or "") is None:
            raise MentalModelError("definition or dependencies not readable")
        with connect_readonly(database_path) as connection:
            current = connection.execute(
                "SELECT * FROM mental_models WHERE id = ?", (mental_model_id,),
            ).fetchone()
        if current is None or int(current["sensitivity_tier"]) != int(row["sensitivity_tier"]):
            current = row = None
            raise MentalModelError("definition or dependencies not readable")
        result = {key: current[key] for key in current.keys()}
        return result
    except MentalModelError:
        raise
    except Exception:
        raise MentalModelError("definition or dependencies not readable") from None


def record_version(
    database_path: Path, *, mental_model_id: int,
    compiler_identity: str, compiler_version: str, model_identity: str, recipe_sha256: str,
    window_started_at: str, window_ended_at: str,
    inputs: tuple[VersionInput, ...], claims: tuple[DraftClaim, ...],
    contradictions: tuple[DraftContradiction, ...] = (),
    derivation_basis: str | dict | None = None, context=None,
    actor_id: int | None = None,
) -> int:
    """Append one immutable version: receipt (folded into the version row — a version whose receipt
    cannot be reconstructed is not a version), the §3b input snapshots, the atomic claims with
    their evidence links, and any first-class contradictions — ONE transaction, all or nothing.

    Refusals (each a typed error, nothing written): empty inputs; a T0 input (excluded from
    compilation entirely, §3c); an unsupported input record_type; a claim with no evidence, an
    unknown kind, an out-of-range confidence, or an evidence index outside the input list; a
    contradiction naming a claim index that does not exist or naming itself."""
    if not inputs:
        raise MentalModelError("a version with no inputs has no receipt — refused")
    if not (compiler_identity and compiler_version and model_identity and recipe_sha256
            and window_started_at and window_ended_at):
        raise MentalModelError("an incomplete run receipt is not a receipt — every field is required")
    for inp in inputs:
        if inp.record_type not in SUPPORTED_INPUT_TYPES:
            raise MentalModelError(
                f"unsupported input record_type {inp.record_type!r} — widening is a deliberate, "
                "per-type change (the staleness derivation must come with it)"
            )
        if inp.sensitivity_tier == TIER_SECRET:
            raise MentalModelError(
                f"T0 input refused ({inp.record_type} {inp.record_id}): T0 is excluded from "
                "compilation entirely (§3c) — a T0-derived claim would be unreadable by design"
            )
        if inp.sensitivity_tier not in (1, 2, 3):
            raise MentalModelError(f"input tier must be 1-3, got {inp.sensitivity_tier}")
    if not claims:
        raise MentalModelError("a version with no claims asserts nothing — refused")
    for i, claim in enumerate(claims):
        if claim.kind not in VALID_KINDS:
            raise MentalModelError(f"claim {i}: unknown kind {claim.kind!r}")
        if not (0.0 <= claim.confidence <= 1.0):
            raise MentalModelError(f"claim {i}: confidence out of range: {claim.confidence}")
        if not claim.text.strip():
            raise MentalModelError(f"claim {i}: empty text")
        if not claim.evidence_input_indices:
            raise MentalModelError(
                f"claim {i}: no evidence — an unevidenced claim cannot be contradicted, "
                "evaluated or partially retracted, so it cannot be stored"
            )
        if len(set(claim.evidence_input_indices)) != len(claim.evidence_input_indices):
            raise MentalModelError(
                f"claim {i}: duplicate evidence indices — a compiler that cites the same input "
                "twice is malfunctioning, and silently deduplicating would hide it"
            )
        for idx in claim.evidence_input_indices:
            if not (0 <= idx < len(inputs)):
                raise MentalModelError(f"claim {i}: evidence index {idx} outside the input list")
    n = len(claims)
    for c in contradictions:
        if not (0 <= c.claim_index < n) or not (0 <= c.contradicts_claim_index < n):
            raise MentalModelError("contradiction names a claim index that does not exist")
        if c.claim_index == c.contradicts_claim_index:
            raise MentalModelError("a claim cannot contradict itself")
        if not c.rationale.strip():
            raise MentalModelError("a contradiction without a rationale is prose-free noise — refused")

    # §3c (amendment v1.1): min() = most restrictive under the lattice. Version tier from ALL
    # inputs; each claim's tier from ITS evidence inputs.
    version_tier = min(inp.sensitivity_tier for inp in inputs)

    from ledger_memory.mental_model_provenance import (
        BASIS_DIMENSION, basis_to_dict, canonical_json, canonical_sha256,
        parse_derivation_basis,
    )

    basis = None
    if derivation_basis is not None:
        if context is None:
            raise MentalModelError("a new derivation basis requires an authenticated context")
        try:
            raw_basis = canonical_json(derivation_basis) if isinstance(derivation_basis, dict) else derivation_basis
            basis = parse_derivation_basis(raw_basis)
        except (TypeError, ValueError):
            raise MentalModelError("derivation basis is invalid") from None
        if len(basis.inputs) != len(inputs) or len(basis.claims) != len(claims):
            raise MentalModelError("derivation basis does not match version inputs and claims")
        if basis.recipe_sha256 != recipe_sha256:
            raise MentalModelError("derivation basis recipe does not match version recipe")
        try:
            # Definition access is audited before its text is read; the transaction below
            # rechecks the credential and tier under the writer snapshot without nesting an
            # audit write inside BEGIN IMMEDIATE.
            authorized_model_definition(database_path, context, mental_model_id)
        except Exception:
            raise MentalModelError("definition or derivation inputs are no longer authorized") from None
        for i, (entry, claim) in enumerate(zip(basis.claims, claims)):
            if (entry["claim_index"] != i or entry["semantic_scope"] != claim.semantic_scope
                    or entry["cited_input_indices"] != list(claim.evidence_input_indices)):
                raise MentalModelError("derivation basis claim mapping does not match stored claims")
        if any(entry["record_id"] != inp.record_id for entry, inp in zip(basis.inputs, inputs)):
            raise MentalModelError("derivation basis inputs do not match stored input references")
    with connect(database_path) as connection:
        if basis is not None:
            # Establish the writer snapshot before the final authorized projection check. SQLite's
            # IMMEDIATE reservation excludes concurrent writers until the basis is committed.
            connection.execute("BEGIN IMMEDIATE")
        tier_row = connection.execute(
            "SELECT id, sensitivity_tier FROM mental_models WHERE id = ?", (mental_model_id,)
        ).fetchone()
        if tier_row is None:
            raise MentalModelError(f"mental model not found: {mental_model_id}")
        definition_tier = int(tier_row["sensitivity_tier"])
        if basis is not None:
            from ledger_memory.tiered_read import resolve_read_authority
            from ledger_memory.tiers import may_see
            try:
                _, current_ceiling = resolve_read_authority(context, database_path=database_path)
            except Exception:
                raise MentalModelError("definition or derivation inputs are no longer authorized") from None
            if not may_see(definition_tier, current_ceiling):
                raise MentalModelError("definition or derivation inputs are no longer authorized")
        model = connection.execute(
            "SELECT id, subject, scope, purpose, subject_actor_id, sensitivity_tier "
            "FROM mental_models WHERE id = ?", (mental_model_id,)
        ).fetchone()
        if model is None or int(model["sensitivity_tier"]) != definition_tier:
            raise MentalModelError("mental-model definition changed during compilation")
        if basis is not None:
            definition = {"id": int(model["id"]), "subject": model["subject"],
                          "scope": model["scope"], "purpose": model["purpose"],
                          "subject_actor_id": int(model["subject_actor_id"]),
                          "sensitivity_tier": definition_tier}
            expected = {"id": int(model["id"]), "fingerprint": canonical_sha256(definition),
                        "sensitivity_tier": definition_tier}
            if dict(basis.definition) != expected:
                raise MentalModelError("mental-model definition changed during compilation")
            from ledger_memory.mental_model_provenance import input_entry_from_record
            from ledger_memory.tiered_read import TieredReadRefused, _materialize, resolve_read_authority
            from ledger_memory.tiers import may_see

            try:
                _, current_ceiling = resolve_read_authority(
                    context, database_path=database_path,
                )
                authorized = [(inp.record_id, inp.sensitivity_tier) for inp in inputs
                              if may_see(inp.sensitivity_tier, current_ceiling)]
                if len(authorized) != len(inputs):
                    raise MentalModelError("derivation inputs are no longer authorized")
                current_records = _materialize(
                    Path(database_path), authorized, current_ceiling, connection=connection,
                )
                current_by_id = {record.record_id: record for record in current_records}
                if set(current_by_id) != {inp.record_id for inp in inputs}:
                    raise MentalModelError("derivation inputs are no longer authorized")
                for entry, inp in zip(basis.inputs, inputs):
                    if dict(entry) != input_entry_from_record(current_by_id[inp.record_id], inp):
                        raise MentalModelError("authorized input projection changed during compilation")
            except MentalModelError:
                raise
            except Exception:
                raise MentalModelError("derivation inputs could not be revalidated") from None
        version_tier = min(definition_tier, *(inp.sensitivity_tier for inp in inputs))
        # Review blocker 1: the caller's inputs are CLAIMS about records, verified here against a
        # fresh derivation on THIS write transaction (closing the snapshot→write TOCTOU window).
        # T0 is refused on the DERIVED value — the truth — regardless of what the caller declared.
        for inp in inputs:
            derived = _derive_fact_input(connection, inp.record_id)
            if derived.sensitivity_tier == TIER_SECRET:
                raise MentalModelError(
                    f"T0 input refused ({inp.record_type} {inp.record_id}): the LIVE record is "
                    "secret-tier, whatever the caller's snapshot claims (§3c)"
                )
            if derived != inp:
                raise MentalModelError(
                    f"input mismatch for {inp.record_type} {inp.record_id}: the claimed snapshot "
                    "does not match the live record (stale snapshot, or a fabricated receipt) — "
                    "re-snapshot and retry; a receipt that can be fabricated is not a receipt"
                )
        next_number = connection.execute(
            "SELECT COALESCE(MAX(version_number), 0) + 1 FROM mental_model_versions "
            "WHERE mental_model_id = ?",
            (mental_model_id,),
        ).fetchone()[0]
        cursor = connection.execute(
            "INSERT INTO mental_model_versions (mental_model_id, version_number, status, "
            "sensitivity_tier, compiler_identity, compiler_version, model_identity, recipe_sha256, "
            "window_started_at, window_ended_at, created_by_actor_id) "
            "VALUES (?, ?, 'proposed', ?, ?, ?, ?, ?, ?, ?, ?)",
            (mental_model_id, next_number, version_tier, compiler_identity, compiler_version,
             model_identity, recipe_sha256, window_started_at, window_ended_at, actor_id),
        )
        version_id = int(cursor.lastrowid)
        for inp in inputs:
            connection.execute(
                "INSERT INTO mental_model_version_inputs (version_id, record_type, record_id, "
                "content_sha256, authority_class, epistemic_status, input_sensitivity_tier) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (version_id, inp.record_type, inp.record_id, inp.content_sha256,
                 inp.authority_class, inp.epistemic_status, inp.sensitivity_tier),
            )
        claim_ids: list[int] = []
        for i, claim in enumerate(claims):
            claim_tier = version_tier
            c = connection.execute(
                "INSERT INTO mental_model_claims (version_id, claim_index, text, kind, confidence, "
                "sensitivity_tier, content_sha256) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (version_id, i, claim.text, claim.kind, claim.confidence, claim_tier,
                 content_sha256(claim.text)),
            )
            claim_id = int(c.lastrowid)
            claim_ids.append(claim_id)
            for idx in claim.evidence_input_indices:
                inp = inputs[idx]
                connection.execute(
                    "INSERT INTO mental_model_claim_evidence (claim_id, record_type, "
                    "record_id, content_sha256) VALUES (?, ?, ?, ?)",
                    (claim_id, inp.record_type, inp.record_id, inp.content_sha256),
                )
        for c in contradictions:
            pair_tier = version_tier
            connection.execute(
                "INSERT INTO mental_model_contradictions (claim_id, contradicts_claim_id, "
                "rationale, sensitivity_tier) VALUES (?, ?, ?, ?)",
                (claim_ids[c.claim_index], claim_ids[c.contradicts_claim_index], c.rationale, pair_tier),
            )
        if basis is not None:
            connection.execute(
                "INSERT INTO mental_model_evaluations (version_id, dimension, score, rationale, "
                "sensitivity_tier, evaluated_by_actor_id) VALUES (?, ?, 0.0, ?, ?, ?)",
                (version_id, BASIS_DIMENSION, canonical_json(basis_to_dict(basis)), version_tier, actor_id),
            )
        emit_audit_and_event(
            connection,
            action_type="mental_model.version_recorded",
            target_type="mental_model_version",
            target_id=version_id,
            actor_id=actor_id,
            reason=None,
            event_type="mental_model.version_recorded",
            summary=(
                f"mental model {mental_model_id} version {next_number} recorded: "
                f"{len(claims)} claim(s), {len(inputs)} input(s), tier {version_tier}"
            ),
            metadata=json.dumps(
                {"compiler": compiler_identity, "compiler_version": compiler_version,
                 "model": model_identity, "recipe_sha256": recipe_sha256}, sort_keys=True,
            ),
        )
    return version_id


def list_model_versions(database_path: Path, mental_model_id: int) -> tuple[dict, list[dict]]:
    """One model's definition + its versions (id, status, created_at), oldest first — the inspect
    surface's enumeration, in the substrate's own API for the same call-tree reason as below."""
    with connect_readonly(database_path) as connection:
        definition = connection.execute(
            "SELECT * FROM mental_models WHERE id = ?", (mental_model_id,)
        ).fetchone()
        if definition is None:
            raise MentalModelError(f"mental model not found: {mental_model_id}")
        versions = connection.execute(
            "SELECT id, status, created_at FROM mental_model_versions "
            "WHERE mental_model_id = ? ORDER BY id",
            (mental_model_id,),
        ).fetchall()
    return {k: definition[k] for k in definition.keys()}, [dict(r) for r in versions]


def list_mental_model_ids(database_path: Path) -> list[int]:
    """Content-free id enumeration for iterating consumers (the nightly compile loop) — lives in
    the substrate's own API so no consumer writes substrate SQL of its own (the
    call-tree guard treats any literal substrate read outside the family as a violation)."""
    with connect_readonly(database_path) as connection:
        return [int(r[0]) for r in connection.execute("SELECT id FROM mental_models ORDER BY id")]


def transition_version(
    database_path: Path, *, version_id: int, new_status: str, reason: str,
    actor_id: int | None = None,
) -> None:
    """The `update_task_status` pattern verbatim: mandatory reason, no-op rejected, the status
    UPDATE and the what/who/why/when emission in ONE transaction. History is never overwritten —
    the version row's only mutable column is `status`, and every change leaves an event.

    **This function cannot activate**: the first build documented
    `activate_version` as "the only door to active" while leaving THIS public function able to
    write it — the reviewer activated an unadjudicated version with `actor_id=None` in three
    lines. Aspirational prose is not a control: `active` is refused HERE, structurally, and the
    only production SQL that writes `status = 'active'` lives in `mental_model_eligibility.py`'s
    ceremony (proven by the activation-write inventory scan)."""
    if new_status == "active":
        raise MentalModelError(
            "transition_version cannot activate — 'active' is written only by the decision-270 "
            "ceremony (mental_model_eligibility.activate_version), which checks adjudication, "
            "thresholds, and ruling 5 under a named owner actor")
    if new_status not in VERSION_STATES:
        raise MentalModelError(f"unknown status {new_status!r} (choose from {VERSION_STATES})")
    if not reason or not reason.strip():
        raise MentalModelError("a non-empty reason is required for a version status change")
    with connect(database_path) as connection:
        row = connection.execute(
            "SELECT status FROM mental_model_versions WHERE id = ?", (version_id,)
        ).fetchone()
        if row is None:
            raise MentalModelError(f"version not found: {version_id}")
        old_status = str(row["status"])
        if old_status == new_status:
            raise MentalModelError(f"version {version_id} is already {new_status!r}")
        connection.execute(
            "UPDATE mental_model_versions SET status = ? WHERE id = ?", (new_status, version_id)
        )
        emit_audit_and_event(
            connection,
            action_type="mental_model.version_transitioned",
            target_type="mental_model_version",
            target_id=version_id,
            actor_id=actor_id,
            reason=reason,
            event_type="mental_model.version_transitioned",
            summary=f"Version {version_id} {old_status}→{new_status} (reason in audit)",  # an earlier fix
            metadata=json.dumps(
                {"from": old_status, "to": new_status, "reason": reason}, sort_keys=True
            ),
        )


def add_evaluation(
    database_path: Path, *, version_id: int, dimension: str, score: float, rationale: str,
    actor_id: int | None = None,
) -> int:
    """Append-only quality evaluation of a version. Never edits, never replaces."""
    from ledger_memory.mental_model_provenance import ADJUDICATION_RUN_DIMENSION, BASIS_DIMENSION

    if dimension in (BASIS_DIMENSION, ADJUDICATION_RUN_DIMENSION):
        raise MentalModelError(f"evaluation dimension {dimension!r} is reserved")
    if not dimension.strip() or not rationale.strip():
        raise MentalModelError("dimension and rationale are both required")
    with connect(database_path) as connection:
        row = connection.execute(
            "SELECT sensitivity_tier FROM mental_model_versions WHERE id = ?", (version_id,)
        ).fetchone()
        if row is None:
            raise MentalModelError(f"version not found: {version_id}")
        cursor = connection.execute(
            "INSERT INTO mental_model_evaluations (version_id, dimension, score, rationale, "
            "sensitivity_tier, evaluated_by_actor_id) VALUES (?, ?, ?, ?, ?, ?)",
            (version_id, dimension, score, rationale, int(row["sensitivity_tier"]), actor_id),
        )
        evaluation_id = int(cursor.lastrowid)
        emit_audit_and_event(
            connection,
            action_type="mental_model.version_evaluated",
            target_type="mental_model_version",
            target_id=version_id,
            actor_id=actor_id,
            reason=None,
            event_type="mental_model.version_evaluated",
            summary=f"Version {version_id} evaluated on {dimension}: {score}",
        )
    return evaluation_id


def record_adjudication_run(
    database_path: Path, *, version_id: int, run: str | dict,
    actor_id: int | None = None,
) -> int:
    """Atomically append one strict, complete adjudication receipt to its version."""
    from ledger_memory.mental_model_provenance import (
        ADJUDICATION_RUN_DIMENSION, basis_to_dict, canonical_json, canonical_sha256,
        expected_adjudication_pairs, parse_adjudication_run, parse_derivation_basis,
        validate_adjudication_coverage,
    )

    try:
        raw = run if isinstance(run, str) else canonical_json(run)
        parsed = parse_adjudication_run(raw)
        rationale = canonical_json(json.loads(raw))
    except (TypeError, ValueError):
        raise MentalModelError("adjudication receipt is invalid") from None
    if parsed.version_id != version_id:
        raise MentalModelError("adjudication receipt does not match version")

    with connect(database_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        version = connection.execute(
            "SELECT recipe_sha256, sensitivity_tier FROM mental_model_versions WHERE id = ?",
            (version_id,),
        ).fetchone()
        if version is None:
            raise MentalModelError("adjudication receipt does not match version")
        basis_rows = connection.execute(
            "SELECT rationale FROM mental_model_evaluations WHERE version_id = ? AND dimension = ?",
            (version_id, "derivation_basis_v1"),
        ).fetchall()
        if len(basis_rows) != 1:
            raise MentalModelError("adjudication receipt has no unique valid basis")
        try:
            basis = parse_derivation_basis(basis_rows[0]["rationale"])
            basis_fingerprint = canonical_sha256(basis_to_dict(basis))
            validate_adjudication_coverage(parsed, basis)
        except ValueError:
            raise MentalModelError("adjudication receipt does not completely match its basis") from None
        if (parsed.basis_fingerprint != basis_fingerprint
                or basis.recipe_sha256 != version["recipe_sha256"]):
            raise MentalModelError("adjudication receipt does not match the version recipe")

        stored_inputs = [int(row["record_id"]) for row in connection.execute(
            "SELECT record_id FROM mental_model_version_inputs WHERE version_id = ? ORDER BY id",
            (version_id,),
        )]
        if stored_inputs != [int(entry["record_id"]) for entry in basis.inputs]:
            raise MentalModelError("adjudication receipt basis no longer matches stored inputs")
        claim_rows = connection.execute(
            "SELECT id, claim_index FROM mental_model_claims WHERE version_id = ? ORDER BY claim_index",
            (version_id,),
        ).fetchall()
        if [int(row["claim_index"]) for row in claim_rows] != list(range(len(basis.claims))):
            raise MentalModelError("adjudication receipt basis no longer matches stored claims")
        actual_pairs = set()
        for row in claim_rows:
            claim_index = int(row["claim_index"])
            evidence = connection.execute(
                "SELECT record_type, record_id FROM mental_model_claim_evidence WHERE claim_id = ?",
                (row["id"],),
            ).fetchall()
            actual_pairs.update((claim_index, int(item["record_id"])) for item in evidence
                                if item["record_type"] == "fact")
        expected_pairs = set(expected_adjudication_pairs(basis))
        if actual_pairs != expected_pairs:
            raise MentalModelError("adjudication receipt basis no longer matches stored evidence")

        cursor = connection.execute(
            "INSERT INTO mental_model_evaluations (version_id, dimension, score, rationale, "
            "sensitivity_tier, evaluated_by_actor_id) VALUES (?, ?, 0.0, ?, ?, ?)",
            (version_id, ADJUDICATION_RUN_DIMENSION, rationale,
             int(version["sensitivity_tier"]), actor_id),
        )
        evaluation_id = int(cursor.lastrowid)
        emit_audit_and_event(
            connection, action_type="mental_model.adjudication_recorded",
            target_type="mental_model_version", target_id=version_id, actor_id=actor_id,
            reason=None, event_type="mental_model.adjudication_recorded",
            summary=f"Version adjudication recorded: {parsed.status}",
        )
    return evaluation_id


def staleness_check(database_path: Path, *, version_id: int) -> tuple[StaleInput, ...]:
    """The §3b signal: compare
    every input's recorded (content hash, authority class, epistemic status) against CURRENT
    values. A drift on ANY axis — including a byte-identical statement whose epistemic status
    changed — is reported. Returns only when every input still matches: 'explicitly fresh'
    (ledger row 247), never fresh-by-silence. A vanished input record is itself a drift."""
    drifts: list[StaleInput] = []
    with connect_readonly(database_path) as connection:
        inputs = connection.execute(
            "SELECT record_type, record_id, content_sha256, authority_class, epistemic_status, "
            "input_sensitivity_tier "
            "FROM mental_model_version_inputs WHERE version_id = ? ORDER BY id",
            (version_id,),
        ).fetchall()
        if not inputs:
            raise MentalModelError(f"version not found or has no inputs: {version_id}")
        for inp in inputs:
            if inp["record_type"] != "fact":
                raise MentalModelError(
                    f"unsupported input record_type in receipt: {inp['record_type']!r}"
                )
            row = connection.execute(
                "SELECT id, statement, status, superseded_by_fact_id, sensitivity_tier, "
                "COALESCE(authority_class, 'legacy_unclassified') AS authority_class "
                "FROM facts WHERE id = ?",
                (inp["record_id"],),
            ).fetchone()
            if row is None:
                drifts.append(StaleInput(
                    record_type="fact", record_id=int(inp["record_id"]),
                    field="existence", recorded="present", current="missing",
                ))
                continue
            current_hash = content_sha256(str(row["statement"]))
            if current_hash != inp["content_sha256"]:
                drifts.append(StaleInput(
                    record_type="fact", record_id=int(inp["record_id"]),
                    field="content_sha256", recorded=str(inp["content_sha256"]), current=current_hash,
                ))
            if str(row["authority_class"]) != str(inp["authority_class"]):
                drifts.append(StaleInput(
                    record_type="fact", record_id=int(inp["record_id"]),
                    field="authority_class",
                    recorded=str(inp["authority_class"]), current=str(row["authority_class"]),
                ))
            current_status = _fact_epistemic_status(connection, row)
            if current_status != str(inp["epistemic_status"]):
                drifts.append(StaleInput(
                    record_type="fact", record_id=int(inp["record_id"]),
                    field="epistemic_status",
                    recorded=str(inp["epistemic_status"]), current=current_status,
                ))
            if int(row["sensitivity_tier"]) != int(inp["input_sensitivity_tier"]):
                drifts.append(StaleInput(
                    record_type="fact", record_id=int(inp["record_id"]),
                    field="sensitivity_tier", recorded=str(inp["input_sensitivity_tier"]),
                    current=str(row["sensitivity_tier"]),
                ))
    return tuple(drifts)


def get_version(database_path: Path, version_id: int) -> dict:
    """The inspect surface: one version with its receipt fields, inputs, claims (each with its
    evidence), contradictions and evaluations — the exact shape the cold-restore byte-equality
    gate compares."""
    with connect_readonly(database_path) as connection:
        version = connection.execute(
            "SELECT * FROM mental_model_versions WHERE id = ?", (version_id,)
        ).fetchone()
        if version is None:
            raise MentalModelError(f"version not found: {version_id}")
        inputs = connection.execute(
            "SELECT record_type, record_id, content_sha256, authority_class, epistemic_status, "
            "input_sensitivity_tier FROM mental_model_version_inputs WHERE version_id = ? ORDER BY id",
            (version_id,),
        ).fetchall()
        claims = connection.execute(
            "SELECT id, claim_index, text, kind, confidence, sensitivity_tier, content_sha256 "
            "FROM mental_model_claims WHERE version_id = ? ORDER BY claim_index",
            (version_id,),
        ).fetchall()
        evidence = connection.execute(
            "SELECT claim_id, record_type, record_id, content_sha256 "
            "FROM mental_model_claim_evidence WHERE claim_id IN "
            "(SELECT id FROM mental_model_claims WHERE version_id = ?) ORDER BY id",
            (version_id,),
        ).fetchall()
        contradictions = connection.execute(
            "SELECT claim_id, contradicts_claim_id, rationale, sensitivity_tier "
            "FROM mental_model_contradictions WHERE claim_id IN "
            "(SELECT id FROM mental_model_claims WHERE version_id = ?) ORDER BY id",
            (version_id,),
        ).fetchall()
        evaluations = connection.execute(
            "SELECT dimension, score, rationale, sensitivity_tier, evaluated_by_actor_id, created_at "
            "FROM mental_model_evaluations WHERE version_id = ? ORDER BY id",
            (version_id,),
        ).fetchall()
    return {
        "version": {k: version[k] for k in version.keys()},
        "inputs": [dict(r) for r in inputs],
        "claims": [dict(r) for r in claims],
        "evidence": [dict(r) for r in evidence],
        "contradictions": [dict(r) for r in contradictions],
        "evaluations": [dict(r) for r in evaluations],
    }


def get_version_model_id(database_path: Path, version_id: int) -> int:
    """Return only the owning definition id, for authorization-before-content callers."""
    with connect_readonly(database_path) as connection:
        row = connection.execute(
            "SELECT mental_model_id FROM mental_model_versions WHERE id = ?", (version_id,)
        ).fetchone()
    if row is None:
        raise MentalModelError("version unavailable")
    return int(row["mental_model_id"])
