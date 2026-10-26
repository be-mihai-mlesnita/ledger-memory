"""Generation-binding receipt: which corpus is this sidecar a projection OF?

The integrity report can say a sidecar's key set matches *some* database. It cannot say WHICH database
the vectors were computed from — and `semantic_search` maps `(record_type, record_id)` back to LOCAL
records, so a sidecar built from a different corpus whose numeric keys happen to collide returns real
local titles scored against entirely different content. That class is invisible to every key-membership
check; identity is the only thing that can see it.

a reviewer's contract, verbatim: "A database path is not an identity. A copied/restored database should
retain its lineage identity while a different database with colliding numeric keys must not satisfy the
receipt." So the identity lives IN the database file (`config` row), and the receipt lives IN the
sidecar — copy either and its identity travels with it; swap one out and the pair no longer proves.

The receipt is written ONLY by a certifying full replacement rebuild. Nothing else may
write it: a receipt that can be minted outside the rebuild it certifies is a signature without a signer.
"""
from __future__ import annotations
from ledger_memory.paths import default_vectors_path

import hashlib
import os
import uuid
from dataclasses import dataclass
from contextlib import nullcontext
from pathlib import Path

from ledger_memory.config import get_config
from ledger_memory.db import connect, connect_readonly
from ledger_memory.emission import emit_audit_and_event
from ledger_memory.search_index import _source_rows

LINEAGE_KEY = "semantic.lineage_id"

_RECEIPT_TABLE = """
CREATE TABLE IF NOT EXISTS semantic_generation_receipt (
    lineage_id           TEXT NOT NULL,
    embedding_model      TEXT NOT NULL,
    dimensions           INTEGER NOT NULL,
    row_count            INTEGER NOT NULL,
    generation           INTEGER NOT NULL,
    source_manifest_hash TEXT NOT NULL,
    completed_at         TEXT NOT NULL
)
"""


@dataclass(frozen=True)
class GenerationReceipt:
    lineage_id: str
    embedding_model: str
    dimensions: int
    row_count: int
    generation: int
    source_manifest_hash: str
    completed_at: str


def lineage_id(database_path: Path) -> str | None:
    """The database's stable corpus identity, or None if never assigned. Read-only."""
    return get_config(database_path, LINEAGE_KEY)


def ensure_lineage_id(database_path: Path, *, actor_id: int = 1) -> str:
    """Get-or-create the lineage id.

    Created ONCE and stored in `config`, so it travels with the file: a byte-copy or restore keeps its
    identity (same corpus, as required), while a different database — however similar its numeric key
    space — can never share it.

    First-mint-wins under concurrency (an earlier fix, mirroring an earlier fix's
    the lineage-id mint's): the insert is a single ON CONFLICT DO NOTHING
    statement (set_config's DO UPDATE would let a second overlapping process silently overwrite
    the first mint), and the audited config.set is emitted only when THIS call actually minted.
    """
    reason = "assign semantic corpus lineage identity"
    fresh = uuid.uuid4().hex
    with connect(database_path) as connection:
        cursor = connection.execute(
            "INSERT INTO config (key, value, actor_id, reason) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(key) DO NOTHING",
            (LINEAGE_KEY, fresh, actor_id, reason),
        )
        if cursor.rowcount == 1:
            emit_audit_and_event(
                connection,
                action_type="config.set",
                target_type="config",
                target_id=None,
                actor_id=actor_id,
                reason=reason,
                event_type="config.set",
                summary=f"config {LINEAGE_KEY} set",
            )
        row = connection.execute(
            "SELECT value FROM config WHERE key = ?", (LINEAGE_KEY,)
        ).fetchone()
    return str(row[0])


def source_manifest_hash(database_path: Path) -> str:
    """Deterministic sha256 over the sorted authoritative source set.

    This is the manifest a certifying rebuild pins: it proves WHAT was embedded. Readiness does NOT
    require current-manifest equality — that would re-close the lane on every ordinary write, the exact
    failure an earlier fix documented for the `missing_rows` arm.
    """
    with connect_readonly(database_path) as connection:
        rows = _source_rows(connection)
    digest = hashlib.sha256()
    for record_type, record_id, title, body in sorted(rows, key=lambda r: (r[0], r[1])):
        content = hashlib.sha256(f"{title}\n{body}".encode("utf-8")).hexdigest()
        digest.update(f"{record_type}\x1f{record_id}\x1f{content}\x1e".encode("utf-8"))
    return digest.hexdigest()


