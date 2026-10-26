import json
import traceback
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from ledger_memory.authenticated_context import is_authentic
from ledger_memory.client_auth import record_activity, verify_device_token  # the ONE credential-authority door
from ledger_memory.claim_envelope import content_sha256
from ledger_memory.db import connect
from ledger_memory.fact_authority import AUTHORITY_CLASSES, MATERIALIZATIONS
from ledger_memory.interaction_authorization import (
    MINTING_SURFACES,
    OWNER_SUBJECT,
    PURPOSE_MATERIALIZE_T1,
    PURPOSE_TOOL_READ_T1,
    ConsumeResult,
    ProofHandle,
    consume_interaction_authorization,
)
from ledger_memory.presence import effective_ceiling, is_present
from ledger_memory.tiers import may_see


class TieredReadRefused(Exception):
    pass


# A read is a bounded unit of work, not an open-ended stream. Without a cap the CALLER sets how long
# authority stays resolved: 400 candidates kept a revoked principal reading for 0.55s after revocation
# was issued, and nothing stopped a caller passing 400,000. Revocation cannot be prompt if the thing it
# must interrupt has no length limit. A caller needing more pages the request and is re-authorised each
# time — which is the point.
MAX_CANDIDATES = 500

# provenance is content (Phase A finding B1). Two fixed, content-free markers: a
# title that crosses the facts/imported_sources authorization boundary without clearing it, and a
# supersession pointer, both ship as CONSTANTS, never as the withheld value or the target id itself —
# the raw id/title would be exactly the leak this closes.
SOURCE_WITHHELD = "[source withheld]"
SUPERSEDED_MARKER = "[older belief — superseded; mention only as history]"

# moved from about_me_recall.py so tiered_read's OWN materialization stage can apply the
# same cap/single-line treatment to the source label it now ships (invariant 5 — metadata is untrusted
# prompt data). about_me_recall imports these back rather than keeping a second copy: two definitions
# of "cap" are two definitions that can drift.
_STATEMENT_CAP = 280
_SOURCE_CAP = 80


def _one_line(s: str, cap: int) -> str:
    """Memory statements/titles are attacker-influenceable (pasted web/email/code, years ago) and are
    interpolated into the TRUSTED system block — collapse ALL whitespace (incl. newlines, so a value
    can never forge additional block lines), strip non-printable/control chars, and cap the length."""
    cleaned = " ".join(s.split())
    cleaned = "".join(ch for ch in cleaned if ch.isprintable())
    return cleaned[:cap]


ENVELOPE_VERSION = 1
_AUTHORITY_CLASSES = AUTHORITY_CLASSES
_MATERIALIZATIONS = MATERIALIZATIONS


@dataclass(frozen=True)
class Provenance:
    authority_class: str | None
    materialization: str | None
    asserted_by: str | None
    content_author: str | None
    origin: str | None
    imported: bool
    confidence: float | int | None
    valid_from: str | None
    valid_to: str | None
    evidence_count: int | None
    evidence_relationships: tuple[str, ...]
    attestation: str


def _provenance_date(value) -> str | None:
    return ("".join(ch for ch in str(value)[:32] if ch in "0123456789-T: .")[:19] or None) if value else None


def _provenance_text(value) -> str | None:
    if value is None:
        return None
    return _one_line(str(value), len(str(value))).replace("{", "").replace("}", "")[:40]


def _provenance_inputs(connection, fact_rows):
    """Batch only attribution and evidence state, never evidence details or free-text kinds."""
    actors = {}
    actor_ids = sorted({r["asserted_by_actor_id"] for r in fact_rows if r["asserted_by_actor_id"] is not None})
    if actor_ids:
        placeholders = ",".join("?" for _ in actor_ids)
        actors = {r["id"]: _provenance_text(r["name"]) for r in connection.execute(
            f"SELECT id, name FROM actors WHERE id IN ({placeholders})", actor_ids)}
    evidence = {}
    ids = [r["id"] for r in fact_rows]
    if ids:
        placeholders = ",".join("?" for _ in ids)
        for r in connection.execute(
            "SELECT e.fact_id, COUNT(*) AS evidence_count, "
            "GROUP_CONCAT(DISTINCT CASE WHEN e.relationship IN ('attests','supports','verifies') "
            "THEN e.relationship ELSE 'other' END) AS kinds, "
            "CASE WHEN SUM(a.verification_status = 'rejected') > 0 THEN 'rejected' "
            "WHEN SUM(a.verification_status = 'unverified') > 0 THEN 'unverified' "
            "WHEN SUM(a.verification_status = 'verified') > 0 THEN 'verified' ELSE 'none' END AS attestation "
            "FROM fact_evidence e LEFT JOIN source_attestations a "
            "ON e.evidence_type = 'source_attestation' AND a.id = e.evidence_id "
            f"WHERE e.fact_id IN ({placeholders}) GROUP BY e.fact_id", ids,
        ):
            evidence[r["fact_id"]] = (r["evidence_count"], tuple(sorted(r["kinds"].split(","))), r["attestation"])
    return actors, evidence


