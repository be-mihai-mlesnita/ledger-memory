from __future__ import annotations

"""RAG search over the chunk vector lane.

Top-k cosine retrieval over `chunk_vectors`, mirroring `semantic_search` but with two
the design guarantees:

- **Permission-before-retrieval (rule 6):** each candidate chunk is joined to its parent document's
  `sensitivity_class` and a chunk whose class outranks `max_sensitivity` is dropped BEFORE scoring, so a
  forbidden chunk never occupies a top-k slot and never reaches the caller.
- **Evidence with provenance (rule 6):** every result carries its document id/uri/sensitivity + the
  chunk's projection version. The content is returned as DATA (evidence), never executed — the
  never-instruction rule is honored by the caller wrapping it as evidence.

Facts cite RAG provenance through the existing `fact_evidence` machinery — the EXPORTED parent `document`
(`add_fact(evidence_type="document", evidence_id=document_id)`), with the precise locus (chunk_index +
content_sha256) in the evidence summary, NOT the derived `chunk` id, which is not exported and cannot survive a
repository-only restore — no new code here.

Brute-force cosine in pure Python. Pure read: no event,
no write.
"""
from ledger_memory.paths import default_vectors_path

import math
import hashlib
import os
import struct
import sqlite3
from dataclasses import dataclass
from contextlib import ExitStack, nullcontext
from pathlib import Path

from ledger_memory.db import connect, connect_readonly  # the sidecar read is pure
from ledger_memory.embeddings import get_embedding_backend

# public < personal < sensitive. `credentials` is never stored, so it cannot appear.
_SENSITIVITY_ORDER = {"public": 0, "personal": 1, "sensitive": 2}


@dataclass(frozen=True)
class RagResult:
    chunk_id: int
    document_id: int
    document_uri: str | None
    sensitivity_class: str
    projection_version: str
    score: float
    content: str


def rag_search(
    database_path: Path,
    query: str,
    *,
    limit: int = 5,
    max_sensitivity: str = "sensitive",
    sidecar_path: Path | None = None,
) -> list[RagResult]:
    rows, _reason = rag_search_outcome(database_path, query, limit=limit,
        max_sensitivity=max_sensitivity, sidecar_path=sidecar_path)
    return rows


