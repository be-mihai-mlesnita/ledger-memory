"""The local reader: the one credential this package reads memory with.

The package has one user and one local agent. On first start it creates ONE reader device at tier ceiling 1 in
the package's own database; its raw token lives in ``<home>/reader.token`` (mode 0600; on Windows the user
profile's ACL) and is NEVER printed, logged or returned by any tool. Every read re-verifies the token against the
database (revocation takes effect on the next call) and refreshes the reader's presence first, so records stored at
tier 1 are visible. No record's tier is ever rewritten.
"""
from __future__ import annotations

import os
import json
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path

from ledger_memory.authenticated_context import AuthenticatedContext, authenticate
from ledger_memory.client_auth import hash_token, record_activity, verify_device_token
from ledger_memory.db import connect, connect_readonly
from ledger_memory.emission import emit_audit_and_event
from ledger_memory.locking import exclusive
from ledger_memory.presence import DEFAULT_IDLE_SECONDS, is_present

READER_NAME = "local-reader"
READER_TOKEN_FILE = "reader.token"
READER_TIER_CEILING = 1
_CAPABILITIES = '["read.status"]'


class ReaderError(RuntimeError):
    """The reader could not be made available; the message never contains the token."""


def token_path(database_path: Path | str) -> Path:
    """The token file lives beside the database, so it follows the database wherever LEDGER_HOME points."""
    return Path(database_path).with_name(READER_TOKEN_FILE)


def _reader_creation_summary(device_id: int) -> str:
    return f"local reader created (device {device_id})"


def read_reader_token(database_path: Path | str) -> str | None:
    """The raw token from the token file, or None when absent/empty/unreadable. Never logs."""
    try:
        raw = token_path(database_path).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return raw or None


def _write_private(path: Path, text: str) -> None:
    """Atomic, owner-only write: a temp file created 0600 from the start, then renamed over the target."""
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
        try:
            os.chmod(tmp, 0o600)          # best effort on platforms where the mode is advisory
        except OSError:
            pass
        deadline = time.monotonic() + 1.0
        failure = ("reader credential replacement unavailable; existing credential retained; "
                   "close applications holding the credential file and retry")
        for attempt in range(6):
            if attempt and time.monotonic() >= deadline:
                raise ReaderError(failure) from None
            try:
                os.replace(tmp, path)
                break
            except OSError as exc:
                remaining = deadline - time.monotonic()
                if getattr(exc, "winerror", None) not in (32, 33) or attempt == 5 or remaining <= 0:
                    raise ReaderError(failure) from None
                time.sleep(min(0.02 * (2 ** attempt), 0.2, remaining))
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def lock_path(database_path: Path | str) -> Path:
    return Path(database_path).with_name(".ledger.lock")


def remove_stale_temp_files(database_path: Path | str, *, older_than_seconds: int = 3600) -> int:
    """Delete ``.reader.token.*.tmp`` files left behind by a killed process (older than an hour)."""
    removed = 0
    cutoff = time.time() - older_than_seconds
    for f in Path(database_path).parent.glob(f".{READER_TOKEN_FILE}.*.tmp"):
        try:
            if f.stat().st_mtime < cutoff:
                f.unlink()
                removed += 1
        except OSError:
            pass
    return removed


def ensure_reader(database_path: Path | str, *, owner_actor_id: int | None = None) -> None:
    """Create the local reader if it does not exist (or no longer verifies). Idempotent and cheap when it does."""
    database_path = Path(database_path)
    existing = read_reader_token(database_path)
    if existing and verify_device_token(database_path, existing) is not None:
        prune_revoked_readers(database_path, owner_actor_id=owner_actor_id)
        return
    with exclusive(lock_path(database_path)):        # two first starts must end with exactly one active reader
        existing = read_reader_token(database_path)
        if existing and verify_device_token(database_path, existing) is not None:
            prune_revoked_readers(database_path, owner_actor_id=owner_actor_id)
            return
        remove_stale_temp_files(database_path)
        _create_reader(database_path, owner_actor_id)
        prune_revoked_readers(database_path, owner_actor_id=owner_actor_id)