def _provenance_from_row(row, actors, evidence, *, withheld):
    count, kinds, state = (None, (), "withheld") if withheld else evidence.get(row["id"], (0, (), "none"))
    authority = row["authority_class"]
    materialization = row["materialization"]
    confidence = row["confidence"]
    return Provenance(
        authority_class=authority if authority in _AUTHORITY_CLASSES else None,
        materialization=materialization if materialization in _MATERIALIZATIONS else None,
        asserted_by=actors.get(row["asserted_by_actor_id"]),
        content_author=_provenance_text(row["content_author"]),
        origin=_provenance_text(row["origin"]),
        imported=row["origin"] != "local" or row["source_record_id"] is not None,
        confidence=confidence if isinstance(confidence, (int, float)) else None,
        valid_from=_provenance_date(row["valid_from"]), valid_to=_provenance_date(row["valid_to"]),
        evidence_count=count, evidence_relationships=kinds, attestation=state,
    )


@dataclass(frozen=True)
class Record:
    record_type: str
    record_id: int
    sensitivity_tier: int
    content: str
    # defaulted so every existing constructor call (there is exactly ONE, below)
    # and every existing consumer (v1_service, the reachability fixture, prior test files) keeps
    # working unchanged: attribute access with these defaults is indistinguishable from "not carried".
    known_at: str | None = None
    source_label: str | None = None
    current: bool = True
    superseded: bool = False
    provenance: Provenance | None = None
    content_sha256: str | None = None


def _new_record(*, record_type, record_id, sensitivity_tier, content, known_at, source_label, current,
                superseded, provenance) -> Record:
    """The ONE place a `Record` is constructed (pinned by test_the_record_constructor_still_has_exactly_one_call_site).
    facts and decisions both come through here, and both builders are reached only from `_materialize`
    after the stage-3 tier re-check — one constructor, so there is still one place authorization could be forgotten."""
    return Record(
        record_type=record_type, record_id=record_id, sensitivity_tier=sensitivity_tier, content=content,
        known_at=known_at, source_label=source_label, current=current, superseded=superseded,
        provenance=provenance, content_sha256=content_sha256(content),
    )


def _record_from_fact_row(row, actors, evidence, *, source_label, withheld) -> Record:
    """Build the single authorized fact-record shape used by materialization and diagnostics."""
    superseded = bool(row["superseded_by_fact_id"] is not None or row["status"] != "active")
    # Untrusted prompt data (invariant 5): whitelist dates rather than only capping them; a
    # newline in the first 19 characters could otherwise break the rendered memory block.
    return _new_record(
        record_type="fact", record_id=int(row["id"]),
        sensitivity_tier=int(row["sensitivity_tier"]), content=str(row["statement"]),
        known_at=_provenance_date(row["known_at"]), source_label=source_label,
        current=not superseded, superseded=superseded,
        provenance=_provenance_from_row(row, actors, evidence, withheld=withheld),
    )


def _record_from_decision_row(row) -> Record:
    """Build the authorized decision-record shape used by materialization."""
    return _new_record(
        record_type="decision", record_id=int(row["id"]),
        sensitivity_tier=int(row["sensitivity_tier"]), content=f"{row['title']} — {row['summary']}",
        known_at=_provenance_date(row["created_at"]), source_label=None,
        current=row["status"] in ("proposed", "accepted"), superseded=row["status"] == "superseded",
        provenance=None,
    )


# the closed set of record types this door returns; the table name never comes from a caller.
_TABLE_BY_TYPE = {"fact": "facts", "decision": "decisions"}