def rag_search_outcome(
    database_path: Path,
    query: str,
    *,
    limit: int = 5,
    max_sensitivity: str = "sensitive",
    sidecar_path: Path | None = None,
) -> tuple[list[RagResult], str | None]:
    """Return results and an unavailable reason; None also covers an explicit no-op."""
    if max_sensitivity not in _SENSITIVITY_ORDER:
        raise ValueError(f"unknown sensitivity class: {max_sensitivity}")
    max_rank = _SENSITIVITY_ORDER[max_sensitivity]

    if limit <= 0:
        return [], None  # no work requested, not a wrong negative-index slice

    if sidecar_path is None:
        sidecar_path = default_vectors_path(database_path)
    if not sidecar_path.exists():
        return [], "chunk index unavailable"

    # Inference must precede the snapshots actually used for scoring. Otherwise
    # a slow query can serve sources/vectors that changed during inference.
    try:
        backend = get_embedding_backend(database_path)
        query_vector = backend.embed([query])[0]
    except Exception as error:
        from ledger_memory.semantic_pending import _error_category
        return [], f"embedding backend unavailable: {_error_category(error)}"
    from ledger_memory.embeddings import get_embedding_backend as current_backend, SemanticSearchNotConfiguredError
    try:
        selected = current_backend(database_path)
    except SemanticSearchNotConfiguredError:
        return [], "embedding backend disabled during query"
    except ValueError as error:
        from ledger_memory.semantic_pending import _error_category
        return [], f"embedding backend unavailable: {_error_category(error)}"
    if (selected.model_name, selected.dimensions) != (backend.model_name, backend.dimensions):
        return [], "embedding model changed during query"
    from ledger_memory.semantic_binding import binding_violation
    with ExitStack() as stack:
        core_conn = stack.enter_context(connect_readonly(database_path))
        core_conn.execute("BEGIN")
        provenance = _chunk_provenance(database_path, connection=core_conn)
        try:
            sidecar_conn = stack.enter_context(connect_readonly(sidecar_path))
            sidecar_conn.execute("BEGIN")
        except (sqlite3.Error, OSError):
            return [], "chunk index unavailable"
        try:
            blocked = binding_violation(database_path, sidecar_path, active_model=backend.model_name,
                                        core_connection=core_conn, sidecar_connection=sidecar_conn)
            if blocked is not None:
                return [], "chunk generation binding mismatch"
            try:
                tables = {row["name"] for row in sidecar_conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'")}
                if not {"chunk_vectors", "semantic_vector_revisions"} <= tables:
                    return [], "chunk index unavailable"
                vec_rows = sidecar_conn.execute(
                    "SELECT chunk_id, vector FROM chunk_vectors WHERE embedding_model = ? ORDER BY chunk_id",
                    (backend.model_name,),
                ).fetchall()
                receipts = {int(row["record_id"]): (row["content_hash"], row["vector_sha256"])
                    for row in sidecar_conn.execute(
                        "SELECT record_id,content_hash,vector_sha256 FROM semantic_vector_revisions "
                        "WHERE record_type='chunk' AND embedding_model=?", (backend.model_name,))}
            except (sqlite3.Error, OSError):
                return [], "chunk index unavailable"
        finally:
            sidecar_conn.rollback()
            core_conn.rollback()
    if not vec_rows:
        return [], None

    # Permission-before-retrieval: drop forbidden / orphaned chunks BEFORE scoring.
    permitted = []
    for row in vec_rows:
        chunk_id = int(row["chunk_id"])
        meta = provenance.get(chunk_id)
        if meta is None:
            continue  # orphan vector (chunk row gone) — treated as absent, never crashes
        if _SENSITIVITY_ORDER.get(meta["sensitivity_class"], 99) > max_rank:
            continue  # forbidden by permission
        from ledger_memory.semantic_pending import PendingEmbedding, revision_matches
        source = PendingEmbedding("chunk", chunk_id,
            hashlib.sha256(meta["content"].encode("utf-8")).hexdigest(), meta["content"])
        if not revision_matches(source, row["vector"], receipts.get(chunk_id), None,
                                dimensions=backend.dimensions):
            return [], "chunk revision receipt mismatch"
        permitted.append((chunk_id, row["vector"], meta))
    if not permitted:
        return [], None

    norm_query = math.sqrt(sum(a * a for a in query_vector))

    scored = []
    for chunk_id, vector, meta in permitted:
        try:
            floats = struct.unpack(f"{backend.dimensions}f", vector)
        except struct.error:
            continue  # a corrupt/wrong-length chunk vector skips this row, never crashes rag_search
        dot = sum(a * b for a, b in zip(query_vector, floats))
        norm_candidate = math.sqrt(sum(b * b for b in floats))
        if norm_query < 1e-12 or norm_candidate < 1e-12:
            score = 0.0
        else:
            score = dot / (norm_query * norm_candidate)
        scored.append((score, chunk_id, meta))

    scored.sort(key=lambda x: (-x[0], x[1]))  # score desc, chunk_id asc (deterministic tiebreak)

    return [
        RagResult(
            chunk_id=chunk_id,
            document_id=meta["document_id"],
            document_uri=meta["document_uri"],
            sensitivity_class=meta["sensitivity_class"],
            projection_version=meta["projection_version"],
            score=score,
            content=meta["content"],
        )
        for score, chunk_id, meta in scored[:limit]
    ], None


def _chunk_provenance(database_path: Path, *, connection=None) -> dict[int, dict]:
    # `rag_search` is documented "Pure read: no event, no write" — the provenance lookup must use the
    # read-only connector too, so it never flips the journal to WAL or creates sidecars.
    with (nullcontext(connection) if connection is not None else connect_readonly(database_path)) as core_conn:
        rows = core_conn.execute(
            """
            SELECT c.id AS chunk_id, c.document_id, c.content, c.projection_version,
                   d.uri AS document_uri, d.sensitivity_class
            FROM chunks c JOIN documents d ON d.id = c.document_id
            """
        ).fetchall()
    return {
        int(row["chunk_id"]): {
            "document_id": int(row["document_id"]),
            "document_uri": row["document_uri"],
            "sensitivity_class": str(row["sensitivity_class"]),
            "projection_version": str(row["projection_version"]),
            "content": str(row["content"]),
        }
        for row in rows
    }