def prune_revoked_readers(database_path: Path | str, *, owner_actor_id: int | None = None) -> int:
    """Archive nonsecret identity before deleting only unreferenced revoked local readers."""
    database_path = Path(database_path)
    if not os.access(database_path, os.W_OK):
        return 0
    selection = ("SELECT id,name,created_at,revoked_at FROM client_devices WHERE name=? AND revoked_at IS NOT NULL "
                 "AND device_class='interactive' AND principal_kind='device' AND capabilities=? AND tier_ceiling=?")
    parameters = (READER_NAME, _CAPABILITIES, READER_TIER_CEILING)
    with connect_readonly(database_path) as connection:
        if connection.execute(selection + " LIMIT 1", parameters).fetchone() is None:
            return 0

    def quote(identifier):
        return '"' + identifier.replace('"', '""') + '"'

    removed = 0
    try:
        with connect(database_path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            references = []
            tables = connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            for table in tables:
                groups = {}
                for foreign_key in connection.execute(f"PRAGMA foreign_key_list({quote(table['name'])})"):
                    if str(foreign_key["table"]).casefold() == "client_devices":
                        groups.setdefault(foreign_key["id"], []).append(
                            (foreign_key["from"], foreign_key["to"] or "id"))
                references.extend((table["name"], columns) for columns in groups.values())
            for device in connection.execute(selection, parameters).fetchall():
                origin = connection.execute(
                    "SELECT 1 FROM audit_entries a JOIN events e ON e.id=a.event_id "
                    "WHERE a.action_type='client_device.paired' AND a.target_type='client_device' "
                    "AND a.target_id=? AND e.event_type='client_device.paired' AND e.summary=? LIMIT 1",
                    (device["id"], _reader_creation_summary(device["id"]))).fetchone()
                if origin is None:
                    continue  # Matching names/policy alone do not prove this is our local credential.
                referenced = False
                for table, columns in references:
                    # Parent affinity/collation governs FK equality, not the child's declaration.
                    where = " AND ".join(f"parent.{quote(target)}=+child.{quote(column)}" for column, target in columns)
                    if connection.execute(
                            f"SELECT 1 FROM {quote(table)} AS child JOIN client_devices AS parent ON {where} "
                            "WHERE parent.id=? LIMIT 1", (device["id"],)).fetchone():
                        referenced = True
                        break
                if referenced:
                    continue
                metadata = {key: device[key] for key in ("id", "name", "created_at", "revoked_at")}
                emit_audit_and_event(connection, action_type="client_device.pruned", target_type="client_device",
                    target_id=device["id"], actor_id=owner_actor_id, reason="revoked unreferenced local reader",
                    event_type="client_device.pruned", summary="revoked local reader identity archived",
                    metadata=json.dumps(metadata, sort_keys=True))
                connection.execute("DELETE FROM client_devices WHERE id=? AND revoked_at IS NOT NULL", (device["id"],))
                removed += 1
    except Exception:
        raise ReaderError("revoked local-reader cleanup unavailable; historical audit/read rows were not rewritten") from None
    return removed


def _create_reader(database_path: Path, owner_actor_id: int | None) -> None:
    raw = secrets.token_urlsafe(32)
    with connect(database_path) as connection:
        cursor = connection.execute(
            "INSERT INTO client_devices (name, token_hash, device_class, capabilities, tier_ceiling) "
            "VALUES (?, ?, 'interactive', ?, ?)",
            (READER_NAME, hash_token(raw), _CAPABILITIES, READER_TIER_CEILING),
        )
        device_id = int(cursor.lastrowid)
        emit_audit_and_event(
            connection, action_type="client_device.paired", target_type="client_device", target_id=device_id,
            actor_id=owner_actor_id, reason=None, event_type="client_device.paired",
            summary=_reader_creation_summary(device_id),
        )
    try:
        _write_private(token_path(database_path), raw)
    except BaseException:
        # A failed file publication must not leave another active credential behind.
        try:
            with connect(database_path) as connection:
                connection.execute("UPDATE client_devices SET revoked_at=CURRENT_TIMESTAMP WHERE id=?", (device_id,))
                emit_audit_and_event(connection, action_type="client_device.revoked", target_type="client_device",
                    target_id=device_id, actor_id=owner_actor_id, reason="reader credential publication failed",
                    event_type="client_device.revoked", summary="unpublished local reader revoked")
        except Exception:
            raise ReaderError("reader publication failed; unpublished reader revocation unavailable; "
                              "existing credential retained; inspect local reader status before retrying") from None
        raise
    # A second process that raced us may have created its own reader; keep only the one the file names.
    current = read_reader_token(database_path) or raw
    with connect(database_path) as connection:
        connection.execute(
            "UPDATE client_devices SET revoked_at = CURRENT_TIMESTAMP "
            "WHERE name = ? AND revoked_at IS NULL AND token_hash != ?",
            (READER_NAME, hash_token(current)),
        )


def reader_context(database_path: Path | str) -> AuthenticatedContext | None:
    """An authenticated context for the local reader, or None when no reader credential verifies."""
    database_path = Path(database_path)
    token = read_reader_token(database_path)
    if not token:
        return None
    return authenticate(database_path, raw_token=token)


def _presence_is_fresh(database_path: Path, token: str) -> bool:
    """True when the reader's last activity is younger than HALF the presence window (the window `presence.is_present`
    uses), so it cannot lapse before the read that follows. Otherwise the caller refreshes first."""
    with connect(database_path) as connection:
        row = connection.execute(
            "SELECT last_activity_at FROM client_devices WHERE token_hash = ? AND revoked_at IS NULL",
            (hash_token(token),),
        ).fetchone()
    if row is None:
        return False
    return is_present(row["last_activity_at"], datetime.now(timezone.utc).isoformat(),
                      idle_seconds=DEFAULT_IDLE_SECONDS // 2)


def begin_read(database_path: Path | str, *, owner_actor_id: int | None = None) -> AuthenticatedContext:
    """The start of every read: make sure the reader exists, refresh its presence, return its context.

    Presence is evidence that the person is there; without it the presence clamp would hide tier-1 records. An
    unverifiable reader is a refusal, never a silent fallback to an unclamped read."""
    database_path = Path(database_path)
    ensure_reader(database_path, owner_actor_id=owner_actor_id)
    token = read_reader_token(database_path)
    if not token:
        raise ReaderError("the local reader credential is missing; run `ledger init`")
    for attempt in (0, 1):
        if attempt == 0 and _presence_is_fresh(database_path, token):
            break                                  # recent enough: no new audit/event rows for every read
        try:
            record_activity(database_path, raw_token=token)
            break
        except Exception:
            if attempt:
                raise ReaderError("the local reader credential no longer verifies; run `ledger init`") from None
            ensure_reader(database_path, owner_actor_id=owner_actor_id)
            token = read_reader_token(database_path) or token
            time.sleep(0.01)
    context = authenticate(database_path, raw_token=token)
    if context is None:
        raise ReaderError("the local reader credential no longer verifies; run `ledger init`")
    return context


def reader_file_is_private(database_path: Path | str) -> bool:
    """True when the token file exists and is not readable by group/others (always True where modes do not apply)."""
    path = token_path(database_path)
    if not path.is_file():
        return False
    if os.name == "nt":
        return True
    return (path.stat().st_mode & 0o077) == 0