def _authorize(db_path, candidates, ceiling) -> list[tuple[str, int, int]]:
    """Stage 1 — decide WHAT may be read, touching no content.

    Returns [(record_type, record_id, sensitivity_tier)]. Deliberately selects `sensitivity_tier` alone: if this
    stage loaded `statement`, the content would live in a stack frame during the audit write, and an
    audit failure would leave it reachable through the raised exception's traceback.
    """
    authorized = []
    for candidate in candidates:
        table = _TABLE_BY_TYPE.get(candidate.record_type)
        if table is None:
            continue
        with connect(db_path) as connection:
            row = connection.execute(
                f"SELECT sensitivity_tier FROM {table} WHERE id = ?", (candidate.record_id,)
            ).fetchone()
        if row is None:
            continue
        row_tier = row["sensitivity_tier"]
        if may_see(row_tier, ceiling):
            authorized.append((candidate.record_type, candidate.record_id, row_tier))
    return authorized


def _materialize(db_path, authorized, ceiling, *, connection=None) -> list[Record]:
    """Stage 3 — fetch content AND provenance for ids the audit has ALREADY committed for.

    The tier is re-checked at the row we actually fetch, not carried from stage 1: between the two
    stages the row could have been re-classified, and stage 1's verdict would then be stale.

    ONE batched IN-clause query per table, never per-row connects — the
    latency parity is a real budget, and N authorized ids used to mean N separate connections. The
    `imported_sources` row's tier is read in the SAME query that ships its title, so a row reclassified
    above-ceiling between stage 1 and this call is caught here, not from a stale earlier read.
    """
    records = []
    try:
        if not authorized:
            return records

        # stage 1 now yields (record_type, record_id, tier). Direct callers that still pass the
        # pre-1459 (record_id, tier) pairs (mental_models, older tests) mean facts — read them as facts.
        normalized = []
        for item in authorized:
            if len(item) == 3 and isinstance(item[0], str):
                normalized.append(tuple(item))
            elif len(item) == 2:
                normalized.append(("fact", *item))
            else:  # review NIT: a malformed item is refused, never coerced into some record type
                raise TypeError("authorized items are (record_type, record_id, tier) or legacy (record_id, tier)")
        authorized = normalized
        fact_authorized = [(rid, rt) for rtype, rid, rt in authorized if rtype == "fact"]
        decision_authorized = [(rid, rt) for rtype, rid, rt in authorized if rtype == "decision"]

        connection_context = connect(db_path) if connection is None else nullcontext(connection)
        with connection_context as connection:
            fact_by_id = {}
            sources_by_id = {}
            fact_rows = ()
            if fact_authorized:
                fact_ids = [rid for rid, _ in fact_authorized]
                placeholders = ",".join("?" * len(fact_ids))
                fact_rows = connection.execute(
                    "SELECT id, sensitivity_tier, statement, known_at, status, superseded_by_fact_id, "
                    "authority_class, materialization, asserted_by_actor_id, content_author, confidence, valid_from, valid_to, "
                    f"origin, source_db, source_record_id FROM facts WHERE id IN ({placeholders})",
                    fact_ids,
                ).fetchall()
                fact_by_id = {row["id"]: row for row in fact_rows}

                source_ids = sorted({
                    row["source_record_id"] for row in fact_rows if row["source_record_id"] is not None
                })
                if source_ids:
                    src_placeholders = ",".join("?" * len(source_ids))
                    source_rows = connection.execute(
                        "SELECT id, import_origin, sensitivity_tier, conversation_title "
                        f"FROM imported_sources WHERE id IN ({src_placeholders})",
                        source_ids,
                    ).fetchall()
                    sources_by_id = {row["id"]: row for row in source_rows}

            decision_by_id = {}
            decision_rows = ()
            if decision_authorized:
                decision_ids = [rid for rid, _ in decision_authorized]
                placeholders = ",".join("?" * len(decision_ids))
                decision_rows = connection.execute(
                    "SELECT id, sensitivity_tier, title, summary, status, created_at "
                    f"FROM decisions WHERE id IN ({placeholders})",
                    decision_ids,
                ).fetchall()
                decision_by_id = {row["id"]: row for row in decision_rows}

            actors, evidence = _provenance_inputs(connection, fact_rows) if fact_authorized else ({}, {})

        for rtype, record_id, row_tier in authorized:
            if rtype == "fact":
                row = fact_by_id.get(record_id)
                if row is None or row["sensitivity_tier"] != row_tier:
                    continue

                # Three arms, three authorization stories — "the source column" does not exist.
                # `origin`/`source_db` live on the already-authorized `facts` row: usable directly.
                # `conversation_title` crosses a table boundary into `imported_sources`, whose row is
                # independently and MAYBE more sensitive; it ships only when that row itself passes
                # `may_see`, never falling back to the (also-visible) `source_db`/`origin` on withhold —
                # that fallback would leak a proxy signal about the very thing being withheld.
                source_row = sources_by_id.get(row["source_record_id"])
                if source_row is not None and source_row["import_origin"] != row["origin"]:
                    source_row = None  # mirrors the join predicate: a mismatched pointer is no match at all
                title = source_row["conversation_title"] if source_row is not None else None
                if title:
                    label = title if may_see(source_row["sensitivity_tier"], ceiling) else SOURCE_WITHHELD
                else:
                    label = row["source_db"] or row["origin"]
                source_label = _one_line(label, _SOURCE_CAP) if label else None

                # Supersession ships as a BOOLEAN, never the target id (invariant 3 — the raw id is the
                # leak). "Current" mirrors CURRENT_FACT_SQL's own meaning (facts.py); the legacy path's
                # cross-row duplicate-collapse is NOT reproduced here (out of scope, stated).
                records.append(_record_from_fact_row(
                    row, actors, evidence, source_label=source_label,
                    withheld=(source_row is not None
                              and not may_see(source_row["sensitivity_tier"], ceiling)),
                ))
            elif rtype == "decision":
                row = decision_by_id.get(record_id)
                # the same stale-tier drop a fact gets — a decision re-classified between stage 1
                # and this fetch is not returned. Title and summary only; `rationale` is never selected.
                if row is None or row["sensitivity_tier"] != row_tier:
                    continue
                records.append(_record_from_decision_row(row))
    except BaseException as error:
        # Clear the partial results before unwinding. A traceback keeps every frame's locals alive, so
        # a fault here would otherwise leave already-materialized content AND metadata reachable to any
        # caller that catches the exception and walks `__traceback__` — the same escape route the audit
        # path was restructured to close. Review BLOCK (2026-08-07): the LOOP LOCALS carry content and
        # metadata too — the frames outlive the function; every one is severed before the re-raise.
        records.clear()
        del records
        row = source_row = title = label = source_label = None
        known_at = raw_known_at = None
        fact_by_id = sources_by_id = fact_rows = source_rows = None
        actors = evidence = None
        del actors, evidence
        del row, source_row, title, label, source_label, known_at, raw_known_at
        del fact_by_id, sources_by_id, fact_rows, source_rows
        decision_by_id = decision_rows = None
        del decision_by_id, decision_rows
        traceback.clear_frames(error.__traceback__)
        raise
    return records


