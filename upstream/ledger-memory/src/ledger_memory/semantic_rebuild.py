from __future__ import annotations
from ledger_memory.paths import default_vectors_path

import os
import struct
import hashlib
import math
from dataclasses import dataclass
from pathlib import Path

from ledger_memory.db import connect, connect_readonly
from ledger_memory.embeddings import get_embedding_backend
from ledger_memory.search_index import _source_rows, _content_hash, _matches_scope
from ledger_memory.semantic_vec import META_TABLE, build_vec_index, vec_backend_requested


class SourceChangedDuringEmbeddingError(Exception):
    """Raised when the source manifest changes between snapshot and commit during active-model replacement."""
    def __init__(self, original_hash: str, current_hash: str) -> None:
        self.original_hash = original_hash
        self.current_hash = current_hash
        super().__init__(
            f"source manifest changed during embedding: "
            f"original={original_hash[:12]} current={current_hash[:12]}"
        )

# max texts per embed call during a rebuild, so a network backend (LM Studio) doesn't blow its
# per-request timeout on a big db. Override with LEDGER_EMBED_BATCH.
try:
    EMBED_BATCH = max(1, int(os.environ.get("LEDGER_EMBED_BATCH", "64")))
except ValueError:
    EMBED_BATCH = 64


SIDECAR_STATEMENTS = ("""
CREATE TABLE IF NOT EXISTS semantic_vectors (
    record_type  TEXT    NOT NULL,
    record_id    INTEGER NOT NULL,
    embedding_model TEXT NOT NULL,
    vector       BLOB    NOT NULL,
    embedded_at  TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (record_type, record_id, embedding_model)
);
""", """
CREATE TABLE IF NOT EXISTS semantic_vector_revisions (
    record_type TEXT NOT NULL,
    record_id INTEGER NOT NULL,
    embedding_model TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    vector_sha256 TEXT NOT NULL,
    PRIMARY KEY (record_type, record_id, embedding_model)
);
""", """
CREATE TABLE IF NOT EXISTS embedding_failures (
    record_type TEXT NOT NULL,
    record_id INTEGER NOT NULL,
    content_hash TEXT NOT NULL,
    embedding_model TEXT NOT NULL,
    attempts INTEGER NOT NULL CHECK (attempts BETWEEN 1 AND 3),
    last_error_class TEXT NOT NULL,
    failed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (record_type, record_id, content_hash, embedding_model)
);
""")
SIDECAR_DDL = "\n".join(SIDECAR_STATEMENTS)


def invalidate_record_ann(connection, embedding_model: str) -> None:
    """Revoke stale ANN admission in the raw-vector mutation transaction."""
    if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                          (META_TABLE,)).fetchone() is not None:
        connection.execute(f"DELETE FROM {META_TABLE} WHERE model=?", (embedding_model,))


def write_revision_receipt(connection, *, record_type, record_id, embedding_model,
                           content_hash, vector_blob):
    """Caller owns the sidecar transaction containing BOTH vector and receipt."""
    connection.execute("INSERT OR REPLACE INTO semantic_vector_revisions VALUES(?,?,?,?,?)",
                       (record_type, record_id, embedding_model, content_hash,
                        hashlib.sha256(vector_blob).hexdigest()))
    if record_type != "chunk":
        invalidate_record_ann(connection, embedding_model)


def pack_embedding_vector(vector, *, dimensions: int) -> bytes:
    """The shared publication contract for record and chunk vector bytes."""
    try:
        invalid = not isinstance(vector, list) or len(vector) != dimensions or any(
            type(value) not in (int, float) or not math.isfinite(value) for value in vector
        ) or not math.isclose(math.hypot(*vector), 1.0, rel_tol=1e-3, abs_tol=1e-3)
    except OverflowError:
        invalid = True
    if invalid:
        raise ValueError("embedding vector count/values/norm are invalid")
    return struct.pack(f"{dimensions}f", *vector)


