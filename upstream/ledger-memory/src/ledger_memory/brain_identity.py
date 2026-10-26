"""Database-derived store identity.

Incident link 1: the phone was paired to the DEV store and nothing on any screen said so.
a reviewer's finding: a process-supplied name can lie in exactly this failure mode — a "Personal"
profile pointed at the dev database still says Personal. Identity must derive from the opened
DATABASE itself, be pinned by the client at pairing, and be compared on every reconnect. This
module is the identity half: a durable, database-derived identity minted once and never rotated.

Deliberately independent of `semantic.lineage_id`: that
lineage id identifies the CORPUS a sidecar was embedded from and may be rebound by repair flows
. The store's own identity must survive those rebinds, so it
lives under its own config key and is never read or written by semantic_binding.py.
"""
from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from pathlib import Path

from ledger_memory.config import get_config
from ledger_memory.db import connect
from ledger_memory.emission import emit_audit_and_event

LINEAGE_KEY = "store.lineage_id"
DISPLAY_NAME_KEY = "store.display_name"
KIND_KEY = "store.kind"

DEFAULT_DISPLAY_NAME = "Unnamed store"
DEFAULT_KIND = "unspecified"


@dataclass(frozen=True)
class BrainIdentity:
    fingerprint: str
    display_name: str
    kind: str


def ensure_brain_lineage_id(database_path: Path, *, actor_id: int = 1) -> str:
    """Get-or-create the store's lineage id. Minted ONCE and never rotated — a copy/restore of
    the database keeps its identity, and repair flows that rebind `semantic.lineage_id` never
    touch this key.

    First-mint-wins under concurrency: the insert is a single ON CONFLICT DO NOTHING statement
    (set_config's DO UPDATE would let a second overlapping server silently overwrite the first
    mint), and the audited config.set is emitted only when THIS call actually minted.
    """
    reason = "mint durable store identity"
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


def brain_fingerprint(lineage_id: str) -> str:
    """Opaque, secret-free, prefix-stable fingerprint: first 12 hex of sha256(lineage_id).

    The raw lineage id never leaves the server — only this fingerprint is served.
    """
    return hashlib.sha256(lineage_id.encode("utf-8")).hexdigest()[:12]


def get_brain_identity(database_path: Path) -> BrainIdentity:
    """Read-only: the identity to serve. Never mints — callers that need a lineage id to exist
    must call `ensure_brain_lineage_id` first (the one write, done at server startup).
    """
    lineage_id = get_config(database_path, LINEAGE_KEY)
    fingerprint = brain_fingerprint(lineage_id) if lineage_id is not None else ""
    display_name = get_config(database_path, DISPLAY_NAME_KEY, DEFAULT_DISPLAY_NAME)
    kind = get_config(database_path, KIND_KEY, DEFAULT_KIND)
    return BrainIdentity(fingerprint=fingerprint, display_name=display_name, kind=kind)