def projection_matches_current_for_diagnostics(database_path, version_input, expected_entry) -> bool:
    """Compare a stored authorized projection to current facts without serving any content.

    Activation has no reader credential and must not mint one. This diagnostic returns only a
    Boolean: statement and provenance values remain local to the read-only check and are never
    returned, hashed into a persisted side channel, or sent to a model. A missing/quarantined row,
    incomplete snapshot, or any inability to reconstruct fails closed.
    """
    from ledger_memory.claim_envelope import content_sha256
    from ledger_memory.db import connect_readonly
    from ledger_memory.mental_model_provenance import input_entry_from_record
    from ledger_memory.mental_models import _derive_fact_input

    row = actors = evidence = record = snapshot = None
    try:
        with connect_readonly(database_path) as connection:
            row = connection.execute(
                "SELECT id, sensitivity_tier, statement, known_at, status, "
                "superseded_by_fact_id, authority_class, materialization, "
                "asserted_by_actor_id, content_author, confidence, valid_from, valid_to, "
                "origin, source_db, source_record_id, quarantined FROM facts WHERE id = ?",
                (version_input.record_id,),
            ).fetchone()
            if row is None or bool(row["quarantined"]):
                return False
            snapshot = _derive_fact_input(connection, int(version_input.record_id))
            actors, evidence = _provenance_inputs(connection, [row])
            record = _record_from_fact_row(
                row, actors, evidence, source_label=None,
                withheld=expected_entry["provenance"]["evidence_count"] is None,
            )
        actual_entry = input_entry_from_record(record, snapshot)
        return snapshot == version_input and dict(expected_entry) == actual_entry
    except BaseException as error:
        row = actors = evidence = record = snapshot = None
        traceback.clear_frames(error.__traceback__)
        return False