def _publication_snapshot_hash(rows) -> str:
    digest = hashlib.sha256()
    for kind, identifier, title, body in sorted(rows, key=lambda row: (row[0], row[1])):
        digest.update(f"{kind}\x1f{identifier}\x1f{_content_hash(kind, identifier, title, body)}\x1e".encode("utf-8"))
    return digest.hexdigest()


def _verify_publication_source(core, database_path, backend, captured_rows, *, record_type=None, record_id=None):
    """Called under the core writer, after inference, before projection changes."""
    from ledger_memory.embeddings import get_embedding_backend as current_backend, SemanticSearchNotConfiguredError
    try:
        selected = current_backend(database_path)
    except SemanticSearchNotConfiguredError:
        raise RuntimeError("embedding model disabled during rebuild; retry manually") from None
    if (selected.model_name, selected.dimensions) != (backend.model_name, backend.dimensions):
        raise RuntimeError("embedding model changed during rebuild; retry manually")
    current = [row for row in _source_rows(core)
               if _matches_scope(row[0], row[1], record_type, record_id)]
    original_hash = _publication_snapshot_hash(captured_rows)
    current_hash = _publication_snapshot_hash(current)
    if current_hash != original_hash:
        raise SourceChangedDuringEmbeddingError(original_hash, current_hash)


@dataclass(frozen=True)
class SemanticRebuildPreview:
    scope_type: str | None
    scope_id: int | None
    expected_rows: int
    current_metadata_rows: int
    missing_rows: int
    stale_rows: int
    orphaned_rows: int
    would_embed_rows: int
    embedding_model: str


@dataclass(frozen=True)
class SemanticRebuildResult:
    embedded_rows: int
    skipped_rows: int
    embedding_model: str
    scope_type: str | None
    scope_id: int | None


class SemanticRebuildScopeNotFoundError(Exception):
    def __init__(self, record_type: str, record_id: int) -> None:
        self.record_type = record_type
        self.record_id = record_id
        super().__init__(f"record not found: {record_type} {record_id}")


def _scope_notes(record_type: str | None, record_id: int | None) -> str | None:
    if record_type is None:
        return None
    if record_id is None:
        return f"scope_type={record_type}"
    return f"scope_type={record_type} scope_id={record_id}"


def preview_semantic_index_rebuild(
    database_path: Path,
    *,
    record_type: str | None = None,
    record_id: int | None = None,
    force: bool = False,
) -> SemanticRebuildPreview:
    backend = get_embedding_backend(database_path)

    # a preview is a pure read — use the read-only connector so it never flips a DELETE-journal DB to
    # WAL or creates `-wal`/`-shm` sidecars (the write connector `connect()` would).
    with connect_readonly(database_path) as core_conn:
        all_rows = _source_rows(core_conn)
        filtered_rows = [
            row for row in all_rows
            if _matches_scope(row[0], row[1], record_type, record_id)
        ]

        existing = {}
        for entry in core_conn.execute(
            "SELECT record_type, record_id, content_hash FROM semantic_index_entries WHERE embedding_model = ?",
            (backend.model_name,),
        ).fetchall():
            existing[(str(entry["record_type"]), int(entry["record_id"]))] = str(entry["content_hash"])

        scoped_existing = {
            key: value for key, value in existing.items()
            if _matches_scope(key[0], int(key[1]), record_type, record_id)
        }

        current_metadata_rows = len(scoped_existing)
        expected_rows = len(filtered_rows)

        filtered_keys = {(row[0], row[1]) for row in filtered_rows}
        scoped_existing_keys = set(scoped_existing.keys())

        missing_rows = 0
        stale_rows = 0

        for rtype, rid, title, body in filtered_rows:
            key = (rtype, rid)
            h = _content_hash(rtype, rid, title, body)
            if key not in scoped_existing:
                missing_rows += 1
            elif scoped_existing[key] != h:
                stale_rows += 1

        orphaned_rows = len(scoped_existing_keys - filtered_keys)
        # a forced rebuild re-embeds EVERY scoped row regardless of content-hash match — the
        # dry-run preview must say so (would_embed_rows == expected_rows), not silently report the unforced
        # missing+stale diff while a real `--force` (no --dry-run) would then embed far more than previewed.
        would_embed_rows = expected_rows if force else missing_rows + stale_rows

        return SemanticRebuildPreview(
            scope_type=record_type,
            scope_id=record_id,
            expected_rows=expected_rows,
            current_metadata_rows=current_metadata_rows,
            missing_rows=missing_rows,
            stale_rows=stale_rows,
            orphaned_rows=orphaned_rows,
            would_embed_rows=would_embed_rows,
            embedding_model=backend.model_name,
        )


