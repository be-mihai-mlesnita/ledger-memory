from __future__ import annotations

"""chunk persistence — write a document's derived chunk set.

``chunk_document`` runs a :class:`~ledger_memory.chunkers.Chunker` over a document and stores the result in the
derived ``chunks`` table, recording the chunker's ``recipe`` (sorted-key JSON) and ``projection_version``
so the set is rebuildable and re-chunking is detectable. It is idempotent by ``(document_id,
projection_version)``: a set that already exists is returned unchanged, with no second write and no second
``document.chunked`` event. Re-chunking under a different chunker writes a coexisting second set. Chunks
are derived data, so only an event is emitted (``emit_event_only`` — no audit entry).
"""

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from ledger_memory.chunkers import Chunker
from ledger_memory.db import connect
from ledger_memory.documents import DocumentNotFoundError, _fetch_document_row
from ledger_memory.emission import emit_event_only


@dataclass(frozen=True)
class Chunk:
    id: int
    document_id: int
    chunk_index: int
    content: str
    content_sha256: str
    chunking_recipe: str
    projection_version: str
    created_at: str


def chunk_document(
    database_path: Path,
    *,
    document_id: int,
    chunker: Chunker,
    actor_id: int,
    reason: str | None = None,
) -> list[Chunk]:
    """Chunk *document_id* with *chunker* and store the set; idempotent by (document, projection).

    Raises :class:`~ledger_memory.documents.DocumentNotFoundError` if the document is absent. If a set for this
    ``projection_version`` already exists it is returned unchanged (no INSERT, no event). A whitespace-only
    document yields zero chunks and emits no event.
    """
    projection_version = chunker.projection_version
    recipe_json = json.dumps(chunker.recipe(), sort_keys=True)
    with connect(database_path) as conn:
        document_row = _fetch_document_row(conn, document_id)
        if document_row is None:
            raise DocumentNotFoundError(document_id)

        existing = _fetch_chunk_rows(conn, document_id, projection_version)
        if existing:
            return [_row_to_chunk(r) for r in existing]  # idempotent — same document + projection

        pieces = chunker.chunk(document_row["content"])
        if not pieces:
            return []  # nothing to chunk (empty/whitespace document); no rows, no event

        for piece in pieces:
            content_sha256 = hashlib.sha256(piece.content.encode("utf-8")).hexdigest()
            conn.execute(
                """
                INSERT INTO chunks
                    (document_id, chunk_index, content, content_sha256, chunking_recipe,
                     projection_version)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (document_id, piece.index, piece.content, content_sha256, recipe_json,
                 projection_version),
            )
        emit_event_only(
            conn,
            event_type="document.chunked",
            actor_id=actor_id,
            summary=f"document {document_id} chunked into {len(pieces)} ({projection_version})",
            metadata=json.dumps({
                "document_id": document_id,
                "projection_version": projection_version,
                "chunk_count": len(pieces),
                "recipe": chunker.recipe(),
                "reason": reason,
            }),
        )
        return [_row_to_chunk(r) for r in _fetch_chunk_rows(conn, document_id, projection_version)]


def list_chunks(
    database_path: Path,
    *,
    document_id: int,
    projection_version: str | None = None,
) -> list[Chunk]:
    with connect(database_path) as conn:
        if projection_version is not None:
            cursor = conn.execute(
                "SELECT * FROM chunks WHERE document_id = ? AND projection_version = ? "
                "ORDER BY projection_version, chunk_index",
                (document_id, projection_version),
            )
        else:
            cursor = conn.execute(
                "SELECT * FROM chunks WHERE document_id = ? ORDER BY projection_version, chunk_index",
                (document_id,),
            )
        return [_row_to_chunk(r) for r in cursor.fetchall()]


def _fetch_chunk_rows(connection, document_id: int, projection_version: str):
    return connection.execute(
        "SELECT * FROM chunks WHERE document_id = ? AND projection_version = ? "
        "ORDER BY chunk_index",
        (document_id, projection_version),
    ).fetchall()


def _row_to_chunk(row) -> Chunk:
    return Chunk(
        id=row["id"],
        document_id=row["document_id"],
        chunk_index=row["chunk_index"],
        content=row["content"],
        content_sha256=row["content_sha256"],
        chunking_recipe=row["chunking_recipe"],
        projection_version=row["projection_version"],
        created_at=row["created_at"],
    )
