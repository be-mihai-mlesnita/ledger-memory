"""
Semantic search over the adapter-owned sidecar.

NOTE — backends: brute-force cosine in pure Python is the DEFAULT; the optional
sqlite-vec ANN lane is an
exact-KNN C-extension index that replaces the brute-force scan without changing the
sidecar schema or the core metadata tables. Brute-force is correct and fast enough at
personal scale (hundreds to low thousands of records); sqlite-vec is opt-in for larger sets.
"""
from __future__ import annotations
from ledger_memory.paths import default_vectors_path

import math
import os
import struct
import sqlite3
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path

from ledger_memory.db import connect, connect_readonly  # sidecar/core reads are pure
from ledger_memory.embeddings import get_embedding_backend
from ledger_memory.search_index import _source_rows, _content_hash
from ledger_memory.semantic_vec import vec_backend_requested, vec_index_row_count, vec_search


@dataclass(frozen=True)
class SemanticSearchResult:
    record_type: str
    record_id: int
    title: str
    score: float


def semantic_lane_blocked(database_path: Path, sidecar_path: Path) -> str | None:
    """A reason the semantic lane must not serve results from *this exact sidecar*, or None.

    TEMPORARY, and a reviewer's ruling rather than my judgement call. The dev sidecar currently
    holds **11,311 rows that are not a projection of this database** (`semantic-index-integrity`), and
    `semantic_search` maps `(record_type, record_id)` back to local records. Two consequences, one much
    worse than the other:

      * a foreign key that resolves to nothing consumed a result slot and came back blank — 107 of 200
        slots measured;
      * a foreign key that COLLIDES with a real local id returns that local record's title carrying a
        score computed from entirely different content. No key-membership filter can see that class.

    So resolve-before-truncate stops the blanks but cannot stop the wrong-but-plausible rows. Until the
    sidecar is rebuilt and bound to the database by a generation receipt, the honest behaviour is to serve
    nothing from this lane.

    **The whole lane, not one arm.** Returning `None` from the sqlite-vec path alone would fall straight
    through to brute-force scoring over the same contaminated sidecar — a fallback into the defect.
    `unified_search` keeps its independent FTS lane, so recall degrades rather than stopping.

    **Ceiling:** this refuses on integrity deltas it can measure. It CANNOT prove the absence of a
    colliding foreign key when the key sets happen to match — only the binding receipt plus a forced
    rebuild closes that. Removing this gate without that receipt reopens the defect.
    """
    # the generation-binding identity check runs FIRST — it is cheap (one receipt row, one
    # COUNT) and it sees the one class the O(N) arms below cannot: a sidecar computed from a DIFFERENT
    # corpus whose numeric keys collide with local records. Transition mode: an unbound sidecar (no
    # receipt yet — every live sidecar until the certifying rebuild) passes through to the
    # integrity arms unchanged.
    try:
        from ledger_memory.embeddings import get_embedding_backend as _backend
        from ledger_memory.semantic_binding import binding_violation
        model = _backend(database_path).model_name
        violation = binding_violation(
            database_path, sidecar_path, active_model=model() if callable(model) else model
        )
    except Exception as exc:  # noqa: BLE001 — an unverifiable binding is not a trustworthy one
        return f"generation binding could not be verified: {type(exc).__name__}: {exc}"
    if violation is not None:
        return violation

    try:
        from ledger_memory.semantic_index import get_semantic_index_integrity_report
        report = get_semantic_index_integrity_report(
            database_path, limit=0, sidecar_path=sidecar_path
        )
    except Exception as exc:  # noqa: BLE001 — an unmeasurable sidecar is not a trustworthy one
        return f"sidecar integrity could not be established: {type(exc).__name__}: {exc}"
    if report.missing_sidecar_rows or report.unexpected_sidecar_rows:
        return (f"sidecar is not a projection of this database: "
                f"{report.unexpected_sidecar_rows} unexpected, {report.missing_sidecar_rows} missing")
    # key-set deltas are not the only measurable staleness. A source record whose
    # CONTENT changed keeps its key, so `missing`/`unexpected` stay zero while the stored vector now
    # encodes text that no longer exists. Reproduced on a scratch store after editing one task title:
    #
    #     status.fresh: False | stale_rows: 1
    #     missing_sidecar: 0   unexpected_sidecar: 0   blocked -> None
    #
    # checked only the two sidecar deltas and admitted that. Core freshness carries the
    # stale/missing/orphaned metadata arm, and a lane that answers from a superseded embedding is the same
    # class of wrong-but-plausible result the gate exists to stop.
    # NARROWED from a reviewer's literal "refuse when status.fresh is false" — measured, and taken back to him.
    # `fresh` folds three arms that do NOT share a failure mode:
    #
    #   * stale_rows   — a stored vector no longer matches its source content. The lane would answer from
    #                    a SUPERSEDED embedding: wrong-but-plausible, the exact class this gate exists for.
    #   * missing_rows — a source record has no vector YET. That is under-coverage, not a wrong answer,
    #                    and it is the normal state moments after any write: creating a record emits an
    #                    event, which is itself indexable. Probed on a scratch store — `embed_chunks`
    #                    alone takes the index to `missing 1` purely by emitting its own event.
    #   * orphaned     — a record DELETED from the authoritative table leaves its metadata + vector
    # behind. an earlier fix called this "can only slow a search" — FALSE, and review
    # reproduced it: the key sets still match, the gate admitted the
    #                    sidecar, and the deleted record came back as `('task', 1, '')` — a phantom
    # consuming a ranked slot. Until an earlier fix resolves-and-filters before
    #                    truncation, an orphan IS a manufactured result, so it blocks.
    #
    # Blocking on `fresh` (which folds in `missing_rows`) would disable semantic recall after EVERY
    # record creation until the next rebuild, a 30-minute tick. That trades a real defect for a lane that
    # is almost never available — and an always-closed gate teaches its reader to route around it. So
    # `missing_rows` stays non-blocking here: a stated under-coverage exception for this explicit
    # single-lane diagnostic only. The default unified lane keeps full `semantic_status.fresh` gating.
    if report.status.stale_rows:
        return f"derived index is stale: {report.status.stale_rows} superseded vector(s)"
    if report.status.orphaned_rows:
        return (f"derived index carries {report.status.orphaned_rows} orphaned vector(s) "
                f"for deleted records — phantom results until a rebuild prunes them")
    return None


