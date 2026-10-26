"""Part A — the ONE place `facts.authority_class`/`asserted_by_actor_id` pairing is
validated. Migration 043 added both columns additively (no backfill, no write-side wiring), so every
existing writer left the class at its DEFAULT `legacy_unclassified` and never touched the assertor.
Without a shared primitive, each writer that starts assigning a class would grow its own near-copy of
this check — and a near-copy is exactly how the A2 invariant would drift out of step with itself
(the same "fix-the-class, not the instance" lesson this codebase repeats: an earlier fix's
`_eligibility_clause`, the ingest path's `looks_like_credential`).

A2 (binding, Contract Part A): a row may be `actor_asserted` ONLY with a non-null, VERIFIED
`asserted_by_actor_id` — class and assertor are set TOGETHER, in the same statement, never one
without the other. The inverse (an assertor set on a non-`actor_asserted` class) is equally rejected:
`asserted_by_actor_id` carries meaning ONLY for `actor_asserted` rows, so setting it anywhere else is
not "extra provenance", it is a field that means something different than the writer intended.

"Verified": the assertor resolves, via
`actors.id`, to the actor registered under the name `'owner'`. ledger is a single-owner system
(`interaction_authorization.py`'s own framing) with no elaborate identity mechanism to build here, and
none was asked for — `promote_fact` is invoked by nothing else in practice. The
CLI already gates an owner-only command the identical way: `cli.py`'s `_call_owner_cmd` refuses
`actor_name != "owner"` rather than resolving any richer notion of "the real owner".
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from ledger_memory.actors import get_actor

# The full `authority_class` vocabulary (must match the baseline schema's CHECK constraint verbatim — this
# module does not own the schema, it must never silently diverge from it).
AUTHORITY_CLASSES: frozenset[str] = frozenset({
    "evidence_attested", "actor_asserted", "model_inferred", "system_observed", "legacy_unclassified",
})

# Must remain an exact match for the baseline schema's; the substrate migration is immutable.
MATERIALIZATIONS: frozenset[str] = frozenset({
    "authoritative_claim", "durable_interpretation", "rebuildable_projection",
    "materialization_unclassified",
})

# The one class whose assertor field carries meaning (A2's pairing rule).
ASSERTOR_BEARING_CLASS = "actor_asserted"

# Owner a design decision, item 1.
OWNER_ACTOR_NAME = "owner"


class AuthorityAssignmentError(ValueError):
    """A proposed (authority_class, asserted_by_actor_id) pair violates A2. Every writer that assigns
    authority raises this THROUGH `validate_authority_assignment` rather than inventing its own
    message — one error type, one place callers need to catch it."""


def is_verified_owner_actor(
    database_path: Path | None,
    actor_id: int | None,
    *,
    connection: sqlite3.Connection | None = None,
) -> bool:
    """True only if *actor_id* resolves to the registered 'owner' actor. Fails CLOSED: None, an
    unknown id, or an id that resolves to any OTHER actor (including 'ledger-system' or a model actor)
    all return False — there is no partial credit for "an actor exists"."""
    if actor_id is None:
        return False
    if connection is not None:
        row = connection.execute("SELECT name FROM actors WHERE id = ?", (actor_id,)).fetchone()
        name = row["name"] if row is not None else None
        return name == OWNER_ACTOR_NAME
    if database_path is None:
        # Neither a connection nor a database_path was given — this is a caller bug, not an unverified
        # actor. Failing closed (returning False) would silently masquerade as "not the owner" when the
        # real problem is "nobody told this function how to look anything up".
        raise ValueError("is_verified_owner_actor requires database_path or connection")
    actor = get_actor(database_path, actor_id)
    return actor is not None and actor.name == OWNER_ACTOR_NAME


def validate_authority_assignment(
    authority_class: str,
    asserted_by_actor_id: int | None,
    *,
    database_path: Path | None = None,
    connection: sqlite3.Connection | None = None,
) -> None:
    """A2's invariant, enforced identically by every writer. Raises `AuthorityAssignmentError` on any
    violation; returns silently when the pair is valid.

    Structural checks (unknown class, assertor set on a non-asserted class) never need database
    access. The "is this assertor VERIFIED" check does — a caller assigning `actor_asserted` MUST
    supply `database_path` or `connection`, or this raises (fail closed: a caller that cannot prove
    verification gets refused, not silently waved through as though verification were skipped).
    """
    if authority_class not in AUTHORITY_CLASSES:
        raise AuthorityAssignmentError(f"unknown authority_class: {authority_class!r}")

    if authority_class == ASSERTOR_BEARING_CLASS:
        if asserted_by_actor_id is None:
            raise AuthorityAssignmentError(
                "actor_asserted requires a non-null asserted_by_actor_id (A2)"
            )
        if database_path is None and connection is None:
            raise AuthorityAssignmentError(
                "actor_asserted requires database_path or connection to verify the assertor (A2)"
            )
        if not is_verified_owner_actor(database_path, asserted_by_actor_id, connection=connection):
            raise AuthorityAssignmentError(
                f"actor_asserted requires a VERIFIED assertor; actor {asserted_by_actor_id} does not "
                "resolve to the registered 'owner' actor (A2)"
            )
    elif asserted_by_actor_id is not None:
        raise AuthorityAssignmentError(
            f"asserted_by_actor_id is only meaningful on actor_asserted rows, not on "
            f"{authority_class!r} (A2 inverse)"
        )


def classify_from_source_evidence(
    *,
    author_role: str | None,
    evidence_level: str | None,
    has_resolving_attestation: bool,
) -> str:
    """C1's classification rule, expressed in ONE place so the write-time path (`the ingest path's`) and
    the retroactive backfill (`authority_backfill.compute_assignments`) can never diverge from each
    other — two independent readings of "what does author_role='user' mean" is exactly the defect
    class B2 exists to catch structurally; this closes it at the source instead.

    - `author_role == 'user'` OR `evidence_level == 'user_authored'` -> `actor_asserted` (the owner's
      own recorded words).
    - a RESOLVING attestation (a verified `source_attestations` row whose normalized value the fact's
      stored text actually contains) -> `evidence_attested`.
    - everything else -> `legacy_unclassified` (a design decision, item 2: stays unclassified, stays
      visible; no inference from origin, `created_by_actor_id`, or `content_author` — forbidden,
      an earlier fix / this contract's C1).
    """
    if author_role == "user" or evidence_level == "user_authored":
        return "actor_asserted"
    if has_resolving_attestation:
        return "evidence_attested"
    return "legacy_unclassified"


__all__ = [
    "AUTHORITY_CLASSES",
    "MATERIALIZATIONS",
    "ASSERTOR_BEARING_CLASS",
    "OWNER_ACTOR_NAME",
    "AuthorityAssignmentError",
    "is_verified_owner_actor",
    "validate_authority_assignment",
    "classify_from_source_evidence",
]