def rebuild_semantic_index(
    database_path: Path,
    *,
    record_type: str | None = None,
    record_id: int | None = None,
    force: bool = False,
    sidecar_path: Path | None = None,
) -> SemanticRebuildResult:
    from ledger_memory.semantic_binding import foreign_binding, resolve_write_sidecar, SidecarOwnershipError

    sidecar_path = resolve_write_sidecar(database_path, sidecar_path)

    reason = foreign_binding(database_path, sidecar_path)
    if reason is not None:
        raise SidecarOwnershipError(
            f"refusing to write semantic vectors for {database_path} into {sidecar_path}: "
            f"{reason}. Pass this database's own sidecar explicitly (--sidecar / sidecar_path=) "
            f"or set LEDGER_VECTORS_PATH."
        )

    sidecar_path.parent.mkdir(parents=True, exist_ok=True)

    backend = get_embedding_backend(database_path)

    with connect(sidecar_path) as sidecar_conn:
        with connect(database_path) as core_conn:
            core_conn.execute("BEGIN")
            all_rows = _source_rows(core_conn)
            filtered_rows = [
                row for row in all_rows
                if _matches_scope(row[0], row[1], record_type, record_id)
            ]

            if record_type is not None and record_id is not None and not filtered_rows:
                # a scoped rebuild of a record DELETED from the source must still be allowed to PRUNE
                # its orphaned entry/vector — only treat the scope as "not found" if there is also nothing indexed.
                already_indexed = core_conn.execute(
                    "SELECT 1 FROM semantic_index_entries WHERE record_type = ? AND record_id = ? AND embedding_model = ? LIMIT 1",
                    (record_type, record_id, backend.model_name),
                ).fetchone()
                if not already_indexed:
                    raise SemanticRebuildScopeNotFoundError(record_type, record_id)

            skipped_rows = 0
            to_embed: list[tuple[str, int, str, str]] = []

            if not force:
                from ledger_memory.semantic_pending import pending_embeddings
                pending_keys = {(item.record_type, item.record_id) for item in pending_embeddings(
                    database_path, sidecar_path, backend=backend, include_failed=True)
                    if item.record_type != "chunk"}
                for rtype, rid, title, body in filtered_rows:
                    if (rtype, rid) not in pending_keys:
                        skipped_rows += 1
                    else:
                        to_embed.append((rtype, rid, title, body))
            else:
                to_embed = list(filtered_rows)

            core_conn.rollback()
            # Release the read snapshot too: inference must hold no database
            # transaction. Stage every validated batch before publication.
            embedded: list[tuple[str, int, str, bytes]] = []
            for start in range(0, len(to_embed), EMBED_BATCH):
                batch = to_embed[start:start + EMBED_BATCH]
                vectors = backend.embed([f"{title} {body}" for _, _, title, body in batch])
                if not isinstance(vectors, list) or len(vectors) != len(batch):
                    raise ValueError("embedding backend returned an invalid vector count")
                for (rtype, rid, title, body), vector in zip(batch, vectors):
                    packed = pack_embedding_vector(vector, dimensions=backend.dimensions)
                    h = _content_hash(rtype, rid, title, body)
                    embedded.append((rtype, rid, h, packed))

            core_conn.execute("BEGIN IMMEDIATE")
            _verify_publication_source(core_conn, database_path, backend, filtered_rows,
                                       record_type=record_type, record_id=record_id)
            sidecar_conn.execute("BEGIN IMMEDIATE")
            if foreign_binding(database_path, sidecar_path) is not None:
                raise SidecarOwnershipError("sidecar ownership changed during rebuild")
            if skipped_rows:
                # A source can change away and back while another publisher
                # updates its vector. Source equality alone cannot validate skips.
                skipped_keys = {(row[0], row[1]) for row in filtered_rows} - {
                    (row[0], row[1]) for row in embedded}
                if any((item.record_type, item.record_id) in skipped_keys
                       for item in pending_embeddings(database_path, sidecar_path, backend=backend,
                                                      include_failed=True)):
                    raise RuntimeError("semantic index changed during rebuild; retry manually")
            for statement in SIDECAR_STATEMENTS:
                sidecar_conn.execute(statement)
            rebuild_cursor = core_conn.execute(
                "INSERT INTO semantic_index_rebuilds (status, embedding_model, notes) VALUES ('running', ?, ?)",
                (backend.model_name, _scope_notes(record_type, record_id)),
            )
            rebuild_id = int(rebuild_cursor.lastrowid)

            for rtype, rid, h, packed in embedded:
                sidecar_conn.execute(
                    "INSERT OR REPLACE INTO semantic_vectors (record_type, record_id, embedding_model, vector) VALUES (?, ?, ?, ?)",
                    (rtype, rid, backend.model_name, packed),
                )
                write_revision_receipt(sidecar_conn, record_type=rtype, record_id=rid,
                    embedding_model=backend.model_name, content_hash=h, vector_blob=packed)
                core_conn.execute(
                    "INSERT OR REPLACE INTO semantic_index_entries (record_type, record_id, content_hash, embedding_model, embedding_dimensions) VALUES (?, ?, ?, ?, ?)",
                    (rtype, rid, h, backend.model_name, backend.dimensions),
                )

            # the index was INSERT-OR-REPLACE only, so a source row deleted since the last build left
            # an ORPHAN entry/vector that still surfaced in semantic search. Prune derived rows (this model, within the
            # rebuild's scope) whose (record_type, record_id) is no longer in the live source — from BOTH the core
            # table and the sidecar, in lockstep with the brute-force source so the index never drifts.
            live_keys = {(rtype, rid) for rtype, rid, _title, _body in filtered_rows}
            for entry in core_conn.execute(
                "SELECT record_type, record_id FROM semantic_index_entries WHERE embedding_model = ?",
                (backend.model_name,),
            ).fetchall():
                key = (str(entry["record_type"]), int(entry["record_id"]))
                if key in live_keys or not _matches_scope(key[0], key[1], record_type, record_id):
                    continue
                core_conn.execute(
                    "DELETE FROM semantic_index_entries WHERE record_type = ? AND record_id = ? AND embedding_model = ?",
                    (key[0], key[1], backend.model_name),
                )
                sidecar_conn.execute(
                    "DELETE FROM semantic_vectors WHERE record_type = ? AND record_id = ? AND embedding_model = ?",
                    (key[0], key[1], backend.model_name),
                )
                sidecar_conn.execute(
                    "DELETE FROM semantic_vector_revisions WHERE record_type=? AND record_id=? AND embedding_model=?",
                    (key[0], key[1], backend.model_name))
                invalidate_record_ann(sidecar_conn, backend.model_name)

            # Stage ANN in the sidecar transaction. These two databases still
            # commit separately; revision receipts expose a split, not atomicity.
            if vec_backend_requested():
                build_vec_index(sidecar_conn, model_name=backend.model_name, dimensions=backend.dimensions)

            # an ordinary rebuild MAINTAINS a receipt that is still valid for this database,
            # inside this same transaction. Without it the certifying receipt froze at the row count of
            # its certification: every later write made core metadata stale, self-heal added the vectors,
            # the count moved, and the binding gate closed the lane FOREVER while `semantic=fresh` was
            # still being printed. Measured live on DEV at 24c2af1 (1842 rows vs a receipt certifying
            # 1832). `maintain_receipt` never mints trust — an unbound sidecar stays unbound and a
            # foreign one stays blocking; only drift this rebuild just corrected is advanced.
            from ledger_memory.semantic_binding import maintain_receipt, source_manifest_hash
            live_count = sidecar_conn.execute(
                "SELECT COUNT(*) FROM semantic_vectors WHERE embedding_model = ?",
                (backend.model_name,),
            ).fetchone()[0]
            receipt_state = maintain_receipt(
                database_path, sidecar_conn,
                active_model=backend.model_name,
                row_count=int(live_count),
                source_manifest_hash=source_manifest_hash(database_path),
            )

    # mark the core rebuild 'completed' ONLY after the sidecar vectors AND the optional ANN build have
    # succeeded and committed. If anything above raised, the outer sidecar transaction rolled back and we never reach
    # here, so the rebuild row stays 'running' — an honest "incomplete", not a false "completed" over an empty sidecar.
    with connect(database_path) as core_conn:
        core_conn.execute(
            "UPDATE semantic_index_rebuilds SET status = 'completed', completed_at = CURRENT_TIMESTAMP, indexed_rows = ? WHERE id = ?",
            (len(to_embed), rebuild_id),
        )

    return SemanticRebuildResult(
        embedded_rows=len(to_embed),
        skipped_rows=skipped_rows,
        embedding_model=backend.model_name,
        scope_type=record_type,
        scope_id=record_id,
    )