def write_receipt_with(connection, receipt: GenerationReceipt) -> None:
    """Replace THE receipt (single row) on an ALREADY-OPEN sidecar connection.

    Exists so a certifying rebuild can write the receipt INSIDE its replacement transaction — a receipt
    committed separately from the generation it certifies could describe rows that never landed.
    """
    connection.execute(_RECEIPT_TABLE)
    connection.execute("DELETE FROM semantic_generation_receipt")
    connection.execute(
        "INSERT INTO semantic_generation_receipt "
        "(lineage_id, embedding_model, dimensions, row_count, generation, source_manifest_hash, completed_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (receipt.lineage_id, receipt.embedding_model, receipt.dimensions, receipt.row_count,
         receipt.generation, receipt.source_manifest_hash, receipt.completed_at),
    )


def write_receipt(sidecar_path: Path, receipt: GenerationReceipt) -> None:
    """Replace THE receipt (single row) transactionally. Callers: certifying rebuilds only."""
    with connect(sidecar_path) as connection:
        write_receipt_with(connection, receipt)


def read_receipt(sidecar_path: Path, *, connection=None) -> GenerationReceipt | None:
    """The sidecar's receipt, or None when absent or malformed.

    A receipt table holding anything other than EXACTLY ONE row is malformed, not "pick one" —
    an earlier fix's lesson applies verbatim: choosing among conflicting claims is not validation.
    """
    if connection is None and not Path(sidecar_path).exists():
        return None
    try:
        with (nullcontext(connection) if connection is not None else connect_readonly(sidecar_path)) as connection:
            table = connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='semantic_generation_receipt'"
            ).fetchone()
            if table is None:
                return None
            rows = connection.execute("SELECT * FROM semantic_generation_receipt LIMIT 2").fetchall()
    except Exception:  # noqa: BLE001 — an unreadable receipt is not a trustworthy one
        return None
    if len(rows) != 1:
        return None
    row = rows[0]
    try:
        return GenerationReceipt(
            lineage_id=str(row["lineage_id"]),
            embedding_model=str(row["embedding_model"]),
            dimensions=int(row["dimensions"]),
            row_count=int(row["row_count"]),
            generation=int(row["generation"]),
            source_manifest_hash=str(row["source_manifest_hash"]),
            completed_at=str(row["completed_at"]),
        )
    except (KeyError, ValueError, TypeError, IndexError):
        return None


def binding_violation(database_path: Path, sidecar_path: Path, *, active_model: str,
                      core_connection=None, sidecar_connection=None) -> str | None:
    """The cheap identity check at the readiness chokepoint. None = no violation.

    TRANSITION MODE ENDED 2026-08-01: both live stores are now certified. A sidecar with no valid
    generation receipt while its database carries a lineage_id is a binding violation — the lane
    stays blocked until a certifying rebuild replaces the sidecar. Only a never-certified scratch
    database (no lineage_id) still returns None on absence.
    """
    # Optional caller-owned transactions bind the check to the snapshots scored;
    # nullcontext never commits, rolls back or closes those connections.
    def selected_lineage():
        if core_connection is None:
            return lineage_id(database_path)
        row = core_connection.execute("SELECT value FROM config WHERE key=?", (LINEAGE_KEY,)).fetchone()
        return str(row["value"]) if row is not None else None

    receipt = read_receipt(sidecar_path, connection=sidecar_connection)
    if receipt is None:
        db_lineage = selected_lineage()
        if db_lineage is not None:
            return (f"sidecar carries no valid generation receipt while database {database_path} "
                    f"is certified (lineage {db_lineage[:8]}…) — a certifying rebuild is required")
        return None

    db_lineage = selected_lineage()
    if db_lineage is None or db_lineage != receipt.lineage_id:
        return (f"sidecar is bound to a different corpus: receipt lineage {receipt.lineage_id[:8]}…, "
                f"database lineage {(db_lineage or 'unset')[:8]}…")
    if receipt.embedding_model != active_model:
        return (f"sidecar generation was embedded by {receipt.embedding_model!r}, "
                f"active model is {active_model!r}")
    try:
        with (nullcontext(sidecar_connection) if sidecar_connection is not None
              else connect_readonly(sidecar_path)) as connection:
            actual = connection.execute(
                "SELECT COUNT(*) FROM semantic_vectors WHERE embedding_model = ?", (active_model,)
            ).fetchone()[0]
    except Exception:  # noqa: BLE001
        return "sidecar row count could not be established for receipt verification"
    if int(actual) != receipt.row_count:
        return (f"sidecar holds {actual} active-model vectors but the receipt certifies "
                f"{receipt.row_count} — the generation has been modified since certification")
    return None


