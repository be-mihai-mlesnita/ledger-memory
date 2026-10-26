from __future__ import annotations

"""chunk embedding — embed chunk content into the semantic sidecar.

Chunks are embedded into a DEDICATED `chunk_vectors` table in the vectors sidecar
. It is a separate
table from the record lane's `semantic_vectors` precisely so the record-lane reads
(`semantic_search`, the `semantic-index-integrity` report) never see chunk vectors — true isolation, not
a `record_type` filter the record lane would have to remember everywhere. Embedding is idempotent by
verified source/vector receipt under the current model, not vector presence alone. RAG search reads
`chunk_vectors`.
"""

import json
import hashlib
from dataclasses import dataclass
from pathlib import Path

from ledger_memory.db import connect, connect_readonly
from ledger_memory.embeddings import get_embedding_backend
from ledger_memory.emission import emit_event_only

# The chunk vector lane — a sidecar table distinct from the record lane's `semantic_vectors`, following
# the same the design packed-float format. Isolation is structural (separate table), so the record-lane
# reads are physically incapable of returning chunk vectors.
CHUNK_SIDECAR_DDL = """
CREATE TABLE IF NOT EXISTS chunk_vectors (
    chunk_id        INTEGER NOT NULL,
    embedding_model TEXT    NOT NULL,
    vector          BLOB    NOT NULL,
    embedded_at     TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (chunk_id, embedding_model)
);
"""


@dataclass(frozen=True)
class ChunkEmbedResult:
    embedded_rows: int
    skipped_rows: int
    embedding_model: str
    document_id: int | None


def _embedded_chunk_ids(sidecar_conn, embedding_model: str) -> set[int]:
    rows = sidecar_conn.execute(
        "SELECT chunk_id FROM chunk_vectors WHERE embedding_model = ?",
        (embedding_model,),
    ).fetchall()
    return {int(row["chunk_id"]) for row in rows}


def embed_chunks(
    database_path: Path,
    *,
    document_id: int | None = None,
    actor_id: int,
    force: bool = False,
    sidecar_path: Path | None = None,
) -> ChunkEmbedResult:
    """Embed chunk content into the semantic sidecar as `record_type='chunk'` vectors.

    Verified current chunks are skipped unless *force*. Inference holds no database
    transaction. Each publication rechecks source/model and commits its vector and
    receipt together; a later failure may leave earlier derived rows resumably
    published. No cross-database or whole-batch atomicity is claimed.
    """
    from ledger_memory.semantic_binding import foreign_binding, resolve_write_sidecar, SidecarOwnershipError

    sidecar_path = resolve_write_sidecar(database_path, sidecar_path)

    reason = foreign_binding(database_path, sidecar_path)
    if reason is not None:
        raise SidecarOwnershipError(
            f"refusing to write semantic vectors for {database_path} into {sidecar_path}: "
            f"{reason}. Pass this database's own sidecar explicitly (--sidecar / sidecar_path=) "
            f"or set LEDGER_VECTORS_PATH."
        )

    backend = get_embedding_backend(database_path)
    from ledger_memory.semantic_pending import PendingEmbedding, pending_embeddings, publish_embedding
    with connect_readonly(database_path) as core_conn:
        chunk_rows = core_conn.execute(
            "SELECT id,content FROM chunks "
            + ("WHERE document_id=? " if document_id is not None else "") + "ORDER BY id",
            (document_id,) if document_id is not None else (),
        ).fetchall()
    pending_ids = {item.record_id for item in pending_embeddings(
        database_path, sidecar_path, backend=backend, include_failed=True)
        if item.record_type == "chunk"} if not force else set()
    to_embed = [PendingEmbedding("chunk", int(row["id"]),
                hashlib.sha256(row["content"].encode("utf-8")).hexdigest(), row["content"])
                for row in chunk_rows if force or int(row["id"]) in pending_ids]
    skipped = len(chunk_rows) - len(to_embed)
    if to_embed:
        vectors = backend.embed([item.text for item in to_embed])
        if len(vectors) != len(to_embed):
            raise RuntimeError("embedding backend returned an invalid chunk vector count")
        for item, vector in zip(to_embed, vectors):
            if not publish_embedding(database_path, sidecar_path, item, vector, backend=backend):
                raise RuntimeError("chunk source or model changed during embedding; retry manually")
        # Preserve the explicit command's historical event. Background maintenance
        # calls publish_embedding directly and never emits this event.
        with connect(database_path) as core_conn:
            emit_event_only(core_conn, event_type="chunks.embedded", actor_id=actor_id,
                summary=f"embedded {len(to_embed)} chunk(s) ({backend.model_name})",
                metadata=json.dumps({"embedded": len(to_embed), "skipped": skipped,
                                     "embedding_model": backend.model_name, "document_id": document_id}))

    return ChunkEmbedResult(
        embedded_rows=len(to_embed),
        skipped_rows=skipped,
        embedding_model=backend.model_name,
        document_id=document_id,
    )