# the purposes a read may consume a proof for. Closed: a proof is only ever consumed for the purpose it was
# minted for, so naming one can never widen what is read.
PROOF_PURPOSES = frozenset({PURPOSE_MATERIALIZE_T1, PURPOSE_TOOL_READ_T1})


def _check_proof_purpose(proof_purpose) -> None:
    if proof_purpose not in PROOF_PURPOSES:
        raise ValueError(f"proof_purpose must be one of {sorted(PROOF_PURPOSES)}, got {proof_purpose!r}")


def resolve_read_authority(context, *, database_path=None, surface=None,
                           interaction_proof: ProofHandle | None = None,
                           proof_purpose: str = PURPOSE_MATERIALIZE_T1) -> tuple[int, int]:
    """Resolve identity from the credential itself, plus revocation and the existing clamp.

    `verify_device_token` is called DIRECTLY, not via `context.resolve()`. Calling the context's
    own method looked equivalent, but it routes identity through a method the CALLER owns. A
    duck-typed context carrying a valid low-tier token and its own `resolve()` returning another
    device made `is_authentic` return True (correctly: the token is real) and then handed
    `tiered_read` an identity the token never established. Identity comes from the verified
    credential and nothing else; the context supplies material to verify, never a conclusion.
    Fresh verification on each read means a cached ceiling cannot outlive revocation.

    For proof-based reads, the issuer is derived from the database actually being read, never
    taken from the caller. This makes cross-database nonce replay fail closed even for structurally
    identical databases. A minting surface without a valid proof clamps to non-present and audits
    the absent-proof outcome; it never falls back to ambient `last_activity_at`, which could let a
    T2/T3 or unbound device read T1 merely because another runtime credential is active. Non-
    minting surfaces retain their existing presence clamp until their own migration changes it.
    """
    _check_proof_purpose(proof_purpose)
    if not is_authentic(context):
        raise TieredReadRefused("context is not authentic")
    if database_path is not None and Path(database_path) != Path(context.database_path):
        raise TieredReadRefused("context database mismatch")
    db_path = Path(context.database_path)
    device = verify_device_token(db_path, getattr(context, "token", "") or "")
    if device is None or device.revoked_at is not None:
        raise TieredReadRefused("credential no longer verifies, or the device is revoked")
    with connect(db_path) as connection:
        row = connection.execute(
            "SELECT tier_ceiling, revoked_at, last_activity_at FROM client_devices WHERE id = ?",
            (device.id,),
        ).fetchone()
    if row is None or row["revoked_at"] is not None:
        raise TieredReadRefused("device not found or revoked")
    if interaction_proof is not None:
        from ledger_memory.brain_identity import get_brain_identity

        outcome = consume_interaction_authorization(
            interaction_proof.nonce, issuer=get_brain_identity(db_path).fingerprint,
            subject=OWNER_SUBJECT, surface=surface or "unknown",
            purpose=proof_purpose, request_id=interaction_proof.request_id,
        )
        present = outcome.authorization is not None
        _audit_interaction_authorization_outcome(db_path, outcome=outcome, surface=surface)
    elif surface in MINTING_SURFACES:
        present = False
        _audit_interaction_authorization_outcome(
            db_path, outcome=_absent_proof_outcome(), surface=surface,
        )
    else:
        present = is_present(row["last_activity_at"], datetime.now(timezone.utc).isoformat())
    return int(device.id), effective_ceiling(row["tier_ceiling"], present=present)