def maintain_receipt(database_path: Path, sidecar_connection, *, active_model: str, row_count: int, source_manifest_hash: str) -> str:
    """Update the receipt's mutable fields on an already-open sidecar connection.

    Returns one of:
    - "absent"   — no receipt table or no receipt row; writes NOTHING (certification is replace_active_model_rebuild's job)
    - "foreign"  — receipt exists but lineage_id or embedding_model mismatches, or database has no lineage; writes NOTHING
    - "advanced" — receipt matches on lineage_id and embedding_model; updates row_count and source_manifest_hash only
    """
    # Check if receipt table exists
    table = sidecar_connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='semantic_generation_receipt'"
    ).fetchone()
    if table is None:
        return "absent"

    # Fetch receipt rows — must be exactly one
    rows = sidecar_connection.execute("SELECT * FROM semantic_generation_receipt LIMIT 2").fetchall()
    if len(rows) != 1:
        return "foreign"

    row = rows[0]
    try:
        receipt_lineage_id = str(row["lineage_id"])
        receipt_embedding_model = str(row["embedding_model"])
        receipt_dimensions = int(row["dimensions"])
        receipt_generation = int(row["generation"])
    except (KeyError, ValueError, TypeError, IndexError):
        return "foreign"

    db_lineage = lineage_id(database_path)
    if db_lineage is None or db_lineage != receipt_lineage_id:
        return "foreign"
    if receipt_embedding_model != active_model:
        return "foreign"

    # Both match — advance mutable fields, preserve identity fields
    sidecar_connection.execute(
        "UPDATE semantic_generation_receipt SET row_count = ?, source_manifest_hash = ?",
        (row_count, source_manifest_hash),
    )
    return "advanced"



# the WRITE side of sidecar ownership. `binding_violation` (above) keeps a reader off a sidecar
# that is not this database's projection; nothing kept a WRITER off one, so a refresh that defaulted to a
# sidecar belonging to another database wrote foreign vectors into it.


class SidecarOwnershipError(RuntimeError):
    """Raised when a semantic write would target a sidecar bound to a different database."""




def foreign_binding(database_path: Path, sidecar_path: Path) -> str | None:
    """Why *sidecar_path* belongs to a different database, or None when writing into it is allowed.

    None when the file is absent or carries no (valid) receipt — an unbound sidecar keeps the legacy behaviour.
    A receipt naming another lineage refuses, and so does any receipt when this database has no lineage yet.
    Model and row-count mismatches are not write refusals: a rebuild repairs those.
    """
    receipt = read_receipt(sidecar_path)
    if receipt is None:
        return None
    db_lineage = lineage_id(database_path)
    if db_lineage != receipt.lineage_id:
        return (f"its generation receipt names database lineage {receipt.lineage_id[:8]}…, "
                f"this database is {(db_lineage or 'unset')[:8]}…")
    return None


def resolve_write_sidecar(database_path: Path, sidecar_path: Path | None) -> Path:
    """The sidecar a writer should use when the caller named none — chosen by ownership, not working directory.

    An explicit *sidecar_path* is returned unchanged (the writer still guards it with `foreign_binding`). Otherwise
    the legacy default (`LEDGER_VECTORS_PATH`, else the vectors file beside the database) is kept unless it belongs to
    another database; then the write refuses.
    """
    if sidecar_path is not None:
        return sidecar_path
    legacy = default_vectors_path(database_path)
    if foreign_binding(database_path, legacy) is None:
        return legacy
    raise SidecarOwnershipError(
        f"no semantic sidecar belongs to {database_path}: {legacy} is bound to another database. Pass this database's own sidecar explicitly "
        f"(--sidecar / sidecar_path=) or set LEDGER_VECTORS_PATH."
    )