def semantic_search_outcome(
    database_path: Path,
    query: str,
    *,
    limit: int = 10,
    sidecar_path: Path | None = None,
) -> tuple[list[SemanticSearchResult], str | None]:
    """The shared readiness/outcome primitive: `(results, blocked_reason)`.

    `unified_search` needs the OUTCOME, not just the results: a blocked lane returning `[]` is
    indistinguishable from a fresh lane with no hits, which is exactly how `semantic_fresh=yes` got
    printed over a lane that never ran. Running the gate here — once — also
    keeps the O(N) integrity report to a single pass per unified call instead of two.
    """
    if limit <= 0:
        # a negative limit makes candidates[:limit] silently DROP results (0 ⇒ none).
        return [], None
    if sidecar_path is None:
        sidecar_path = default_vectors_path(database_path)

    # the gate runs before either lane, and before the embedding call — refusing a
    # contaminated sidecar must not depend on a model being reachable.
    blocked = semantic_lane_blocked(database_path, sidecar_path)
    if blocked is not None:
        return [], blocked
    return _semantic_query(database_path, query, limit=limit, sidecar_path=sidecar_path)


def semantic_search(
    database_path: Path,
    query: str,
    *,
    limit: int = 10,
    sidecar_path: Path | None = None,
) -> list[SemanticSearchResult]:
    results, _reason = semantic_search_outcome(
        database_path, query, limit=limit, sidecar_path=sidecar_path
    )
    return results