@dataclass(frozen=True)
class ReplacementRebuildResult:
    """Every field is a POSTCONDITION observation, not an intention."""
    dry_run: bool
    authoritative_rows: int
    active_model_rows_before: int
    unexpected_before: int
    missing_before: int
    embedded_rows: int
    active_model_rows_after: int | None
    receipt_verified: bool | None
    gate_open: bool | None
    rollback_artifact: str | None
    embedding_model: str
    generation: int | None


def replace_active_model_rebuild(
    database_path: Path,
    *,
    sidecar_path: Path | None = None,
    dry_run: bool = False,
    require_owned: bool = False,
) -> ReplacementRebuildResult:
    """Full active-model replacement: the sidecar's active-model rows become EXACTLY the embedded set.

    Why `--force` cannot do this: its pruning
    iterates CORE `semantic_index_entries`, so a row existing only in `semantic_vectors` — the shape of
    the 11,311 live foreign rows — is invisible to it. Replacement is delete-then-insert over the whole
    active-model partition, never upsert.

    Contract highlights: one pinned source snapshot with a deterministic manifest;
    embed everything BEFORE mutating; other-model rows untouched; the generation receipt is written LAST
    inside the same transaction; a retained `.pre-replacement-<stamp>` copy preserves the prior derived
    state; postconditions are verified after commit and reported, never assumed. No caller is automatic.
    `require_owned` narrows explicit provisioning to unbound/same-database sidecars;
    its ownership check is repeated inside the actual publication transaction.
    """
    from ledger_memory.semantic_binding import (
        GenerationReceipt, binding_violation, ensure_lineage_id, write_receipt_with, LINEAGE_KEY,
        foreign_binding, read_receipt,
    )
    from ledger_memory.semantic_search import semantic_lane_blocked
    from datetime import datetime, timezone

    def current_stamp() -> str:
        return datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")

    if sidecar_path is None:
        sidecar_path = default_vectors_path(database_path)
    if require_owned and foreign_binding(database_path, sidecar_path) is not None:
        raise RuntimeError("generation replacement refused: sidecar belongs to another database")
    sidecar_path.parent.mkdir(parents=True, exist_ok=True)

    backend = get_embedding_backend(database_path)
    model = backend.model_name

    # --- lineage BEFORE the snapshot (its event/audit rows must fall inside the manifest) ----------
    lineage = ensure_lineage_id(database_path)

    # --- pin ONE source snapshot with explicit transaction -----------------------------------------
    import hashlib as _hashlib
    with connect_readonly(database_path) as core:
        core.execute("BEGIN")
        try:
            rows = _source_rows(core)
        finally:
            core.rollback()

    manifest = _hashlib.sha256()
    for rtype, rid, title, body in sorted(rows, key=lambda r: (r[0], r[1])):
        content = _hashlib.sha256(f"{title}\n{body}".encode("utf-8")).hexdigest()
        manifest.update(f"{rtype}\x1f{rid}\x1f{content}\x1e".encode("utf-8"))
    manifest_hash = manifest.hexdigest()
    source_keys = {(rtype, rid) for rtype, rid, _t, _b in rows}

    # --- measure the current sidecar (also the whole of dry-run) -----------------------------------
    before = 0
    sidecar_keys: set[tuple[str, int]] = set()
    if sidecar_path.exists():
        with connect_readonly(sidecar_path) as sc:
            for row in sc.execute(
                "SELECT record_type, record_id FROM semantic_vectors WHERE embedding_model = ?", (model,)
            ).fetchall():
                sidecar_keys.add((str(row["record_type"]), int(row["record_id"])))
        before = len(sidecar_keys)
    unexpected_before = len(sidecar_keys - source_keys)
    missing_before = len(source_keys - sidecar_keys)

    if dry_run:
        return ReplacementRebuildResult(
            dry_run=True, authoritative_rows=len(rows), active_model_rows_before=before,
            unexpected_before=unexpected_before, missing_before=missing_before,
            embedded_rows=0, active_model_rows_after=None, receipt_verified=None,
            # The gate WOULD open iff the replacement set equals the source set and the receipt matches
            # — which is what the operation constructs, so the honest dry-run answer is the precondition
            # that can still fail: the embedding backend must be able to serve.
            gate_open=None, rollback_artifact=None, embedding_model=model, generation=None,
        )

    # --- embed EVERYTHING before touching the live projection --------------------------------------
    embedded: list[tuple[str, int, str, bytes]] = []
    for start in range(0, len(rows), EMBED_BATCH):
        batch = rows[start:start + EMBED_BATCH]
        vectors = backend.embed([f"{title} {body}" for _, _, title, body in batch])
        if not isinstance(vectors, list) or len(vectors) != len(batch):
            raise ValueError("embedding backend returned an invalid vector count")
        for (rtype, rid, title, body), vector in zip(batch, vectors):
            packed = pack_embedding_vector(vector, dimensions=backend.dimensions)
            embedded.append((rtype, rid, _content_hash(rtype, rid, title, body), packed))

    # --- re-verify the manifest before mutating ----------------------------------------------------
    with connect_readonly(database_path) as core:
        core.execute("BEGIN")
        try:
            current_rows = _source_rows(core)
        finally:
            core.rollback()
    current_manifest = _hashlib.sha256()
    for rtype, rid, title, body in sorted(current_rows, key=lambda r: (r[0], r[1])):
        content = _hashlib.sha256(f"{title}\n{body}".encode("utf-8")).hexdigest()
        current_manifest.update(f"{rtype}\x1f{rid}\x1f{content}\x1e".encode("utf-8"))
    current_manifest_hash = current_manifest.hexdigest()
    if current_manifest_hash != manifest_hash:
        raise SourceChangedDuringEmbeddingError(manifest_hash, current_manifest_hash)

    # --- retained rollback artifact (a SUCCESSFUL replacement must not silently delete history) ----
    rollback_artifact = None
    if sidecar_path.exists():
        base = f"{sidecar_path}.pre-replacement-{current_stamp()}"
        candidate, suffix = base, 1
        while Path(candidate).exists():
            candidate, suffix = f"{base}-{suffix}", suffix + 1
        rollback_artifact = candidate
        import sqlite3 as _sqlite3
        source_conn = _sqlite3.connect(str(sidecar_path))
        dest_conn = _sqlite3.connect(rollback_artifact)
        try:
            source_conn.backup(dest_conn)
        finally:
            dest_conn.close()
            source_conn.close()
        # Verify the artifact has at least the live active-model count.
        # review fix: `rollback_artifact` is a str and `connect_readonly` builds WAL paths
        # via `.parent` — it needs a Path. The delegated version raised AttributeError on the FIRST
        # real certification, which is exactly where a rollback artifact matters most.
        with connect_readonly(Path(rollback_artifact)) as verify_conn:
            artifact_count = int(verify_conn.execute(
                "SELECT COUNT(*) FROM semantic_vectors WHERE embedding_model = ?", (model,)
            ).fetchone()[0])
        if artifact_count < before:
            raise RuntimeError(
                f"rollback artifact verification failed: "
                f"artifact has {artifact_count} active-model rows, expected at least {before}"
            )

    # --- the replacement transaction ---------------------------------------------------------------
    with connect(sidecar_path) as sc:
        with connect(database_path) as core:
            core.execute("BEGIN IMMEDIATE")
            current_lineage = core.execute("SELECT value FROM config WHERE key=?", (LINEAGE_KEY,)).fetchone()
            if current_lineage is None or str(current_lineage["value"]) != lineage:
                raise RuntimeError("database lineage changed during replacement; retry manually")
            _verify_publication_source(core, database_path, backend, rows)
            sc.execute("BEGIN IMMEDIATE")
            if require_owned:
                existing = read_receipt(sidecar_path, connection=sc)
                if existing is not None and existing.lineage_id != lineage:
                    raise RuntimeError("generation replacement refused: sidecar ownership changed")
            for statement in SIDECAR_STATEMENTS:
                sc.execute(statement)
            cursor = core.execute(
                "INSERT INTO semantic_index_rebuilds (status, embedding_model, notes) "
                "VALUES ('running', ?, 'replace-active-model')", (model,),
            )
            generation = int(cursor.lastrowid)

            sc.execute("DELETE FROM semantic_vectors WHERE embedding_model = ?", (model,))
            sc.execute("DELETE FROM semantic_vector_revisions WHERE embedding_model=? AND record_type<>'chunk'", (model,))
            invalidate_record_ann(sc, model)
            core.execute("DELETE FROM semantic_index_entries WHERE embedding_model = ?", (model,))
            for rtype, rid, h, packed in embedded:
                sc.execute(
                    "INSERT INTO semantic_vectors (record_type, record_id, embedding_model, vector) "
                    "VALUES (?, ?, ?, ?)", (rtype, rid, model, packed))
                write_revision_receipt(sc, record_type=rtype, record_id=rid,
                    embedding_model=model, content_hash=h, vector_blob=packed)
                core.execute(
                    "INSERT INTO semantic_index_entries (record_type, record_id, content_hash, "
                    "embedding_model, embedding_dimensions) VALUES (?, ?, ?, ?, ?)",
                    (rtype, rid, h, model, backend.dimensions))

            if vec_backend_requested():
                build_vec_index(sc, model_name=model, dimensions=backend.dimensions)

            # The receipt goes LAST inside the transaction: if anything above failed we never certify,
            # and a certifying row can never describe a generation that was not fully written.
            receipt = GenerationReceipt(
                lineage_id=lineage, embedding_model=model, dimensions=backend.dimensions,
                row_count=len(embedded), generation=generation,
                source_manifest_hash=manifest_hash, completed_at=current_stamp(),
            )
            write_receipt_with(sc, receipt)

    with connect(database_path) as core:
        core.execute(
            "UPDATE semantic_index_rebuilds SET status = 'completed', completed_at = CURRENT_TIMESTAMP, "
            "indexed_rows = ? WHERE id = ?", (len(embedded), generation))

    # --- postconditions: OBSERVED after commit, never assumed --------------------------------------
    with connect_readonly(sidecar_path) as sc:
        after = int(sc.execute(
            "SELECT COUNT(*) FROM semantic_vectors WHERE embedding_model = ?", (model,)).fetchone()[0])
    from ledger_memory.semantic_binding import read_receipt as _read_receipt
    stored = _read_receipt(sidecar_path)
    receipt_verified = stored == receipt and binding_violation(
        database_path, sidecar_path, active_model=model) is None
    gate_open = semantic_lane_blocked(database_path, sidecar_path) is None

    return ReplacementRebuildResult(
        dry_run=False, authoritative_rows=len(rows), active_model_rows_before=before,
        unexpected_before=unexpected_before, missing_before=missing_before,
        embedded_rows=len(embedded), active_model_rows_after=after,
        receipt_verified=receipt_verified, gate_open=gate_open,
        rollback_artifact=rollback_artifact, embedding_model=model, generation=generation,
    )
