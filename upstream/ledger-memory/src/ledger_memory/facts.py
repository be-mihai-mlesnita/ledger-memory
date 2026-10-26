from __future__ import annotations

from contextlib import nullcontext
from dataclasses import dataclass
import json
from pathlib import Path

from ledger_memory.db import connect
from ledger_memory.emission import emit_audit_and_event
from ledger_memory.fact_authority import (
    AuthorityAssignmentError,
    is_verified_owner_actor,
    validate_authority_assignment,
)

# the ONE current-fact predicate, used by every declared current/default retrieval
# consumer (search.py, search_index.py, projection.py, source_fidelity.py). A dedup member keeps
# status='active' and gains only the superseded_by_fact_id pointer, so the predicate excludes it
# exactly like an ordinary superseded fact (closing the pre-existing 27-fact pointer-superseded leak).
# Deliberately NOT used by list_facts/get_fact/export paths/the store-ingest curation scan (which
# must see ALL facts) — those are named exemptions, not oversights (§10 test_allowlist_is_exhaustive).
CURRENT_FACT_SQL = "status = 'active' AND superseded_by_fact_id IS NULL"


@dataclass(frozen=True)
class Fact:
    id: int
    statement: str
    fact_type: str
    status: str
    confidence: float | None
    known_at: str
    created_at: str
    superseded_by: int | None = None
    valid_to: str | None = None
    # Additive nullable fields — no positional callers exist
    # outside _row_to_fact, so these are safe append-only additions.
    valid_from: str | None = None
    created_by_actor_id: int | None = None
    origin: str | None = None
    source_db: str | None = None
    source_record_id: int | None = None
    # the baseline schema's authority columns, read defensively via `row.keys` exactly like
    # the fields above — this dataclass must keep working against a facts table that predates 043.
    authority_class: str | None = None
    asserted_by_actor_id: int | None = None


@dataclass(frozen=True)
class FactEvidence:
    id: int
    fact_id: int
    evidence_type: str
    evidence_id: int
    relationship: str
    summary: str | None


class FactAlreadySupersededError(Exception):
    def __init__(self, fact_id: int, superseded_by: int) -> None:
        super().__init__(f"fact already superseded: {fact_id} by {superseded_by}")
        self.fact_id = fact_id
        self.superseded_by = superseded_by


class FactNotFoundError(Exception):
    pass


class FactNotPromotableError(Exception):
    pass


def _table_columns(connection, table: str) -> frozenset[str]:
    """Which columns actually exist on *table* RIGHT NOW. `_row_to_fact` already reads additive
    columns defensively via `row.keys` so SELECTs keep working against a facts table that predates
    the baseline schema's/043; a WRITE needs the same defence BEFORE building its column list, since a
    facts table frozen mid-migration (`_apply_migrations_through`-style tests, or a live db paused
    between two `init-db` runs) does not have `authority_class`/`asserted_by_actor_id` yet."""
    return frozenset(row[1] for row in connection.execute(f"PRAGMA table_info({table})"))


def _insert_fact_pre_authority_columns(connection, *, statement, fact_type, confidence, status):
    """NAMED EXEMPTION (`fact_writer_inventory.NAMED_EXEMPTIONS`): the fallback shape for
    a facts table that predates the baseline schema's, where `authority_class` does not exist to assign. Once
    043 applies, the same row reads `legacy_unclassified` via the column's own DEFAULT — migration
    043's header states "no data mutation, no backfill, no UPDATE", so this fallback's silence is that
    SAME contract, not a gap `add_fact` introduces on its own."""
    return connection.execute(
        """
        INSERT INTO facts (statement, fact_type, confidence, status)
        VALUES (?, ?, ?, ?)
        """,
        (statement, fact_type, confidence, status),
    )