def _semantic_query(
    database_path: Path,
    query: str,
    *,
    limit: int,
    sidecar_path: Path,
) -> tuple[list[SemanticSearchResult], str | None]:
    """resolve authoritative membership BEFORE truncating, in both lanes.

    The measured defect: score → sort → truncate → THEN look up titles, so a sidecar key with no
    authoritative row consumed a ranked slot and came back blank (107/200 live slots, counted by
    MEMBERSHIP in the re-measurement — blank-title was revision 1's proxy and is not the criterion).

    Ceiling, verbatim from the spec: key membership CANNOT detect a colliding foreign key — a foreign
    vector keyed ("fact", 1) passes when the local store also has a fact 1, and returns a valid-looking
    local title with a score computed from foreign content. This slice stops blank rows; what protects
    against collisions is the gate + the binding receipt.

    Returns `(results, unavailable_reason)`. `unavailable_reason` is set only when the
    embedding backend itself could not run — a genuinely empty result set (the lane ran, nothing
    matched) stays `(results=[], None)`, so an empty answer is never confused with a dead lane.
    """
    if not sidecar_path.exists():
        return [], "record index unavailable"

    backend = get_embedding_backend(database_path)
    # Embed BEFORE opening/pinning either snapshot: a slow model call must not hold an old WAL view.
    try:
        query_vector = backend.embed([query])[0]
    except Exception as exc:  # noqa: BLE001 — any embed failure means the lane did not run at all
        # established the graceful degradation (this lane contributes nothing; unified
        # search falls back to FTS + LIKE). it must also SAY why, so a caller can tell
        # "ran and found nothing" apart from "never ran" instead of printing `semantic_ready=yes` for
        # both — the same dishonesty an earlier fix fixed one layer up, surviving one layer down.
        return [], f"embedding backend unavailable: {type(exc).__name__}"

    from ledger_memory.embeddings import get_embedding_backend as current_backend, SemanticSearchNotConfiguredError
    from ledger_memory.semantic_binding import binding_violation
    from ledger_memory.semantic_pending import PendingEmbedding, revision_matches, _error_category
    try:
        selected = current_backend(database_path)
    except SemanticSearchNotConfiguredError:
        return [], "embedding backend disabled during query"
    except ValueError as error:
        return [], f"embedding backend unavailable: {_error_category(error)}"
    if (selected.model_name, selected.dimensions) != (backend.model_name, backend.dimensions):
        return [], "embedding model changed during query"

    with ExitStack() as stack:
        core_conn = stack.enter_context(connect_readonly(database_path))
        core_conn.execute("BEGIN")
        source = _source_rows(core_conn)
        metadata = {(row["record_type"], int(row["record_id"])):
                    (row["content_hash"], row["embedding_dimensions"])
                    for row in core_conn.execute(
                        "SELECT record_type,record_id,content_hash,embedding_dimensions "
                        "FROM semantic_index_entries WHERE embedding_model=?", (backend.model_name,))}
        title_map = {(kind, identifier): title for kind, identifier, title, _body in source}
        source_items = {(kind, identifier): PendingEmbedding(kind, identifier,
                        _content_hash(kind, identifier, title, body), "")
                        for kind, identifier, title, body in source}
        try:
            sidecar_conn = stack.enter_context(connect_readonly(sidecar_path))
            sidecar_conn.execute("BEGIN")
        except (sqlite3.Error, OSError):
            return [], "record index unavailable"
        try:
            blocked = binding_violation(database_path, sidecar_path, active_model=backend.model_name,
                                        core_connection=core_conn, sidecar_connection=sidecar_conn)
            if blocked is not None:
                return [], "record generation binding mismatch"
            try:
                tables = {row["name"] for row in sidecar_conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if not {"semantic_vectors", "semantic_vector_revisions"} <= tables:
                    return [], "record index unavailable"
                rows = sidecar_conn.execute(
                    "SELECT record_type,record_id,embedding_model,vector FROM semantic_vectors WHERE embedding_model=?",
                    (backend.model_name,)).fetchall()
                receipt_rows = sidecar_conn.execute(
                    "SELECT record_type,record_id,content_hash,vector_sha256 FROM semantic_vector_revisions "
                    "WHERE embedding_model=?", (backend.model_name,)).fetchall()
            except (sqlite3.Error, OSError):
                return [], "record index unavailable"
            if any(type(row["record_id"]) is not int for row in [*rows, *receipt_rows]):
                return [], "record index unavailable"
            receipts = {(str(row["record_type"]), int(row["record_id"])):
                        (row["content_hash"], row["vector_sha256"]) for row in receipt_rows}
            for row in rows:
                key = (str(row["record_type"]), int(row["record_id"]))
                item = source_items.get(key)
                if item is not None and not revision_matches(item, row["vector"], receipts.get(key),
                                                             metadata.get(key), dimensions=backend.dimensions):
                    return [], "record revision receipt mismatch"
            if not rows:
                return [], None

            top_valid: list[tuple[str, int, float]] | None = None
            if vec_backend_requested():
                top_valid = _vec_lane_resolve(
                    sidecar_conn, query_vector, backend=backend, limit=limit, title_map=title_map,
                )

            if top_valid is None:
                # Brute force: filter by membership → score/sort → truncate. Exact; no over-fetch.
                # the query norm is invariant across candidates — computed once.
                norm_query = math.sqrt(sum(a * a for a in query_vector))
                query_is_degenerate = norm_query < 1e-12
                candidates = []
                for row in rows:
                    record_type = str(row["record_type"])
                    record_id = int(row["record_id"])
                    if str(row["embedding_model"]) != backend.model_name:
                        continue
                    if (record_type, record_id) not in title_map:
                        continue  # a foreign key must not reach scoring, let alone a slot
                    try:
                        vector_floats = list(struct.unpack(f"{backend.dimensions}f", row["vector"]))
                    except struct.error:
                        continue  # corrupt blob skips the row, never crashes search
                    dot_product = sum(a * b for a, b in zip(query_vector, vector_floats))
                    norm_candidate = math.sqrt(sum(a * a for a in vector_floats))
                    if query_is_degenerate or norm_candidate < 1e-12:
                        score = 0.0
                    else:
                        score = dot_product / (norm_query * norm_candidate)
                    candidates.append((record_type, record_id, score))
                candidates.sort(key=lambda x: x[2], reverse=True)
                top_valid = candidates[:limit]
        finally:
            sidecar_conn.rollback()
            core_conn.rollback()

    return [
        SemanticSearchResult(rtype, rid, title_map[(rtype, rid)], score)
        for rtype, rid, score in top_valid
    ], None


def _vec_lane_resolve(
    sidecar_conn,
    query_vector,
    *,
    backend,
    limit: int,
    title_map: dict[tuple[str, int], str],
) -> list[tuple[str, int, float]] | None:
    """The sqlite-vec lane with membership resolution and bounded K expansion. `None` = fall back.

    Spec §8.5/§8.6: the expansion is bounded by the
    pinned TABLE COUNT, never by returned-row count (`vec_search` can drop a vec row whose rowid no
    longer resolves, so "returned fewer than K" is not proof of exhaustion). A missing/zero/inconsistent
    vec index while active-model vectors exist returns `None` — the already-vetted brute-force lane IS
    the fallback. `[]` is reserved for a successful, exhausted query with zero valid rows.
    """
    total = vec_index_row_count(
        sidecar_conn, model_name=backend.model_name, dimensions=backend.dimensions
    )
    if total is None:
        return None
    if total == 0:
        vectors_exist = sidecar_conn.execute(
            "SELECT 1 FROM semantic_vectors WHERE embedding_model = ? LIMIT 1", (backend.model_name,)
        ).fetchone()
        if vectors_exist is not None:
            return None  # an empty index over a non-empty source is inconsistent, not a trusted empty
        return []

    k = min(max(limit, 1), total)
    while True:
        got = vec_search(
            sidecar_conn, query_vector,
            model_name=backend.model_name, dimensions=backend.dimensions, limit=k,
        )
        if got is None:
            return None
        valid = [(rtype, rid, score) for rtype, rid, score in got if (rtype, rid) in title_map]
        if len(valid) >= limit or k == total:
            return valid[:limit]
        # k+1 progress guard: a tiny K or future arithmetic cannot stall the loop.
        k = min(total, max(k + 1, 2 * k))
