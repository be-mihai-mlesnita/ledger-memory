"""Strict, text-free provenance receipts for derived mental-model conclusions."""
from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from typing import Any, Mapping

from ledger_memory.fact_authority import AUTHORITY_CLASSES, MATERIALIZATIONS

BASIS_DIMENSION = "derivation_basis_v1"
ADJUDICATION_RUN_DIMENSION = "adjudication_run_v1"
MAX_RECEIPT_BYTES = 1024 * 1024
ORIGINAL_AVAILABILITY = frozenset((
    "retained", "not_retained", "confirmed_unavailable", "unreachable", "unknown",
))
SEMANTIC_SCOPES = frozenset((
    "reported_assertion", "recorded_observation", "prediction", "interpretation", "unspecified",
))
WHOLE_SET_RESULTS = frozenset((
    "supported_as_phrased", "needs_qualification", "contradicted", "insufficient",
))
PAIR_RESULTS = frozenset(("SUPPORTS", "REFUTES", "unavailable"))
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_PROVENANCE_KEYS = frozenset((
    "authority_class", "materialization", "asserted_by", "content_author", "origin",
    "imported", "confidence", "valid_from", "valid_to", "evidence_count",
    "evidence_relationships", "attestation",
))
_ROOT_KEYS = frozenset(("v", "renderer_version", "recipe_sha256", "prompt_sha256",
                        "definition", "inputs", "claims"))
_INPUT_KEYS = frozenset((
    "record_type", "record_id", "content_sha256", "authority_class", "epistemic_status",
    "sensitivity_tier", "provenance", "known_at", "current", "superseded",
    "original_availability", "projection_sha256",
))


@dataclass(frozen=True)
class DerivationBasis:
    v: int
    renderer_version: str
    recipe_sha256: str
    prompt_sha256: str
    definition: Mapping[str, Any]
    inputs: tuple[Mapping[str, Any], ...]
    claims: tuple[Mapping[str, Any], ...]


@dataclass(frozen=True)
class ProvenanceProfile:
    authorities: tuple[str, ...]
    attestation_states: tuple[str, ...]
    materializations: tuple[str, ...]
    original_availability: tuple[str, ...]
    conclusion_authority: str
    verification: str
    independence: str
    permission: str

    @property
    def authority(self) -> str:
        return self.authorities[0] if len(self.authorities) == 1 else "mixed"


@dataclass(frozen=True)
class AdjudicationPair:
    claim_index: int
    fact_id: int
    result: str


@dataclass(frozen=True)
class AdjudicationClaim:
    claim_index: int
    whole_set: str


@dataclass(frozen=True)
class AdjudicationRun:
    v: int
    run_id: str
    version_id: int
    basis_fingerprint: str
    judge_model: str
    judge_recipe_sha256: str
    started_at: str
    completed_at: str | None
    status: str
    pairs: tuple[AdjudicationPair, ...]
    claims: tuple[AdjudicationClaim, ...]


def input_entry_from_record(record, version_input) -> dict[str, Any]:
    from ledger_memory.claim_envelope import content_sha256

    if (record.record_type != version_input.record_type or
            int(record.record_id) != int(version_input.record_id) or
            record.content_sha256 != version_input.content_sha256 or
            content_sha256(str(record.content)) != record.content_sha256 or
            int(record.sensitivity_tier) != int(version_input.sensitivity_tier)):
        raise ValueError("materialized record does not match its stored input snapshot")
    source = record.provenance
    if source is None:
        raise ValueError("authorized record omitted its provenance envelope")
    snapshot_authority = source.authority_class or "legacy_unclassified"
    if snapshot_authority != version_input.authority_class:
        raise ValueError("materialized record authority does not match its stored snapshot")
    safe = {
        "authority_class": source.authority_class,
        "materialization": source.materialization,
        "asserted_by": source.asserted_by,
        "content_author": source.content_author,
        "origin": source.origin,
        "imported": source.imported,
        "confidence": source.confidence,
        "valid_from": source.valid_from,
        "valid_to": source.valid_to,
        "evidence_count": source.evidence_count,
        "evidence_relationships": list(source.evidence_relationships),
        "attestation": source.attestation,
    }
    projection = {
        "record_type": "fact", "record_id": int(record.record_id),
        "content_sha256": version_input.content_sha256,
        "authority_class": version_input.authority_class,
        "epistemic_status": version_input.epistemic_status,
        "sensitivity_tier": int(record.sensitivity_tier), "provenance": safe,
        "known_at": record.known_at, "current": bool(record.current),
        "superseded": bool(record.superseded), "original_availability": "unknown",
    }
    return {**projection, "projection_sha256": input_projection_sha256(record, version_input,
                                                                         projection=projection)}