def tiered_read(
    context, candidates, *, database_path=None, surface=None, interaction_proof: ProofHandle | None = None,
    tier_floor: int | None = None, proof_purpose: str = PURPOSE_MATERIALIZE_T1,
) -> list[Record]:
    """The one door content leaves by. Authorize → audit → materialize, in that order.

    **The presence clamp is not optional and takes no caller argument.** It was briefly a
    `presence_clamp=False` keyword, which meant any caller could disable the control by simply not
    passing it, and the default disabled it. A security control a caller can switch off is
    a suggestion. Presence is read from the stored `last_activity_at` and applied unconditionally.

    **an earlier fix stage 1362a — `interaction_proof` is OPTIONAL, and the spec pins BOTH of its branches
    (Staging, "Stage 1362a intersection semantics"):**

    - **Present:** verified and atomically consumed via `consume_interaction_authorization`, and it
      ALONE decides the T1 condition — `last_activity_at` is never consulted on this path. A proof that
      fails to verify (replayed, expired, wrong issuer/subject/surface) is treated exactly like an
      absent presence signal: T1 clamps to T2 (Part B2), it does NOT fall back to checking
      `last_activity_at` — that fallback would be a downgrade attack (an attacker who cannot forge a
      proof degrades to the ambient clamp instead).
    - **Absent:** the LEGACY presence clamp applies exactly as before this slice — byte-identical, no
      regression. This is the only path chat/voice and any not-yet-migrated caller take until
      1362b/1362c land.

    Neither branch changes what `effective_ceiling` does with a NON-T1 device ceiling: both only ever
    decide the T1-vs-T2 question already asked below, never grant T1 to a T2/T3 credential (Part B1 —
    two independent dimensions, neither substitutes for nor upgrades the other).

    **The stage order is the defence, not an implementation detail.** Content is materialized only
    AFTER the audit has committed, so there is no execution window in which record text exists in a
    frame that can raise. The audit therefore records an AUTHORIZED read; stage 3 may legitimately
    return fewer rows if a record was re-classified or deleted in between, and `record_count` is the
    authorized count.

    `surface` names where the read originated ("telegram", "voice", "api-chat", …) and
    rides straight into the ONE audit row this call writes. It exists so a caller routed through the
    chokepoint does not lose its surface label —
    the read log keeps exactly one row per read either way (§2.4), now carrying BOTH the real
    `device_id` and the surface, never a caller-supplied ceiling or record count.
    """
    # `tier_floor` only NARROWS. A higher tier number is less private, so max can hide a record and never
    # reveal one; the audit row below records the floored ceiling. Checked before authority is resolved, so a bad
    # value has no side effect.
    if tier_floor is not None and (
        isinstance(tier_floor, bool) or not isinstance(tier_floor, int) or not 1 <= tier_floor <= 3
    ):
        raise ValueError(f"tier_floor must be 1, 2 or 3, got {tier_floor!r}")
    db_path = context.database_path
    _check_proof_purpose(proof_purpose)  # before any side effect, like tier_floor
    device_id, ceiling = resolve_read_authority(
        context, database_path=database_path, surface=surface, interaction_proof=interaction_proof,
        proof_purpose=proof_purpose,
    )
    if tier_floor is not None:
        ceiling = max(ceiling, tier_floor)

    candidates = list(candidates)
    if len(candidates) > MAX_CANDIDATES:
        raise TieredReadRefused(
            f"refused: {len(candidates)} candidates exceeds the {MAX_CANDIDATES} per-read cap. "
            "Page the request; each page re-resolves authority."
        )

    authorized = _authorize(db_path, candidates, ceiling)

    try:
        record_read_audit(
            db_path, device_id=device_id, record_count=len(authorized), ceiling=ceiling,
            surface=surface,
        )
    except Exception:
        # No content exists in any live frame at this point — that is what the staging buys. The
        # refusal carries no detail from `exc` either, so nothing leaks through the message.
        raise TieredReadRefused(
            "read refused: the audit write failed, so no content was read"
        ) from None

    # Re-verify the credential immediately before content is fetched. Authority was resolved at the top
    # of this call; everything between then and now is a window in which a revocation can land. Checking
    # again here does not make revocation atomic — nothing in-process can — but it bounds the window to
    # one stage instead of the whole call, and combined with MAX_CANDIDATES the whole call is bounded.
    if verify_device_token(Path(db_path), getattr(context, "token", "") or "") is None:
        raise TieredReadRefused("credential was revoked during the read; no content returned")

    try:
        return _materialize(db_path, authorized, ceiling)
    except TieredReadRefused:
        raise
    except Exception:
        # A stage-3 fault must not carry content out. `_materialize` clears its own locals before
        # re-raising (see there); this converts whatever is left into a refusal that carries nothing
        # from the original exception — not its message, not its chain.
        raise TieredReadRefused(
            "read refused: content materialization failed, so no content is returned"
        ) from None


