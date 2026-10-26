"""Derive semantic maintenance work from current sources; never a source of truth."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace
from pathlib import Path

from .db import connect, connect_readonly
from .embeddings import SemanticSearchNotConfiguredError, get_embedding_backend
from .search_index import _content_hash, _source_rows


@dataclass(frozen=True)
class PendingEmbedding:
    record_type: str
    record_id: int
    content_hash: str
    text: str
    attempts: int = 0


def revision_matches(item: PendingEmbedding, blob, receipt, metadata, *, dimensions: int) -> bool:
    """Validate values from the caller's actual pinned query/maintenance snapshots.

    The model is selected by the caller's SQL predicates. Chunk vectors have no
    record-lane core metadata; their source hash and byte receipt still must match.
    This proves revision consistency, not generation ownership or vector quality.
    """
    if not isinstance(blob, bytes) or len(blob) != dimensions * 4:
        return False
    if item.record_type != "chunk" and metadata != (item.content_hash, dimensions):
        return False
    return receipt == (item.content_hash, hashlib.sha256(blob).hexdigest())


def _source_items(core):
    items = [PendingEmbedding(kind, identifier, _content_hash(kind, identifier, title, body),
                              f"{title} {body}")
             for kind, identifier, title, body in _source_rows(core)]
    items.extend(PendingEmbedding("chunk", int(row["id"]),
        hashlib.sha256(row["content"].encode("utf-8")).hexdigest(), row["content"])
        for row in core.execute("SELECT id,content FROM chunks ORDER BY id"))
    return items


def _snapshot(database_path: Path, model: str):
    # Pin actual source and its core metadata together. Do not widen _source_rows.
    with connect_readonly(database_path) as core:
        core.execute("BEGIN")
        try:
            items = _source_items(core)
            metadata = {(row["record_type"], int(row["record_id"])):
                        (row["content_hash"], row["embedding_dimensions"])
                        for row in core.execute(
                            "SELECT record_type,record_id,content_hash,embedding_dimensions "
                            "FROM semantic_index_entries WHERE embedding_model=?", (model,))}
        finally:
            core.rollback()
    return items, metadata


def pending_embeddings(database_path: Path, sidecar_path: Path, *, backend=None,
                       include_failed: bool = False) -> list[PendingEmbedding]:
    """Read-only derivation; a matching receipt must describe the actual bytes.

    Missing sidecar/schema means pending, not an implicit initialization. Exhausted
    revision/model failures remain observable with include_failed=True.
    """
    backend = backend if backend is not None else get_embedding_backend(database_path)
    model = backend.model_name
    items, metadata = _snapshot(database_path, model)
    sidecar_path = Path(sidecar_path)
    if not sidecar_path.exists():
        return items
    with connect_readonly(sidecar_path) as sidecar:
        sidecar.execute("BEGIN")
        try:
            tables = {row["name"] for row in sidecar.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            receipts = {}
            if "semantic_vector_revisions" in tables:
                receipts = {(row["record_type"], int(row["record_id"])):
                            (row["content_hash"], row["vector_sha256"])
                            for row in sidecar.execute(
                                "SELECT record_type,record_id,content_hash,vector_sha256 "
                                "FROM semantic_vector_revisions WHERE embedding_model=?", (model,))}
            vectors = {}
            if "semantic_vectors" in tables:
                vectors.update({(row["record_type"], int(row["record_id"])): row["vector"]
                    for row in sidecar.execute(
                        "SELECT record_type,record_id,vector FROM semantic_vectors "
                        "WHERE embedding_model=? AND record_type<>'chunk'", (model,))})
            if "chunk_vectors" in tables:
                vectors.update({("chunk", int(row["chunk_id"])): row["vector"]
                    for row in sidecar.execute(
                        "SELECT chunk_id,vector FROM chunk_vectors WHERE embedding_model=?", (model,))})
            failures = {}
            if "embedding_failures" in tables:
                failures = {(row["record_type"], int(row["record_id"]), row["content_hash"]): int(row["attempts"])
                    for row in sidecar.execute(
                        "SELECT record_type,record_id,content_hash,attempts FROM embedding_failures "
                        "WHERE embedding_model=?", (model,))}
        finally:
            sidecar.rollback()
    pending = []
    for item in items:
        key = (item.record_type, item.record_id)
        if revision_matches(item, vectors.get(key), receipts.get(key), metadata.get(key),
                            dimensions=backend.dimensions):
            continue
        attempts = failures.get((*key, item.content_hash), 0)
        if include_failed or attempts < 3:
            pending.append(replace(item, attempts=attempts))
    return pending


def publish_embedding(database_path: Path, sidecar_path: Path, item: PendingEmbedding,
                      vector: list[float], *, backend, _after_sidecar_commit=None) -> bool:
    """Publish a previously computed result, rechecking source/model under writer lock.

    Vector and byte receipt commit together in the sidecar. Core metadata is a
    SEPARATE commit; a split remains pending, never claimed cross-database atomic.
    This function performs no model inference and emits no domain events.
    """
    from .chunk_embeddings import CHUNK_SIDECAR_DDL
    from .semantic_binding import SidecarOwnershipError, foreign_binding, maintain_receipt, source_manifest_hash
    from .semantic_rebuild import SIDECAR_STATEMENTS, write_revision_receipt, pack_embedding_vector

    packed = pack_embedding_vector(vector, dimensions=backend.dimensions)
    reason = foreign_binding(database_path, sidecar_path)
    if reason is not None:
        raise SidecarOwnershipError("refusing derived publication into a foreign sidecar")
    sidecar_path = Path(sidecar_path)
    sidecar_path.parent.mkdir(parents=True, exist_ok=True)
    with connect(database_path) as core:
        core.execute("BEGIN IMMEDIATE")
        try:
            current_backend = get_embedding_backend(database_path)
        except SemanticSearchNotConfiguredError:
            return False
        if (current_backend.model_name, current_backend.dimensions) != (backend.model_name, backend.dimensions):
            return False
        key = (item.record_type, item.record_id)
        current = next((row for row in _source_items(core)
                        if (row.record_type, row.record_id) == key), None)
        if current is None or current.content_hash != item.content_hash:
            return False
        with connect(sidecar_path) as sidecar:
            sidecar.execute("BEGIN IMMEDIATE")
            if foreign_binding(database_path, sidecar_path) is not None:
                raise SidecarOwnershipError("sidecar ownership changed before publication")
            # executescript would commit before running DDL, reopening the race.
            for statement in SIDECAR_STATEMENTS:
                sidecar.execute(statement)
            sidecar.execute(CHUNK_SIDECAR_DDL)
            if item.record_type == "chunk":
                sidecar.execute("INSERT OR REPLACE INTO chunk_vectors(chunk_id,embedding_model,vector) VALUES(?,?,?)",
                                (item.record_id, backend.model_name, packed))
            else:
                sidecar.execute("INSERT OR REPLACE INTO semantic_vectors(record_type,record_id,embedding_model,vector) "
                                "VALUES(?,?,?,?)", (*key, backend.model_name, packed))
            write_revision_receipt(sidecar, record_type=item.record_type, record_id=item.record_id,
                embedding_model=backend.model_name, content_hash=item.content_hash, vector_blob=packed)
            sidecar.execute("DELETE FROM embedding_failures WHERE record_type=? AND record_id=? "
                            "AND content_hash=? AND embedding_model=?",
                            (*key, item.content_hash, backend.model_name))
            count = sidecar.execute("SELECT COUNT(*) FROM semantic_vectors WHERE embedding_model=?",
                                    (backend.model_name,)).fetchone()[0]
            maintain_receipt(database_path, sidecar, active_model=backend.model_name,
                             row_count=int(count), source_manifest_hash=source_manifest_hash(database_path))
        if _after_sidecar_commit is not None:
            _after_sidecar_commit()
        if item.record_type != "chunk":
            core.execute("INSERT OR REPLACE INTO semantic_index_entries "
                         "(record_type,record_id,content_hash,embedding_model,embedding_dimensions) VALUES(?,?,?,?,?)",
                         (*key, item.content_hash, backend.model_name, backend.dimensions))
    return True


def _error_category(error: Exception) -> str:
    for known in (TimeoutError, ValueError, RuntimeError, OSError):
        if isinstance(error, known):
            return known.__name__
    return "EmbeddingError"


def record_embedding_failure(database_path: Path, sidecar_path: Path, item: PendingEmbedding,
                             *, backend, error: Exception) -> None:
    """Count one revision/model attempt; persist only a fixed error category."""
    from .semantic_binding import SidecarOwnershipError, foreign_binding
    from .semantic_rebuild import SIDECAR_STATEMENTS

    category = _error_category(error)
    if foreign_binding(database_path, sidecar_path) is not None:
        raise SidecarOwnershipError("refusing failure metadata in a foreign sidecar")
    sidecar_path = Path(sidecar_path)
    sidecar_path.parent.mkdir(parents=True, exist_ok=True)
    with connect(sidecar_path) as sidecar:
        sidecar.execute("BEGIN IMMEDIATE")
        if foreign_binding(database_path, sidecar_path) is not None:
            raise SidecarOwnershipError("sidecar ownership changed before failure accounting")
        for statement in SIDECAR_STATEMENTS:
            sidecar.execute(statement)
        sidecar.execute(
            "INSERT INTO embedding_failures(record_type,record_id,content_hash,embedding_model,attempts,last_error_class) "
            "VALUES(?,?,?,?,1,?) ON CONFLICT(record_type,record_id,content_hash,embedding_model) "
            "DO UPDATE SET attempts=MIN(3,embedding_failures.attempts+1), "
            "last_error_class=excluded.last_error_class,failed_at=CURRENT_TIMESTAMP",
            (item.record_type, item.record_id, item.content_hash, backend.model_name, category))


def drain_pending(database_path: Path, sidecar_path: Path, *, backend=None) -> dict[str, int]:
    """One resumable snapshot pass; inference holds no database transaction.

    An unavailable service (EmbeddingServiceUnavailable) ends the pass at once and is never charged to a record:
    the work stays pending and is derived again on the next write or server start."""
    from .embedding_worker import EmbeddingServiceUnavailable

    backend = backend if backend is not None else get_embedding_backend(database_path)
    items = pending_embeddings(database_path, sidecar_path, backend=backend)
    counts = {"published": 0, "failed": 0, "discarded": 0}
    for item in items:
        try:
            vectors = backend.embed([item.text])
            if len(vectors) != 1:
                raise ValueError("embedding backend returned an invalid vector count")
            vector = vectors[0]
        except EmbeddingServiceUnavailable:
            raise
        except Exception as error:
            record_embedding_failure(database_path, sidecar_path, item, backend=backend, error=error)
            counts["failed"] += 1
            continue
        try:
            published = publish_embedding(database_path, sidecar_path, item, vector, backend=backend)
        except ValueError as error:
            record_embedding_failure(database_path, sidecar_path, item, backend=backend, error=error)
            counts["failed"] += 1
            continue
        counts["published" if published else "discarded"] += 1
    return counts


def pending_lock_path(sidecar_path: Path) -> Path:
    path = Path(sidecar_path)
    path = path.parent.resolve() / path.name
    identity = hashlib.sha256(str(path).encode()).hexdigest()[:24]
    return path.parent / f".ledger-pending-{identity}.lock"


def _orphan_rows(core, sidecar, model: str):
    current = {(item.record_type, item.record_id) for item in _source_items(core)}
    rows = {"semantic_index_entries": {
        (row["record_type"], int(row["record_id"])) for row in core.execute(
            "SELECT record_type,record_id FROM semantic_index_entries WHERE embedding_model=?", (model,))
        if row["record_type"] == "chunk" or (row["record_type"], int(row["record_id"])) not in current}}
    if sidecar is None:
        return rows
    tables = {row["name"] for row in sidecar.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for table in ("semantic_vectors", "semantic_vector_revisions", "embedding_failures"):
        if table in tables:
            rows[table] = {(row["record_type"], int(row["record_id"])) for row in sidecar.execute(
                f"SELECT record_type,record_id FROM {table} WHERE embedding_model=?", (model,))
                if (row["record_type"], int(row["record_id"])) not in current
                or (table == "semantic_vectors" and row["record_type"] == "chunk")}
    if "chunk_vectors" in tables:
        rows["chunk_vectors"] = {("chunk", int(row["chunk_id"])) for row in sidecar.execute(
            "SELECT chunk_id FROM chunk_vectors WHERE embedding_model=?", (model,))
            if ("chunk", int(row["chunk_id"])) not in current}
    return rows


def has_orphaned_embeddings(database_path: Path, sidecar_path: Path, *, backend) -> bool:
    """Read-only final-work check, including deletions that create no pending source."""
    with connect_readonly(database_path) as core:
        core.execute("BEGIN")
        if not Path(sidecar_path).exists():
            return any(_orphan_rows(core, None, backend.model_name).values())
        with connect_readonly(sidecar_path) as sidecar:
            sidecar.execute("BEGIN")
            return any(_orphan_rows(core, sidecar, backend.model_name).values())


def prune_orphaned_embeddings(database_path: Path, sidecar_path: Path, *, backend) -> int:
    """Remove only active-model derived rows absent from the locked current source.

    This never certifies a generation or emits domain events. Core and sidecar
    commits remain separate; query gates still validate their actual snapshots.
    """
    from contextlib import ExitStack
    from .semantic_binding import (SidecarOwnershipError, foreign_binding, read_receipt,
                                   lineage_id, maintain_receipt, source_manifest_hash)
    from .semantic_rebuild import invalidate_record_ann
    if foreign_binding(database_path, sidecar_path) is not None:
        raise SidecarOwnershipError("refusing orphan maintenance for a foreign sidecar")
    with connect(database_path) as core:
        core.execute("BEGIN IMMEDIATE")
        try:
            selected = get_embedding_backend(database_path)
        except SemanticSearchNotConfiguredError:
            return 0
        if (selected.model_name, selected.dimensions) != (backend.model_name, backend.dimensions):
            return 0
        with ExitStack() as stack:
            sidecar = None
            if Path(sidecar_path).exists():
                sidecar = stack.enter_context(connect(sidecar_path))
                sidecar.execute("BEGIN IMMEDIATE")
                receipt = read_receipt(sidecar_path, connection=sidecar)
                if receipt is not None and receipt.lineage_id != lineage_id(database_path):
                    raise SidecarOwnershipError("sidecar ownership changed before orphan maintenance")
            orphans = _orphan_rows(core, sidecar, backend.model_name)
            for table, keys in orphans.items():
                connection = core if table == "semantic_index_entries" else sidecar
                for kind, identifier in keys:
                    if table == "chunk_vectors":
                        connection.execute("DELETE FROM chunk_vectors WHERE chunk_id=? AND embedding_model=?",
                                           (identifier, backend.model_name))
                    else:
                        connection.execute(f"DELETE FROM {table} WHERE record_type=? AND record_id=? AND embedding_model=?",
                                           (kind, identifier, backend.model_name))
            if sidecar is not None:
                if orphans.get("semantic_vectors") or orphans["semantic_index_entries"]:
                    invalidate_record_ann(sidecar, backend.model_name)
                tables = {row["name"] for row in sidecar.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if "semantic_vectors" in tables and any(orphans.values()):
                    count = sidecar.execute("SELECT COUNT(*) FROM semantic_vectors WHERE embedding_model=?",
                                            (backend.model_name,)).fetchone()[0]
                    maintain_receipt(database_path, sidecar, active_model=backend.model_name,
                                     row_count=int(count), source_manifest_hash=source_manifest_hash(database_path))
    return sum(len(keys) for keys in orphans.values())


def _require_database(database_path: Path) -> None:
    from .db import database_is_initialized

    if not database_is_initialized(database_path):
        raise RuntimeError("database is not initialized; run ledger init first")


def request_drain(database_path: Path, sidecar_path: Path) -> bool:
    """Coalesce writes without inference or waiting for an active coordinator."""
    from .embeddings import resolve_embedding_backend_name
    from .locking import exclusive
    from .semantic_binding import SidecarOwnershipError, foreign_binding
    from .worker_spawn import spawn_pending_worker

    _require_database(database_path)
    if resolve_embedding_backend_name(database_path) not in ("bge-m3", "fake"):
        return False
    if foreign_binding(database_path, sidecar_path) is not None:
        raise SidecarOwnershipError("refusing maintenance for a foreign sidecar")
    try:
        with exclusive(pending_lock_path(sidecar_path), timeout=0):
            pass
    except TimeoutError:
        return False
    # Never retain the probe lock while a fast child tries to acquire ownership.
    spawn_pending_worker(database_path, sidecar_path)
    return True


def run_pending_worker(database_path: Path, sidecar_path: Path, *, backend=None,
                       _after_unlock=None) -> dict[str, int | str]:
    """Drain under a per-sidecar lock; release then recheck closes lost wakeups."""
    from contextlib import ExitStack
    from .locking import session_lock_path, shared

    _require_database(database_path)
    # Hold the memory's shared session lock for the whole run: a restore cannot start mid-drain, and a drain started
    # during a restore leaves its work pending (it is derived again on the next write or start).
    session = ExitStack()
    try:
        session.enter_context(shared(session_lock_path(database_path), timeout=0))
    except TimeoutError:
        return {"status": "maintenance", "published": 0, "failed": 0, "discarded": 0}
    with session:
        return _run_pending_worker_locked(database_path, sidecar_path, backend=backend, _after_unlock=_after_unlock)


def _run_pending_worker_locked(database_path: Path, sidecar_path: Path, *, backend=None,
                               _after_unlock=None) -> dict[str, int | str]:
    from contextlib import ExitStack
    from .embedding_worker import EmbeddingServiceUnavailable
    from .locking import exclusive

    counts = {"published": 0, "failed": 0, "discarded": 0}

    def selected():
        actual = get_embedding_backend(database_path)
        if backend is not None and (actual.model_name, actual.dimensions) == (backend.model_name, backend.dimensions):
            return backend
        return actual

    while True:
        with ExitStack() as stack:
            try:
                stack.enter_context(exclusive(pending_lock_path(sidecar_path), timeout=0))
            except TimeoutError:
                return {"status": "busy", **counts}
            while True:
                try:
                    current = selected()
                except SemanticSearchNotConfiguredError:
                    break  # A producer may enable while our lock still coalesces it.
                prune_orphaned_embeddings(database_path, sidecar_path, backend=current)
                if not pending_embeddings(database_path, sidecar_path, backend=current):
                    break
                try:
                    result = drain_pending(database_path, sidecar_path, backend=current)
                except EmbeddingServiceUnavailable:
                    return {"status": "unavailable", **counts}  # nothing charged; retried on the next trigger
                for key in counts:
                    counts[key] += result[key]
        if _after_unlock is not None:
            _after_unlock()
        try:
            current = selected()
        except SemanticSearchNotConfiguredError:
            return {"status": "disabled", **counts}
        if (pending_embeddings(database_path, sidecar_path, backend=current)
                or has_orphaned_embeddings(database_path, sidecar_path, backend=current)):
            continue  # A writer saw our lock held; its write is not lost.
        failed = pending_embeddings(database_path, sidecar_path, backend=current, include_failed=True)
        return {"status": "degraded" if failed else "completed", **counts}


def main(argv=None) -> int:
    import argparse
    import json

    parser = argparse.ArgumentParser(description="Run one derived embedding maintenance job.")
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--sidecar", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        result = run_pending_worker(args.database, args.sidecar)
    except Exception as error:
        print(json.dumps({"status": "failed", "error_class": _error_category(error)}))
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0 if result["status"] in ("completed", "disabled", "busy", "unavailable", "maintenance") else 1


if __name__ == "__main__":
    raise SystemExit(main())


def reset_embedding_failures(sidecar_path: Path, model: str) -> int:
    """Forget the retry counts for one model (derived metadata only), so an explicit re-enable retries failed records."""
    import sqlite3

    path = Path(sidecar_path)
    if not path.exists():
        return 0
    with sqlite3.connect(path) as connection:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "embedding_failures" not in tables:
            return 0
        return connection.execute("DELETE FROM embedding_failures WHERE embedding_model=?", (model,)).rowcount
