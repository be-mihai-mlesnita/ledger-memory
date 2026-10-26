"""sqlite-vec ANN backend for the semantic-index sidecar.

ADAPTER-ONLY. Core (`db.py` / the core schema) NEVER imports this module and never depends on the
sqlite-vec extension — `sqlite_vec` is imported lazily *inside* ``load_extension`` and every failure is
swallowed, so importing this module is always safe even with no extension installed. It operates only on
the adapter-owned sidecar (``var/semantic.db``) and degrades gracefully to the brute-force lane
(``semantic_search.py``) whenever the extension is absent or the index doesn't match the active model.

The sidecar already stores raw little-endian f32 BLOB vectors in ``semantic_vectors``. Here
we build a sqlite-vec ``vec0`` virtual table ``semantic_vec0`` over those BLOBs (keyed by the
``semantic_vectors`` rowid) with ``distance_metric=cosine``, so ``score = 1 - distance`` matches the
existing brute-force cosine exactly. vec0's KNN is an exact C linear scan — identical results to
brute-force, just faster. Opt-in via ``LEDGER_SEMANTIC_BACKEND=sqlite-vec`` (default: brute-force).
"""
from __future__ import annotations

import os
import struct

VEC_TABLE = "semantic_vec0"
META_TABLE = "semantic_vec_meta"


def vec_backend_requested() -> bool:
    """True iff the owner opted into the sqlite-vec backend. Default is the brute-force lane."""
    return os.environ.get("LEDGER_SEMANTIC_BACKEND", "bruteforce").strip().lower() == "sqlite-vec"


def load_extension(conn) -> bool:
    """Load sqlite-vec onto *conn* (a sidecar connection). Returns False on ANY failure — package
    missing, a sqlite build without load-extension support, non-macOS, etc. — so the caller falls back."""
    try:
        import sqlite_vec
    except Exception:
        return False
    try:
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
        return True
    except Exception:
        return False


def build_vec_index(sidecar_conn, *, model_name: str, dimensions: int) -> bool:
    """(Re)build the vec0 ANN index from ``semantic_vectors`` for *model_name*. Idempotent — drops and
    repopulates so it never drifts from the brute-force source. No-op (False) if the extension can't load."""
    if not load_extension(sidecar_conn):
        return False
    dim = int(dimensions)
    sidecar_conn.execute(f"DROP TABLE IF EXISTS {VEC_TABLE}")
    sidecar_conn.execute(
        f"CREATE VIRTUAL TABLE {VEC_TABLE} USING vec0(embedding float[{dim}] distance_metric=cosine)"
    )
    sidecar_conn.execute(
        f"INSERT INTO {VEC_TABLE}(rowid, embedding) "
        "SELECT rowid, vector FROM semantic_vectors WHERE embedding_model = ?",
        (model_name,),
    )
    sidecar_conn.execute(f"CREATE TABLE IF NOT EXISTS {META_TABLE} (model TEXT, dimensions INTEGER)")
    sidecar_conn.execute(f"DELETE FROM {META_TABLE}")
    sidecar_conn.execute(
        f"INSERT INTO {META_TABLE}(model, dimensions) VALUES (?, ?)", (model_name, dim)
    )
    return True


def vec_search(sidecar_conn, query_vector, *, model_name: str, dimensions: int, limit: int):
    """KNN over the vec0 index. Returns a list of ``(record_type, record_id, score)`` sorted best-first
    (``score = 1 - cosine_distance``), or ``None`` if the index is absent / built for a different
    model-dimension / the extension can't load — in which case the caller falls back to brute-force."""
    if not load_extension(sidecar_conn):
        return None
    dim = int(dimensions)
    try:
        meta = sidecar_conn.execute(f"SELECT model, dimensions FROM {META_TABLE} LIMIT 1").fetchone()
    except Exception:
        return None
    if meta is None or str(meta["model"]) != model_name or int(meta["dimensions"]) != dim:
        return None
    packed = struct.pack(f"{dim}f", *query_vector)
    try:
        knn = sidecar_conn.execute(
            f"SELECT rowid, distance FROM {VEC_TABLE} WHERE embedding MATCH ? AND k = ? ORDER BY distance",
            (packed, int(limit)),
        ).fetchall()
    except Exception:
        return None
    results: list[tuple[str, int, float]] = []
    for row in knn:
        src = sidecar_conn.execute(
            "SELECT record_type, record_id FROM semantic_vectors WHERE rowid = ?",
            (int(row["rowid"]),),
        ).fetchone()
        if src is None:
            continue
        results.append((str(src["record_type"]), int(src["record_id"]), 1.0 - float(row["distance"])))
    # empty -> None so the caller falls back to brute-force (keeps the contract "non-empty list or None";
    # avoids a meta-matching-but-empty index silently bypassing the brute-force lane).
    return results or None


def vec_index_row_count(sidecar_conn, *, model_name: str, dimensions: int) -> int | None:
    """The REAL row count of the vec0 index on an ALREADY-OPEN (and, in search, pinned) connection.

    `semantic_search` owns membership and K expansion; this adapter owns the extension,
    the metadata verification and the table name. Returns None for absent/unavailable/mismatched state —
    the caller falls back to brute force. review amendment 2: takes the caller's connection, never opens
    its own — a private connection would defeat the explicit `BEGIN` snapshot contract.
    """
    if not load_extension(sidecar_conn):
        return None
    try:
        meta = sidecar_conn.execute(f"SELECT model, dimensions FROM {META_TABLE} LIMIT 1").fetchone()
    except Exception:
        return None
    if meta is None or str(meta["model"]) != model_name or int(meta["dimensions"]) != int(dimensions):
        return None
    try:
        return int(sidecar_conn.execute(f"SELECT COUNT(*) FROM {VEC_TABLE}").fetchone()[0])
    except Exception:
        return None