def input_projection_sha256(record, version_input, *, projection=None) -> str:
    """Fingerprint the permitted projection and ephemeral digest of the actual consumed text.

    The consumed-text digest is intentionally not serialized. Both capture and later authorized
    verification call this function with a freshly materialized Record; the persisted v1 receipt
    retains only its closed key set and opaque projection hash.
    """
    from ledger_memory.claim_envelope import content_sha256

    if (record.record_type != version_input.record_type
            or int(record.record_id) != int(version_input.record_id)
            or record.content_sha256 != version_input.content_sha256
            or content_sha256(str(record.content)) != record.content_sha256
            or int(record.sensitivity_tier) != int(version_input.sensitivity_tier)):
        raise ValueError("materialized record does not match its stored input snapshot")
    if projection is None:
        captured = input_entry_from_record(record, version_input)
        return str(captured["projection_sha256"])
    return canonical_sha256({
        "projection": projection,
        "consumed_text_sha256": content_sha256(str(record.content)),
    })


def render_labeled_input(index: int, entry: Mapping[str, Any], text: str, delimit) -> str:
    metadata = {key: entry[key] for key in (
        "authority_class", "epistemic_status", "sensitivity_tier", "provenance",
        "known_at", "current", "superseded", "original_availability",
    )}
    return (f"Input [{index}] provenance (metadata, not a truth score): {canonical_json(metadata)}\n"
            f"Input [{index}] untrusted DATA: {delimit(text)}")


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False)


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _load_json(raw: str) -> dict:
    if not isinstance(raw, str) or len(raw.encode("utf-8")) > MAX_RECEIPT_BYTES:
        raise ValueError("receipt exceeds 1 MiB or is not text")
    try:
        value = json.loads(raw, object_pairs_hook=_unique_object,
                           parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
    except (json.JSONDecodeError, UnicodeEncodeError) as exc:
        raise ValueError("receipt is not valid JSON") from exc
    if not isinstance(value, dict):
        raise ValueError("receipt root must be an object")
    return value


def _exact_keys(value: Mapping[str, Any], keys: frozenset[str], label: str) -> None:
    if set(value) != keys:
        raise ValueError(f"{label} has missing or unknown fields")


def _hash(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise ValueError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _int(value: Any, label: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{label} must be an integer >= {minimum}")
    return value


def _nullable_text(value: Any, label: str) -> None:
    if value is not None and not isinstance(value, str):
        raise ValueError(f"{label} must be text or null")


def _validate_provenance(value: Any) -> None:
    if not isinstance(value, dict):
        raise ValueError("provenance must be an object")
    _exact_keys(value, _PROVENANCE_KEYS, "provenance")
    if (value["authority_class"] is not None and
            (not isinstance(value["authority_class"], str)
             or value["authority_class"] not in AUTHORITY_CLASSES)):
        raise ValueError("unknown provenance authority class")
    if (value["materialization"] is not None and
            (not isinstance(value["materialization"], str)
             or value["materialization"] not in MATERIALIZATIONS)):
        raise ValueError("unknown provenance materialization")
    for key in ("asserted_by", "content_author", "origin", "valid_from", "valid_to", "attestation"):
        _nullable_text(value[key], f"provenance.{key}")
    if not isinstance(value["imported"], bool):
        raise ValueError("provenance.imported must be Boolean")
    confidence = value["confidence"]
    if confidence is not None and (isinstance(confidence, bool) or not isinstance(confidence, (int, float))
                                   or not math.isfinite(float(confidence))):
        raise ValueError("provenance.confidence must be finite or null")
    count = value["evidence_count"]
    if count is not None:
        _int(count, "provenance.evidence_count")
    rels = value["evidence_relationships"]
    if not isinstance(rels, list) or any(not isinstance(v, str) for v in rels):
        raise ValueError("provenance.evidence_relationships must be a string array")


def parse_derivation_basis(raw: str) -> DerivationBasis:
    value = _load_json(raw)
    _exact_keys(value, _ROOT_KEYS, "derivation basis")
    if _int(value["v"], "v", minimum=1) != 1:
        raise ValueError("unsupported derivation basis version")
    if not isinstance(value["renderer_version"], str) or not value["renderer_version"].strip():
        raise ValueError("renderer_version is required")
    _hash(value["recipe_sha256"], "recipe_sha256")
    _hash(value["prompt_sha256"], "prompt_sha256")
    definition = value["definition"]
    if not isinstance(definition, dict):
        raise ValueError("definition must be an object")
    _exact_keys(definition, frozenset(("id", "fingerprint", "sensitivity_tier")), "definition")
    _int(definition["id"], "definition.id", minimum=1)
    _hash(definition["fingerprint"], "definition.fingerprint")
    if _int(definition["sensitivity_tier"], "definition.sensitivity_tier", minimum=1) not in (1, 2, 3):
        raise ValueError("definition tier must be 1-3")
    inputs = value["inputs"]
    if not isinstance(inputs, list) or not 1 <= len(inputs) <= 500:
        raise ValueError("inputs must contain 1-500 entries")
    ids = set()
    for entry in inputs:
        if not isinstance(entry, dict):
            raise ValueError("input must be an object")
        _exact_keys(entry, _INPUT_KEYS, "input")
        if entry["record_type"] != "fact":
            raise ValueError("unsupported input type")
        rid = _int(entry["record_id"], "record_id", minimum=1)
        if rid in ids:
            raise ValueError("duplicate input id")
        ids.add(rid)
        _hash(entry["content_sha256"], "content_sha256")
        _hash(entry["projection_sha256"], "projection_sha256")
        if (not isinstance(entry["authority_class"], str)
                or entry["authority_class"] not in AUTHORITY_CLASSES):
            raise ValueError("unknown input authority class")
        if not isinstance(entry["epistemic_status"], str) or not entry["epistemic_status"]:
            raise ValueError("epistemic_status is required")
        if _int(entry["sensitivity_tier"], "sensitivity_tier", minimum=1) not in (1, 2, 3):
            raise ValueError("input tier must be 1-3")
        _validate_provenance(entry["provenance"])
        _nullable_text(entry["known_at"], "known_at")
        if not isinstance(entry["current"], bool) or not isinstance(entry["superseded"], bool):
            raise ValueError("input current/superseded must be Boolean")
        if (not isinstance(entry["original_availability"], str)
                or entry["original_availability"] not in ORIGINAL_AVAILABILITY):
            raise ValueError("unknown original availability")
    claims = value["claims"]
    if not isinstance(claims, list) or not claims:
        raise ValueError("claims must be a nonempty array")
    indexes = set()
    for claim in claims:
        if not isinstance(claim, dict):
            raise ValueError("claim must be an object")
        _exact_keys(claim, frozenset(("claim_index", "semantic_scope", "cited_input_indices")), "claim")
        index = _int(claim["claim_index"], "claim_index")
        if index in indexes:
            raise ValueError("duplicate claim index")
        indexes.add(index)
        if (not isinstance(claim["semantic_scope"], str)
                or claim["semantic_scope"] not in SEMANTIC_SCOPES):
            raise ValueError("unknown semantic scope")
        cited = claim["cited_input_indices"]
        if not isinstance(cited, list) or not cited:
            raise ValueError("claim citations must be a nonempty array")
        parsed = [_int(i, "cited input index") for i in cited]
        if len(set(parsed)) != len(parsed) or any(i >= len(inputs) for i in parsed):
            raise ValueError("duplicate or out-of-range cited input index")
    if indexes != set(range(len(claims))):
        raise ValueError("claim indices must be contiguous")
    return DerivationBasis(1, value["renderer_version"], value["recipe_sha256"],
                           value["prompt_sha256"], definition, tuple(inputs), tuple(claims))


def basis_to_dict(basis: DerivationBasis | Mapping[str, Any]) -> dict[str, Any]:
    if isinstance(basis, DerivationBasis):
        return {"v": basis.v, "renderer_version": basis.renderer_version,
                "recipe_sha256": basis.recipe_sha256, "prompt_sha256": basis.prompt_sha256,
                "definition": dict(basis.definition), "inputs": [dict(v) for v in basis.inputs],
                "claims": [dict(v) for v in basis.claims]}
    return dict(basis)


def make_provenance_profile(*, authority_classes, attestation_states, materializations,
                             original_availability, permission: str) -> ProvenanceProfile:
    authorities = tuple(sorted(set(authority_classes)))
    attestations = tuple(sorted(set(attestation_states)))
    mats = tuple(sorted(set(materializations)))
    availability = tuple(sorted(set(original_availability)))
    if not authorities or not set(authority_classes) <= AUTHORITY_CLASSES:
        raise ValueError("invalid authority classes")
    if not set(mats) <= MATERIALIZATIONS or not set(availability) <= ORIGINAL_AVAILABILITY:
        raise ValueError("invalid provenance profile values")
    if permission not in ("permitted", "denied"):
        raise ValueError("permission must be permitted or denied")
    verification = "limited" if any(v != "retained" for v in availability) else "not_established"
    return ProvenanceProfile(authorities, attestations, mats, availability, "model_inferred",
                             verification, "unknown", permission)


def format_basis_profile(profile: ProvenanceProfile) -> str:
    originals = ", ".join(profile.original_availability)
    return (f"{{model inference; originals: {originals}; verification: {profile.verification}; "
            f"independence: {profile.independence}}}")


def parse_adjudication_run(raw: str) -> AdjudicationRun:
    value = _load_json(raw)
    keys = frozenset(("v", "run_id", "version_id", "basis_fingerprint", "judge_model",
                      "judge_recipe_sha256", "started_at", "completed_at", "status", "pairs", "claims"))
    _exact_keys(value, keys, "adjudication run")
    if _int(value["v"], "v", minimum=1) != 1:
        raise ValueError("unsupported adjudication run version")
    for key in ("run_id", "judge_model", "started_at"):
        if not isinstance(value[key], str) or not value[key].strip():
            raise ValueError(f"{key} is required")
    _int(value["version_id"], "version_id", minimum=1)
    _hash(value["basis_fingerprint"], "basis_fingerprint")
    _hash(value["judge_recipe_sha256"], "judge_recipe_sha256")
    _nullable_text(value["completed_at"], "completed_at")
    if value["status"] not in ("completed", "unavailable"):
        raise ValueError("unknown adjudication status")
    if value["status"] == "completed" and not value["completed_at"]:
        raise ValueError("completed adjudication run requires completed_at")
    pairs = value["pairs"]
    claims = value["claims"]
    if not isinstance(pairs, list) or not isinstance(claims, list):
        raise ValueError("pairs and claims must be arrays")
    parsed_pairs = []
    pair_keys = set()
    for item in pairs:
        if not isinstance(item, dict):
            raise ValueError("pair must be an object")
        _exact_keys(item, frozenset(("claim_index", "fact_id", "result")), "pair")
        claim_index = _int(item["claim_index"], "pair.claim_index")
        fact_id = _int(item["fact_id"], "pair.fact_id", minimum=1)
        if (not isinstance(item["result"], str) or item["result"] not in PAIR_RESULTS
                or (claim_index, fact_id) in pair_keys):
            raise ValueError("invalid or duplicate pair")
        pair_keys.add((claim_index, fact_id))
        parsed_pairs.append(AdjudicationPair(claim_index, fact_id, item["result"]))
    parsed_claims = []
    claim_indexes = set()
    for item in claims:
        if not isinstance(item, dict):
            raise ValueError("claim result must be an object")
        _exact_keys(item, frozenset(("claim_index", "whole_set")), "claim result")
        claim_index = _int(item["claim_index"], "claim.claim_index")
        if (claim_index in claim_indexes or not isinstance(item["whole_set"], str)
                or item["whole_set"] not in WHOLE_SET_RESULTS):
            raise ValueError("invalid or duplicate whole-set result")
        claim_indexes.add(claim_index)
        parsed_claims.append(AdjudicationClaim(claim_index, item["whole_set"]))
    return AdjudicationRun(1, value["run_id"], value["version_id"], value["basis_fingerprint"],
                           value["judge_model"], value["judge_recipe_sha256"], value["started_at"],
                           value["completed_at"], value["status"], tuple(parsed_pairs), tuple(parsed_claims))


def expected_adjudication_pairs(basis: DerivationBasis) -> frozenset[tuple[int, int]]:
    """Return the exact (claim index, fact id) coverage required by a derivation basis."""
    return frozenset(
        (int(claim["claim_index"]), int(basis.inputs[index]["record_id"]))
        for claim in basis.claims for index in claim["cited_input_indices"]
    )


def validate_adjudication_coverage(run: AdjudicationRun, basis: DerivationBasis) -> None:
    claim_indexes = set(range(len(basis.claims)))
    expected_pairs = expected_adjudication_pairs(basis)
    actual_pairs = {(pair.claim_index, pair.fact_id) for pair in run.pairs}
    actual_claims = {claim.claim_index for claim in run.claims}
    if actual_pairs != expected_pairs or actual_claims != claim_indexes:
        raise ValueError("adjudication run does not completely cover the derivation basis")


def is_adjudication_qualifying(result: str) -> bool:
    return result == "supported_as_phrased"


def select_latest_complete_run(runs, *, version_id: int, basis_fingerprint: str,
                               judge_recipe_sha256: str,
                               expected_pairs: frozenset[tuple[int, int]],
                               expected_claims: frozenset[int]):
    for run in reversed(list(runs)):
        if isinstance(run, str):
            run = parse_adjudication_run(run)
        elif isinstance(run, AdjudicationRun):
            pass
        else:
            raise ValueError("judge run selector requires strict parsed receipts")
        if (run.version_id != version_id or run.basis_fingerprint != basis_fingerprint
                or run.judge_recipe_sha256 != judge_recipe_sha256 or run.status != "completed"):
            continue
        if ({(p.claim_index, p.fact_id) for p in run.pairs} != expected_pairs
                or {c.claim_index for c in run.claims} != expected_claims):
            raise ValueError("completed adjudication receipt has incomplete or mismatched coverage")
        return adjudication_run_to_dict(run)
    return None


def adjudication_run_to_dict(run: AdjudicationRun) -> dict[str, Any]:
    return {"v": run.v, "run_id": run.run_id, "version_id": run.version_id,
            "basis_fingerprint": run.basis_fingerprint, "judge_model": run.judge_model,
            "judge_recipe_sha256": run.judge_recipe_sha256, "started_at": run.started_at,
            "completed_at": run.completed_at, "status": run.status,
            "pairs": [{"claim_index": p.claim_index, "fact_id": p.fact_id, "result": p.result}
                      for p in run.pairs],
            "claims": [{"claim_index": c.claim_index, "whole_set": c.whole_set}
                       for c in run.claims]}


def latest_applicable_adjudication_run(version: dict, *, judge_recipe_sha256: str):
    basis_rows = [e for e in version["evaluations"] if e["dimension"] == BASIS_DIMENSION]
    if len(basis_rows) != 1:
        return None
    try:
        basis = parse_derivation_basis(basis_rows[0]["rationale"])
    except ValueError:
        return None
    fingerprint = canonical_sha256(basis_to_dict(basis))
    expected_pairs = expected_adjudication_pairs(basis)
    expected_claims = frozenset(range(len(basis.claims)))
    rows = []
    for evaluation in version["evaluations"]:
        if evaluation["dimension"] != ADJUDICATION_RUN_DIMENSION:
            continue
        run = parse_adjudication_run(evaluation["rationale"])
        if (run.status == "completed" and run.version_id == version["version"]["id"]
                and run.basis_fingerprint == fingerprint
                and run.judge_recipe_sha256 == judge_recipe_sha256):
            validate_adjudication_coverage(run, basis)
            rows.append(run)
    return select_latest_complete_run(
        rows, version_id=int(version["version"]["id"]), basis_fingerprint=fingerprint,
        judge_recipe_sha256=judge_recipe_sha256, expected_pairs=expected_pairs,
        expected_claims=expected_claims,
    )


__all__ = ["ADJUDICATION_RUN_DIMENSION", "BASIS_DIMENSION", "DerivationBasis", "ProvenanceProfile",
           "adjudication_run_to_dict", "basis_to_dict", "canonical_json", "canonical_sha256",
           "expected_adjudication_pairs", "input_projection_sha256", "validate_adjudication_coverage",
           "format_basis_profile", "is_adjudication_qualifying", "latest_applicable_adjudication_run",
           "make_provenance_profile", "parse_adjudication_run", "parse_derivation_basis",
           "select_latest_complete_run"]
