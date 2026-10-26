from __future__ import annotations

"""documents — the authoritative, immutable, hashed store for full-document RAG.

A document's CONTENT is stored once and never edited: ingestion is idempotent by ``content_sha256`` (the
SHA-256 of the content bytes), so re-ingesting identical bytes returns the existing row without a second
INSERT. A re-ingest carrying a STRICTER ``sensitivity_class`` RAISES the stored classification in place (never
a downgrade) and emits a ``document.reclassified`` event; every ingest of new content emits a
``document.ingested`` audit-and-event with the mandatory actor.
The ``chunks`` derived projection references documents. Document content reaches a model
context ONLY wrapped as evidence with provenance, enforced at retrieval (R.4).
"""

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from ledger_memory.db import connect
from ledger_memory.emission import emit_audit_and_event


@dataclass(frozen=True)
class Document:
    id: int
    origin_type: str
    uri: str | None
    media_type: str
    sensitivity_class: str
    content_sha256: str
    content: str
    retrieved_at: str
    ingested_by_actor_id: int
    created_at: str


class DocumentNotFoundError(Exception):
    """Raised when a document id does not exist."""

    def __init__(self, document_id: int):
        self.document_id = document_id
        super().__init__(f"document not found: {document_id}")


_SENSITIVITY_RANK = {"public": 0, "personal": 1, "sensitive": 2}


def _strictest_sensitivity(a: str, b: str) -> str:
    """The stricter of two sensitivity classes (public < personal < sensitive) — so a duplicate ingest can RAISE but
    never LOWER a document's classification."""
    return a if _SENSITIVITY_RANK.get(a, 0) >= _SENSITIVITY_RANK.get(b, 0) else b


def ingest_document(
    database_path: Path,
    *,
    content: str,
    origin_type: str,
    media_type: str,
    sensitivity_class: str = "personal",
    uri: str | None = None,
    retrieved_at: str,
    actor_id: int,
    reason: str | None = None,
) -> Document:
    """Store *content* as a document, idempotent by its SHA-256.

    Re-ingesting identical content returns the EXISTING document (no INSERT). It is a no-op UNLESS the re-ingest
    carries a STRICTER ``sensitivity_class`` (public < personal < sensitive) — then the stored row is RAISED in
    place (never downgraded) and a ``document.reclassified`` audit-and-event is emitted. A genuine
    new ingest INSERTs the row and emits a ``document.ingested`` audit-and-event in the same transaction.
    """
    content_sha256 = hashlib.sha256(content.encode("utf-8")).hexdigest()
    with connect(database_path) as conn:
        existing = _fetch_document_row_by_hash(conn, content_sha256)
        if existing is not None:
            existing_doc = _row_to_document(existing)
            # a hash-keyed re-ingest must NOT silently keep a WEAKER sensitivity than the caller asked
            # for — that downgrades the content (a sensitive document re-ingested onto a public row would stay publicly
            # retrievable). Take the STRICTEST of stored vs incoming; RAISE in place (audited) but never downgrade.
            strictest = _strictest_sensitivity(existing_doc.sensitivity_class, sensitivity_class)
            if strictest == existing_doc.sensitivity_class:
                return existing_doc  # identical bytes AND no stricter class → a true idempotent no-op
            conn.execute(
                "UPDATE documents SET sensitivity_class = ? WHERE id = ?",
                (strictest, existing_doc.id),
            )
            emit_audit_and_event(
                conn,
                action_type="document.reclassified",
                target_type="document",
                target_id=existing_doc.id,
                actor_id=actor_id,
                event_type="document.reclassified",
                reason=reason,
                summary=(
                    f"Document {existing_doc.id} sensitivity raised "
                    f"{existing_doc.sensitivity_class} → {strictest} on duplicate ingest"
                ),
                metadata=json.dumps({
                    "content_sha256": content_sha256,
                    "from_sensitivity_class": existing_doc.sensitivity_class,
                    "to_sensitivity_class": strictest,
                }),
            )
            return _row_to_document(_fetch_document_row(conn, existing_doc.id))

        cursor = conn.execute(
            """
            INSERT INTO documents
                (origin_type, uri, media_type, sensitivity_class, content_sha256, content,
                 retrieved_at, ingested_by_actor_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (origin_type, uri, media_type, sensitivity_class, content_sha256, content,
             retrieved_at, actor_id),
        )
        document_id = cursor.lastrowid
        emit_audit_and_event(
            conn,
            action_type="document.ingested",
            target_type="document",
            target_id=document_id,
            actor_id=actor_id,
            event_type="document.ingested",
            reason=reason,
            summary=f"Document ingested: {document_id} ({media_type}, {sensitivity_class})",
            metadata=json.dumps({
                "content_sha256": content_sha256,
                "origin_type": origin_type,
                "sensitivity_class": sensitivity_class,
            }),
        )
        return _row_to_document(_fetch_document_row(conn, document_id))


def get_document(database_path: Path, *, document_id: int) -> Document:
    with connect(database_path) as conn:
        row = _fetch_document_row(conn, document_id)
        if row is None:
            raise DocumentNotFoundError(document_id)
        return _row_to_document(row)


def list_documents(database_path: Path, *, sensitivity_class: str | None = None) -> list[Document]:
    with connect(database_path) as conn:
        if sensitivity_class is not None:
            cursor = conn.execute(
                "SELECT * FROM documents WHERE sensitivity_class = ? ORDER BY created_at DESC, id DESC",
                (sensitivity_class,),
            )
        else:
            cursor = conn.execute("SELECT * FROM documents ORDER BY created_at DESC, id DESC")
        return [_row_to_document(r) for r in cursor.fetchall()]


def _fetch_document_row(connection, document_id: int):
    return connection.execute("SELECT * FROM documents WHERE id = ?", (document_id,)).fetchone()


def _fetch_document_row_by_hash(connection, content_sha256: str):
    return connection.execute(
        "SELECT * FROM documents WHERE content_sha256 = ?", (content_sha256,)
    ).fetchone()


def _row_to_document(row) -> Document:
    return Document(
        id=row["id"],
        origin_type=row["origin_type"],
        uri=row["uri"],
        media_type=row["media_type"],
        sensitivity_class=row["sensitivity_class"],
        content_sha256=row["content_sha256"],
        content=row["content"],
        retrieved_at=row["retrieved_at"],
        ingested_by_actor_id=row["ingested_by_actor_id"],
        created_at=row["created_at"],
    )