def _absent_proof_outcome() -> ConsumeResult:
    """The refusal recorded when a MINTING surface presents no proof at all.

    Distinguishable in the audit from a proof that was presented and failed: this reason means the
    ingress that should have minted did not, which is the shape a refused mint (T2/T3 device, revoked
    device, absent bearer token, or a mint that errored) leaves behind.
    """
    return ConsumeResult(authorization=None, reason="absent_proof_on_minting_surface")


def _audit_interaction_authorization_outcome(database_path, *, outcome, surface) -> None:
    """Part B4 — 'the audit names the evidence kind and the opaque proof/request id, never raw channel
    identifiers or credentials.' Best-effort telemetry (mirrors `_touch_device_quietly`'s
    never-break-the-caller discipline elsewhere in this codebase) into the EXISTING `events` table —
    no schema change, and never the raw nonce: only the sha256-derived `proof_id` an
    `InteractionAuthorization` already carries, or (on refusal) the distinguishable reason string.
    This is supplementary to `record_read_audit` above, never a replacement for it — the read itself is
    still audited exactly once, there, regardless of whether a proof was presented."""
    try:
        from ledger_memory.emission import emit_event_only

        if outcome.authorization is not None:
            event_type = "interaction_authorization.consumed"
            metadata = {
                "proof_id": outcome.authorization.proof_id,
                "evidence_kind": outcome.authorization.evidence_kind,
                "surface": surface or "unknown",
            }
        else:
            event_type = "interaction_authorization.refused"
            metadata = {"reason": outcome.reason, "surface": surface or "unknown"}
        with connect(Path(database_path)) as connection:
            emit_event_only(
                connection, event_type=event_type, summary=event_type, metadata=json.dumps(metadata),
            )
    except Exception:
        # Telemetry must never break a read that otherwise succeeded (or correctly failed closed) —
        # the SAME discipline `_touch_device_quietly` and `refresh_runtime_presence` already apply.
        pass


def record_read_audit(database_path, *, device_id, record_count, ceiling, surface=None) -> None:
    """Write EXACTLY ONE row per returning read, into the read log's own table.

    Not `audit_entries`, for two independent reasons:

    1. **Retention.** The retention machinery selects whole TABLES (`selector_scope` is nullable and
       v1-unused, so there is no row-level filter). A 30-day read retention pointed at
       `audit_entries` would delete the governance audit trail every month.
    2. **Attribution.** `audit_entries.actor_id` is a FOREIGN KEY into `actors`. A device id is not an
       actor id; writing one there either fails outright or, worse, silently attributes a read to
       whichever actor happens to share that integer.

    One row per READ, never one per record: per-record rows would make this log a copy of the read.
    And no content column exists to write to — this records THAT a read happened, never what it said.

    `surface` names where the read originated ("api-chat", "telegram", "voice", "v1-context");
    'unknown' is a valid recorded value, never an error. `device_id` may be None ONLY for funnel
    reads, whose principal is a channel rather than a paired device — None means "not a
    device", it never means "nobody", and inventing a device id to satisfy a NOT NULL was the exact
    fabrication the baseline schema's exists to make unnecessary.
    """
    with connect(Path(database_path)) as connection:
        connection.execute(
            "INSERT INTO read_audit_entries (device_id, record_count, effective_ceiling, surface) "
            "VALUES (?, ?, ?, ?)",
            (None if device_id is None else int(device_id), int(record_count), int(ceiling),
             surface or "unknown"),
        )


def read_audit_capable(database_path) -> bool:
    """Deterministic schema probe: True iff the read log exists in its migration-048 shape.

    A PROBE, never an exception match — catching OperationalError to detect a missing table lets a
    locked database impersonate a missing capability, turning fail-closed into fail-open on exactly
    the contended-write case the audit must survive.
    """
    with connect(Path(database_path)) as connection:
        table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='read_audit_entries'"
        ).fetchone()
        if table is None:
            return False
        columns = {row[1] for row in connection.execute("PRAGMA table_info(read_audit_entries)")}
        return "surface" in columns


__all__ = ["Record", "TieredReadRefused", "record_activity", "record_read_audit", "tiered_read"]