def add_fact(
    database_path: Path,
    *,
    statement: str,
    fact_type: str,
    confidence: float | None = None,
    evidence_type: str | None = None,
    evidence_id: int | None = None,
    relationship: str = "supports",
    actor_id: int | None = None,
    reason: str | None = None,
    status: str = "active",
    connection=None,
    # an earlier fix / A1: deliberately NOT derived from `actor_id` — every caller of `add_fact` writes
    # through the SAME door (the CLI, `error_patterns.inject_facts`, `operator_file_bridge`), and
    # `actor_id` is whichever executable wrote the row, not a verified claim about who AUTHORED the
    # content. Defaults to the pre-existing behaviour (legacy_unclassified via the column default) so
    # every caller that does not explicitly request a class is unaffected by this slice.
    authority_class: str = "legacy_unclassified",
    asserted_by_actor_id: int | None = None,
) -> Fact:
    # when a caller passes an open `connection`, write on it WITHOUT opening/committing our own, so the
    # caller's transaction owns atomicity (e.g. the operator-ingest single transaction). `connection=None` is unchanged.
    with (nullcontext(connection) if connection is not None else connect(database_path)) as connection:
        if "authority_class" in _table_columns(connection, "facts"):
            # A2, enforced in the SAME statement the row is written in — never after the fact.
            validate_authority_assignment(
                authority_class, asserted_by_actor_id, database_path=database_path, connection=connection,
            )
            cursor = connection.execute(
                """
                INSERT INTO facts (statement, fact_type, confidence, status, authority_class, asserted_by_actor_id)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (statement, fact_type, confidence, status, authority_class, asserted_by_actor_id),
            )
        else:
            if authority_class != "legacy_unclassified" or asserted_by_actor_id is not None:
                # A caller explicitly asked for a real classification on a table that structurally
                # cannot represent one yet — refuse loudly rather than silently drop the request.
                raise AuthorityAssignmentError(
                    "facts table predates migration 043 (no authority_class column) — cannot honour "
                    f"authority_class={authority_class!r} / asserted_by_actor_id={asserted_by_actor_id!r}"
                )
            cursor = _insert_fact_pre_authority_columns(
                connection, statement=statement, fact_type=fact_type, confidence=confidence, status=status,
            )
        fact_id = int(cursor.lastrowid)
        if evidence_type is not None and evidence_id is not None:
            connection.execute(
                """
                INSERT INTO fact_evidence (
                    fact_id,
                    evidence_type,
                    evidence_id,
                    relationship
                )
                VALUES (?, ?, ?, ?)
                """,
                (fact_id, evidence_type, evidence_id, relationship),
            )
        metadata = json.dumps({"reason": reason}) if reason is not None else None
        emit_audit_and_event(
            connection,
            action_type="fact.created",
            target_type="fact",
            target_id=fact_id,
            actor_id=actor_id,
            reason=reason,
            event_type="fact.created",
            summary=f"Fact created: {fact_id}",
            metadata=metadata,
        )
        row = _fetch_fact_row(connection, fact_id)

    if row is None:
        raise RuntimeError(f"created fact could not be loaded: {fact_id}")
    return _row_to_fact(row)


def supersede_fact(
    database_path: Path,
    *,
    fact_id: int,
    statement: str,
    fact_type: str | None = None,
    confidence: float | None = None,
    actor_id: int | None = None,
    reason: str | None = None,
    authority_class: str | None = None,
    asserted_by_actor_id: int | None = None,
    evidence_type: str | None = None,
    evidence_id: int | None = None,
    relationship: str = "supports",
) -> Fact:
    """Create a replacement fact, close the old fact's validity, link the chain.

    Returns the newly created fact. Raises FactAlreadySupersededError if the
    target fact is already superseded. Raises ValueError if the target fact
    does not exist. The entire operation is transactional.

    the write lock is taken BEFORE the predecessor is read, and the predecessor is superseded only while
    it is still current — two sessions revising the same fact can no longer both succeed (one gets
    FactAlreadySupersededError and its insert rolls back). `authority_class=None` inherits the predecessor's
    authority pair (the default, unchanged); an explicit pair is validated and used as given, never mixed with the
    inherited assertor. `evidence_type`/`evidence_id` record ONE evidence row for the NEW statement; the
    predecessor's evidence is never copied onto changed content.
    """
    with connect(database_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        existing = _fetch_fact_row(connection, fact_id)
        if existing is None:
            raise ValueError(f"fact not found: {fact_id}")
        if existing["superseded_by_fact_id"] is not None:
            raise FactAlreadySupersededError(
                fact_id, int(existing["superseded_by_fact_id"])
            )
        resolved_type = fact_type if fact_type is not None else str(existing["fact_type"])

        # B3: a supersession CARRIES the parent's authority forward — it never silently downgrades a
        # verified claim to legacy_unclassified just because it is new-born. Defensive `.keys()` check
        # for the same reason `_row_to_fact` reads the migration-032 columns that way: this must keep
        # working against a facts table that predates the baseline schema's.
        existing_keys = existing.keys() if hasattr(existing, "keys") else []
        parent_authority_class = (
            existing["authority_class"] if "authority_class" in existing_keys else "legacy_unclassified"
        )
        parent_asserted_by_actor_id = (
            existing["asserted_by_actor_id"] if "asserted_by_actor_id" in existing_keys else None
        )
        # The parent already satisfied A2 (nothing else can have written it otherwise), so copying the
        # SAME pair forward is always structurally valid EXCEPT for a row that predates this slice's
        # enforcement — refuse rather than propagate a corrupt pairing to a second row (B3's "or
        # refuses").
        authority_inherited = authority_class is None
        if authority_inherited:
            authority_class, asserted_by_actor_id = parent_authority_class, parent_asserted_by_actor_id
        if "authority_class" in _table_columns(connection, "facts"):
            validate_authority_assignment(
                authority_class, asserted_by_actor_id,
                database_path=database_path, connection=connection,
            )
            cursor = connection.execute(
                """
                INSERT INTO facts (statement, fact_type, confidence, authority_class, asserted_by_actor_id)
                VALUES (?, ?, ?, ?, ?)
                """,
                (statement, resolved_type, confidence, authority_class, asserted_by_actor_id),
            )
        else:
            if not authority_inherited and (authority_class != "legacy_unclassified" or asserted_by_actor_id is not None):
                raise AuthorityAssignmentError(
                    "facts table predates migration 043 (no authority_class column) — cannot honour "
                    f"authority_class={authority_class!r} / asserted_by_actor_id={asserted_by_actor_id!r}"
                )
            # The table predates the baseline schema's — `existing_keys` already forced
            # `parent_authority_class` to "legacy_unclassified" above, so there is nothing to carry
            # forward yet; this is the SAME pre-043 shape `add_fact` falls back to.
            cursor = _insert_fact_pre_authority_columns(
                connection, statement=statement, fact_type=resolved_type, confidence=confidence,
                status="active",
            )
        new_fact_id = int(cursor.lastrowid)
        updated = connection.execute(
            """
            UPDATE facts
            SET superseded_by_fact_id = ?, valid_to = CURRENT_TIMESTAMP
            WHERE id = ? AND superseded_by_fact_id IS NULL
            """,
            (new_fact_id, fact_id),
        )
        if updated.rowcount != 1:
            # Another writer superseded it first; raising inside the transaction rolls back our insert.
            current = _fetch_fact_row(connection, fact_id)
            winner = current["superseded_by_fact_id"] if current is not None else None
            raise FactAlreadySupersededError(fact_id, int(winner) if winner is not None else -1)
        if evidence_type is not None and evidence_id is not None:
            connection.execute(
                """
                INSERT INTO fact_evidence (
                    fact_id,
                    evidence_type,
                    evidence_id,
                    relationship
                )
                VALUES (?, ?, ?, ?)
                """,
                (new_fact_id, evidence_type, evidence_id, relationship),
            )
        metadata = json.dumps(
            {
                "old_fact_id": fact_id,
                "new_fact_id": new_fact_id,
                "reason": reason,
                "authority_inherited": authority_inherited,
            }
        )
        emit_audit_and_event(
            connection,
            action_type="fact.superseded",
            target_type="fact",
            target_id=fact_id,
            actor_id=actor_id,
            reason=reason,
            event_type="fact.superseded",
            summary=f"Fact superseded: {fact_id} by {new_fact_id}",
            metadata=metadata,
        )
        row = _fetch_fact_row(connection, new_fact_id)

    if row is None:
        raise RuntimeError(f"created replacement fact could not be loaded: {new_fact_id}")
    return _row_to_fact(row)


def dispute_fact(
    database_path: Path,
    *,
    fact_id: int,
    reason: str,
    actor_id: int | None = None,
    disputed_by_fact_id: int | None = None,
) -> Fact:
    """Record a dispute event for a fact. Returns the disputed fact.

    The dispute is stored as a fact.disputed event with mandatory reason in
    metadata. No schema change; show-fact derives disputed state from events.
    Raises ValueError if the fact does not exist.
    """
    with connect(database_path) as connection:
        existing = _fetch_fact_row(connection, fact_id)
        if existing is None:
            raise ValueError(f"fact not found: {fact_id}")
        metadata = json.dumps({"fact_id": fact_id, "reason": reason, "disputed_by_fact_id": disputed_by_fact_id} if disputed_by_fact_id is not None else {"fact_id": fact_id, "reason": reason})
        emit_audit_and_event(
            connection,
            action_type="fact.disputed",
            target_type="fact",
            target_id=fact_id,
            actor_id=actor_id,
            reason=reason,
            event_type="fact.disputed",
            summary=f"Fact disputed: {fact_id}",
            metadata=metadata,
        )
        row = _fetch_fact_row(connection, fact_id)

    if row is None:
        raise RuntimeError(f"disputed fact could not be loaded: {fact_id}")
    return _row_to_fact(row)


def promote_fact(
    database_path: Path,
    *,
    fact_id: int,
    actor_id: int,
    reason: str | None = None,
    evidence_artifact_ids: list[int] | None = None,
    # an earlier fix / A3: `promote_fact` performs NO
    # ownership check on `actor_id` today — that is the exact gap A3 names ("accepts any registered
    # actor_id"). An authority upgrade is therefore a SEPARATE, opt-in pair of kwargs, applied ONLY
    # when `actor_id` itself resolves to the verified owner actor. Nothing else in this codebase calls
    # `promote_fact` with these set — this is deliberately the whole mechanism,
    # not a first slice of a bigger one.
    authority_class: str | None = None,
    asserted_by_actor_id: int | None = None,
) -> Fact:
    with connect(database_path) as connection:
        row = _fetch_fact_row(connection, fact_id)
        if row is None:
            raise FactNotFoundError(f"fact not found: {fact_id}")
        if row["status"] != "proposed":
            raise FactNotPromotableError(f"fact status is not 'proposed': {fact_id}")

        # A3: promotion upgrades authority ONLY through this owner-authenticated chokepoint —
        # otherwise promotion moves status alone. A caller that is NOT the verified owner but still
        # asked for an upgrade does not get an error (it did not ask to fail; it asked to promote, and
        # status-only promotion is the pre-existing, unnarrowed contract) — the upgrade is simply not
        # applied, and that fact is recorded in this event's metadata below so it is auditable rather
        # than silently swallowed.
        authority_upgrade_requested = authority_class is not None
        authority_upgrade_applied = False
        if authority_upgrade_requested:
            if "authority_class" not in _table_columns(connection, "facts"):
                # The table predates the baseline schema's — refuse loudly rather than let a raw
                # sqlite3.OperationalError leak the schema gap to the caller.
                raise AuthorityAssignmentError(
                    "facts table predates migration 043 (no authority_class column) — cannot honour "
                    f"a requested authority upgrade ({authority_class!r})"
                )
            if is_verified_owner_actor(database_path, actor_id, connection=connection):
                # A structurally invalid request (e.g. actor_asserted with no assertor) from the OWNER
                # is a real error, not a quiet no-op — the owner asked for something specific and it
                # must either happen or fail loudly.
                validate_authority_assignment(
                    authority_class, asserted_by_actor_id,
                    database_path=database_path, connection=connection,
                )
                connection.execute(
                    "UPDATE facts SET authority_class = ?, asserted_by_actor_id = ? WHERE id = ?",
                    (authority_class, asserted_by_actor_id, fact_id),
                )
                authority_upgrade_applied = True

        connection.execute(
            """
            UPDATE facts
            SET status = 'active'
            WHERE id = ?
            """,
            (fact_id,),
        )
        if evidence_artifact_ids is not None:
            for artifact_id in evidence_artifact_ids:
                connection.execute(
                    """
                    INSERT INTO fact_evidence (
                        fact_id,
                        evidence_type,
                        evidence_id,
                        relationship
                    )
                    VALUES (?, ?, ?, ?)
                    """,
                    (fact_id, "artifact", artifact_id, "supports"),
                )
        metadata_dict: dict = {"reason": reason}
        if authority_upgrade_requested:
            # A3 auditability: whether the upgrade actually landed is NOT recoverable from `reason`
            # alone — record it explicitly so "asked for an upgrade and got one" is distinguishable
            # from "asked for an upgrade and it was silently declined" after the fact.
            metadata_dict["requested_authority_class"] = authority_class
            metadata_dict["authority_upgrade_applied"] = authority_upgrade_applied
        metadata = json.dumps(metadata_dict) if metadata_dict.get("reason") is not None or authority_upgrade_requested else None
        emit_audit_and_event(
            connection,
            action_type="fact.promoted",
            target_type="fact",
            target_id=fact_id,
            actor_id=actor_id,
            reason=reason,
            event_type="fact.promoted",
            summary=f"Fact promoted: {fact_id}",
            metadata=metadata,
        )
        row = _fetch_fact_row(connection, fact_id)

    if row is None:
        raise RuntimeError(f"promoted fact could not be loaded: {fact_id}")
    return _row_to_fact(row)


def reject_fact(
    database_path: Path,
    *,
    fact_id: int,
    actor_id: int,
    reason: str,
) -> Fact:
    with connect(database_path) as connection:
        row = _fetch_fact_row(connection, fact_id)
        if row is None:
            raise FactNotFoundError(f"fact not found: {fact_id}")
        if row["status"] != "proposed":
            raise FactNotPromotableError(f"fact status is not 'proposed': {fact_id}")
        connection.execute(
            """
            UPDATE facts
            SET status = 'rejected'
            WHERE id = ?
            """,
            (fact_id,),
        )
        metadata = json.dumps({"reason": reason})
        emit_audit_and_event(
            connection,
            action_type="fact.rejected",
            target_type="fact",
            target_id=fact_id,
            actor_id=actor_id,
            reason=reason,
            event_type="fact.rejected",
            summary=f"Fact rejected: {fact_id}",
            metadata=metadata,
        )
        row = _fetch_fact_row(connection, fact_id)

    if row is None:
        raise RuntimeError(f"rejected fact could not be loaded: {fact_id}")
    return _row_to_fact(row)


def list_facts(database_path: Path, *, active_only: bool = False, status: str | None = None) -> list[Fact]:
    # status filter takes precedence over active_only; always parameterized
    # (never string-interpolated) to avoid SQL injection from caller input.
    where = ""
    params: tuple = ()
    if status is not None:
        where = "WHERE status = ?"
        params = (status,)
    elif active_only:
        where = "WHERE superseded_by_fact_id IS NULL"
    with connect(database_path) as connection:
        rows = connection.execute(
            f"""
            SELECT id, statement, fact_type, status, confidence, known_at, created_at,
                   superseded_by_fact_id, valid_to
            FROM facts
            {where}
            ORDER BY id
            """,
            params,
        ).fetchall()
    return [_row_to_fact(row) for row in rows]


def get_fact_supersession_chain(
    database_path: Path, fact_id: int
) -> tuple[list[int], list[int]]:
    """Return (predecessors, successors) id lists for a fact, excluding itself.

    predecessors: facts that were superseded leading up to fact_id (ancestor chain).
    successors: facts that succeeded fact_id (descendent chain).
    """
    with connect(database_path) as connection:
        predecessors: list[int] = []
        current_id = fact_id
        while True:
            row = connection.execute(
                """
                SELECT id FROM facts WHERE superseded_by_fact_id = ?
                """,
                (current_id,),
            ).fetchone()
            if row is None:
                break
            predecessors.append(int(row["id"]))
            current_id = int(row["id"])

        successors: list[int] = []
        current_id = fact_id
        while True:
            row = connection.execute(
                """
                SELECT superseded_by_fact_id FROM facts WHERE id = ?
                """,
                (current_id,),
            ).fetchone()
            if row is None or row["superseded_by_fact_id"] is None:
                break
            next_id = int(row["superseded_by_fact_id"])
            successors.append(next_id)
            current_id = next_id

    return predecessors, successors


def get_fact_dispute_events(database_path: Path, fact_id: int) -> list[dict]:
    """Return list of fact.disputed event metadata dicts for a fact."""
    with connect(database_path) as connection:
        rows = connection.execute(
            """
            SELECT metadata FROM events
            WHERE event_type = 'fact.disputed'
              AND summary = ?
            ORDER BY id
            """,
            (f"Fact disputed: {fact_id}",),
        ).fetchall()
    result = []
    for row in rows:
        if row["metadata"]:
            try:
                data = json.loads(row["metadata"])
                result.append(data)
            except (json.JSONDecodeError, KeyError):
                pass
    return result


def get_fact(database_path: Path, fact_id: int) -> Fact | None:
    with connect(database_path) as connection:
        row = _fetch_fact_row(connection, fact_id)
    if row is None:
        return None
    return _row_to_fact(row)


def _supersedes_id(database_path: Path, fact_id: int) -> int | None:
    """Return the id of the fact that fact_id superseded, or None."""
    with connect(database_path) as connection:
        row = connection.execute(
            "SELECT id FROM facts WHERE superseded_by_fact_id = ?",
            (fact_id,),
        ).fetchone()
    return None if row is None else int(row["id"])


def list_fact_evidence(database_path: Path, fact_id: int) -> list[FactEvidence]:
    with connect(database_path) as connection:
        rows = connection.execute(
            """
            SELECT id, fact_id, evidence_type, evidence_id, relationship, summary
            FROM fact_evidence
            WHERE fact_id = ?
            ORDER BY id
            """,
            (fact_id,),
        ).fetchall()
    return [_row_to_evidence(row) for row in rows]


def _fetch_fact_row(connection, fact_id: int):
    # `SELECT *` (not an explicit column list) so this keeps working against a facts table that
    # predates the baseline schema's provenance columns (e.g. a mid-upgrade db at the baseline schema's) —
    # `_row_to_fact` reads the additive fields defensively via `row.keys()` and defaults to None
    # when a column is not yet present.
    return connection.execute(
        "SELECT * FROM facts WHERE id = ?",
        (fact_id,),
    ).fetchone()


def _row_to_fact(row) -> Fact:
    confidence = row["confidence"]
    superseded_by = row["superseded_by_fact_id"]
    keys = row.keys() if hasattr(row, "keys") else []
    return Fact(
        id=int(row["id"]),
        statement=str(row["statement"]),
        fact_type=str(row["fact_type"]),
        status=str(row["status"]),
        confidence=None if confidence is None else float(confidence),
        known_at=str(row["known_at"]),
        created_at=str(row["created_at"]),
        superseded_by=None if superseded_by is None else int(superseded_by),
        valid_to=row["valid_to"],
        valid_from=row["valid_from"] if "valid_from" in keys else None,
        created_by_actor_id=(
            int(row["created_by_actor_id"])
            if "created_by_actor_id" in keys and row["created_by_actor_id"] is not None
            else None
        ),
        origin=row["origin"] if "origin" in keys else None,
        source_db=row["source_db"] if "source_db" in keys else None,
        source_record_id=(
            int(row["source_record_id"])
            if "source_record_id" in keys and row["source_record_id"] is not None
            else None
        ),
        authority_class=row["authority_class"] if "authority_class" in keys else None,
        asserted_by_actor_id=(
            int(row["asserted_by_actor_id"])
            if "asserted_by_actor_id" in keys and row["asserted_by_actor_id"] is not None
            else None
        ),
    )


def get_superseded_attestations(database_path: Path, fact_id: int) -> list[Fact]:
    """plural view (provenance-complete, normative): every DIRECT attestation that
    points at *fact_id* — genuine N>2 fan-in, ordered by id. The pre-existing single-predecessor
    helper (`_supersedes_id`) is NOT reused or modified; `show-fact`'s existing `supersedes: <id>`
    line keeps working byte-identically."""
    with connect(database_path) as connection:
        rows = connection.execute(
            "SELECT * FROM facts WHERE superseded_by_fact_id = ? ORDER BY id",
            (fact_id,),
        ).fetchall()
    return [_row_to_fact(row) for row in rows]


def _row_to_evidence(row) -> FactEvidence:
    return FactEvidence(
        id=int(row["id"]),
        fact_id=int(row["fact_id"]),
        evidence_type=str(row["evidence_type"]),
        evidence_id=int(row["evidence_id"]),
        relationship=str(row["relationship"]),
        summary=row["summary"],
    )
