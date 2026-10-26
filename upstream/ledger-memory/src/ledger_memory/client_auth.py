from __future__ import annotations

"""Device pairing + token auth for the ledger client API.

A one-time, short-lived PAIRING CODE mints a long-lived DEVICE TOKEN that gates the
an earlier fix client API as defense-in-depth behind the owner's Teleport VPN. Credential
discipline mirrors the chat bot token (``telegram_bot.py``): the raw device token is
returned to the caller exactly ONCE (at pairing) and is stored NOWHERE — not in the db,
not in an export, not in a log. Only its SHA-256 hash is persisted (``client_devices``
has no raw-token column by construction). Revocation invalidates a token; the token does
not expire on a timer (the pairing CODE does).

an earlier fix's server calls ``verify_device_token`` (a None return is its 401) and serves the
``POST /api/pair`` route that calls ``redeem_pairing_code``. The pending pairing code lives in
the ``pairing_codes`` operational table — a third table class beside
authoritative/derived, EXCLUDED from exports — so a code minted by the CLI ``client-pair`` on one
process is redeemable by the long-running server on another. Only the SHA-256 hash of the code is
stored (it too is a short-lived secret); rows are single-use (deleted on redeem) and lazily
evicted once expired.
"""

import hashlib
import json
import math
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from ledger_memory.db import connect
from ledger_memory.emission import emit_audit_and_event

DEFAULT_PAIRING_TTL_MINUTES = 10
TOKEN_BYTES = 32

# Unambiguous alphabet — no 0/O, 1/I/L — so a code is easy to read off a screen and type.
_CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
_CODE_LENGTH = 8

ALL_CLIENT_CAPABILITIES = frozenset(
    {
        "read.status",
        "read.dashboard",
        "read.projects",
        "read.graph",
        "chat",
        "voice",
        "events",
        "ingest",
        "approvals",
        "control.agents",
        "control.egress",
        "owner.devices",
        # gates POST /api/prewarm — a device that may warm the resident model.
        # Interactive-only by the ceiling below (a node has no business shaping residency).
        "prewarm",
    }
)
DEVICE_CLASS_CAPABILITY_CEILINGS = {
    "interactive": ALL_CLIENT_CAPABILITIES,
    "node": frozenset(
        capability
        for capability in ALL_CLIENT_CAPABILITIES
        if capability.startswith("read.") or capability in {"ingest", "events"}
    ),
}
LEGACY_CLIENT_CAPABILITIES = frozenset({"chat", "read.dashboard", "read.status"})

def _now() -> float:
    """Monotonic-ish wall clock; a seam so tests can advance it past an expiry."""
    return time.time()


@dataclass(frozen=True, kw_only=True)
class ClientDevice:
    """A paired device. Deliberately carries NO token/token_hash field — listing must
    never surface token material."""

    id: int
    name: str
    created_at: str
    last_seen_at: str | None
    revoked_at: str | None
    device_class: str
    platform: str | None
    capabilities: frozenset[str]
    app_version: str | None


@dataclass(frozen=True, kw_only=True)
class PairingResult:
    """The ONLY object that ever carries the raw token — returned once, persisted never."""

    device_id: int
    name: str
    token: str
    created_at: str
    device_class: str
    platform: str | None
    capabilities: frozenset[str]


@dataclass(frozen=True, kw_only=True)
class ClientPrincipal:
    device_id: int | None
    device_class: str
    capabilities: frozenset[str]
    synthetic: bool = False

    @classmethod
    def synthetic_loopback(cls) -> "ClientPrincipal":
        return cls(
            device_id=None,
            device_class="interactive",
            capabilities=ALL_CLIENT_CAPABILITIES - {"owner.devices"},
            synthetic=True,
        )


class ClientAuthError(Exception):
    """Base for client-auth errors."""


class PairingError(ClientAuthError):
    """An unknown, expired, or already-used pairing code."""


class OwnershipUnavailable(ClientAuthError):
    """Ownership cannot be determined until the authenticated chokepoint (PA-3b)
    exists; raising is deliberate, because a boolean would be believed, and a
    False invites a caller to treat "unknown" as "authoritatively not the owner"."""


class DeviceNotFoundError(ClientAuthError):
    """An unknown or already-revoked device id."""


class PresenceRefreshRefused(ClientAuthError):
    """C2 — raised by `record_activity` when its OWN atomic conditional UPDATE matched
    ZERO rows, despite `verify_device_token` having verified the same device moments earlier in the
    same call. `state` distinguishes `"revoked"` (the device existed and verified at read time, but
    THIS transaction's write found `revoked_at` already set — the TOCTOU race the previous
    implementation silently audited as success) from `"not_found"` (the row is gone outright —
    unreachable today, since `client_devices` rows are never deleted, but representable rather than
    silently conflated with "revoked" the moment that ever changes). Distinct from the base
    `ClientAuthError` raised when the token itself never verifies at all, so a caller — and a test —
    can tell "no credential" apart from "credential verified, but the write lost a race"."""

    def __init__(self, *, device_id: int, state: str, message: str) -> None:
        super().__init__(message)
        self.device_id = device_id
        self.state = state


def hash_token(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def generate_pairing_code() -> str:
    return "".join(secrets.choice(_CODE_ALPHABET) for _ in range(_CODE_LENGTH))


def validate_capability_grant(
    *, device_class: str, capabilities: tuple[str, ...] | list[str] | frozenset[str]
) -> frozenset[str]:
    ceiling = DEVICE_CLASS_CAPABILITY_CEILINGS.get(device_class)
    if ceiling is None:
        raise ValueError(f"unknown device_class: {device_class}")
    values = tuple(capabilities)
    if len(values) != len(set(values)):
        raise ValueError("capabilities must not contain duplicates")
    grant = frozenset(values)
    unknown = grant - ALL_CLIENT_CAPABILITIES
    if unknown:
        raise ValueError(f"unknown capabilities: {','.join(sorted(unknown))}")
    above_ceiling = grant - ceiling
    if above_ceiling:
        raise ValueError(
            f"capabilities exceed {device_class} ceiling: {','.join(sorted(above_ceiling))}"
        )
    return grant


def _capabilities_json(capabilities: frozenset[str]) -> str:
    return json.dumps(sorted(capabilities), separators=(",", ":"))


def _parse_capabilities(raw: str, *, device_class: str) -> frozenset[str]:
    try:
        value = json.loads(raw)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("stored capabilities must be a JSON array") from exc
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError("stored capabilities must be a JSON string array")
    return validate_capability_grant(device_class=device_class, capabilities=tuple(value))


def start_pairing(
    database_path: Path,
    *,
    name: str,
    ttl_minutes: float = DEFAULT_PAIRING_TTL_MINUTES,
    device_class: str = "interactive",
    platform: str | None = None,
    capabilities: tuple[str, ...] | list[str] | frozenset[str] = LEGACY_CLIENT_CAPABILITIES,
) -> str:
    """Mint a short-lived single-use pairing code into the cross-process ``pairing_codes``
    store (only its hash is persisted). Returns the raw code (to print once). Lazily evicts
    expired codes on the way in. Rejects a non-positive/non-finite ttl: a negative ttl mints an
    already-expired code, a NaN ttl an un-redeemable/un-evictable zombie row, an infinite ttl a never-expiring code."""
    if not (math.isfinite(ttl_minutes) and ttl_minutes > 0):
        raise ValueError(f"ttl_minutes must be a positive, finite number of minutes, got {ttl_minutes!r}")
    if platform is not None and not isinstance(platform, str):
        raise ValueError("platform must be a string or None")
    grant = validate_capability_grant(device_class=device_class, capabilities=capabilities)
    code = generate_pairing_code()
    now = _now()
    with connect(database_path) as connection:
        connection.execute("DELETE FROM pairing_codes WHERE expires_at <= ?", (now,))
        connection.execute(
            "INSERT INTO pairing_codes "
            "(code_hash, name, expires_at, device_class, platform, capabilities) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                hash_token(code),
                name,
                now + ttl_minutes * 60.0,
                device_class,
                platform,
                _capabilities_json(grant),
            ),
        )
    return code


def redeem_pairing_code(database_path: Path, *, code: str, actor_id: int) -> PairingResult:
    """Verify an unexpired, unused code; consume it; mint a device token; record the device.

    Single-use under concurrency: the code row is DELETEd inside the same transaction as the
    device INSERT, so two racing redeems cannot both succeed. Raises PairingError (a uniform
    error — no oracle for which codes exist) if the code is unknown, expired, or already used.
    On success the raw token is in the return value and NOWHERE else.
    """
    now = _now()
    code_hash = hash_token(code)
    device_id: int | None = None
    raw = ""
    created_at = ""
    consumed_name: str | None = None
    with connect(database_path) as connection:
        connection.execute("DELETE FROM pairing_codes WHERE expires_at <= ?", (now,))  # lazy evict
        row = connection.execute(
            "SELECT name, device_class, platform, capabilities FROM pairing_codes "
            "WHERE code_hash = ? AND expires_at > ?",
            (code_hash, now),
        ).fetchone()
        if row is not None:
            deleted = connection.execute(
                "DELETE FROM pairing_codes WHERE code_hash = ?", (code_hash,)
            )
            if deleted.rowcount == 1:  # we won the consume race
                consumed_name = row["name"]
                consumed_device_class = row["device_class"]
                consumed_platform = row["platform"]
                consumed_capabilities = _parse_capabilities(
                    row["capabilities"], device_class=consumed_device_class
                )
        if consumed_name is not None:
            raw = secrets.token_urlsafe(TOKEN_BYTES)
            cursor = connection.execute(
                "INSERT INTO client_devices "
                "(name, token_hash, device_class, platform, capabilities) VALUES (?, ?, ?, ?, ?)",
                (
                    consumed_name,
                    hash_token(raw),
                    consumed_device_class,
                    consumed_platform,
                    _capabilities_json(consumed_capabilities),
                ),
            )
            device_id = int(cursor.lastrowid)
            created_at = connection.execute(
                "SELECT created_at FROM client_devices WHERE id = ?", (device_id,)
            ).fetchone()["created_at"]
            emit_audit_and_event(
                connection,
                action_type="client_device.paired",
                target_type="client_device",
                target_id=device_id,
                actor_id=actor_id,
                reason=None,
                event_type="client_device.paired",
                summary=f"client device paired: {consumed_name}",
            )
    if consumed_name is None:
        raise PairingError("pairing code is invalid, expired, or already used")
    return PairingResult(
        device_id=device_id,
        name=consumed_name,
        token=raw,
        created_at=created_at,
        device_class=consumed_device_class,
        platform=consumed_platform,
        capabilities=consumed_capabilities,
    )


def _row_to_device(row) -> ClientDevice:
    return ClientDevice(
        id=row["id"], name=row["name"], created_at=row["created_at"],
        last_seen_at=row["last_seen_at"], revoked_at=row["revoked_at"],
        device_class=row["device_class"], platform=row["platform"],
        capabilities=_parse_capabilities(row["capabilities"], device_class=row["device_class"]),
        app_version=row["app_version"],
    )


def verify_device_token(database_path: Path, raw_token: str) -> ClientDevice | None:
    """Return the device for a valid, non-revoked token, else None (the API's 401).

    Positional ``raw_token`` matches the server's seam call.
    """
    if not raw_token:
        return None
    with connect(database_path) as connection:
        row = connection.execute(
            "SELECT id, name, created_at, last_seen_at, revoked_at, device_class, platform, "
            "capabilities, app_version "
            "FROM client_devices WHERE token_hash = ?",
            (hash_token(raw_token),),
        ).fetchone()
    if row is None or row["revoked_at"] is not None:
        return None
    try:
        return _row_to_device(row)
    except ValueError:
        return None


def resolve_principal(database_path: Path, *, raw_token: str) -> ClientPrincipal | None:
    device = verify_device_token(database_path, raw_token)
    if device is None:
        return None
    return ClientPrincipal(
        device_id=device.id,
        device_class=device.device_class,
        capabilities=device.capabilities,
    )


def touch_device(database_path: Path, *, device_id: int) -> None:
    """Hot-path liveness — update last_seen_at. No emission (not an audited action)."""
    with connect(database_path) as connection:
        connection.execute(
            "UPDATE client_devices SET last_seen_at = CURRENT_TIMESTAMP WHERE id = ?", (device_id,)
        )


def record_activity(database_path: Path, *, raw_token: str) -> int:
    """PRESENCE evidence — an authenticated action by this principal.

    **Takes a credential, not a device id.** The first version took `device_id: int`, which meant any
    caller holding an integer could refresh any device's presence, including the owner's, without
    holding its token. Its docstring said "an authenticated action"; its signature
    accepted an integer. Presence a caller can assert for someone else is not presence.

    Deliberately NOT the same thing as ``touch_device``, which sets ``last_seen_at``. That is
    LIVENESS: any process can emit it, and it proves only that a process is running. This sets
    ``last_activity_at``, which the presence clamp reads as evidence that a PERSON is there. If a
    heartbeat could write this column, a walked-away-from device would hold owner-sensitive access
    open indefinitely.

    The refresh is a write, so it is audited like everything else on this path (C4.3).

    Returns the device id whose presence was refreshed. Raises ``ClientAuthError`` if the token does
    not verify or the device is revoked — never silently no-ops, because a caller that believes it
    refreshed presence and did not would read a stale clamp as a policy decision.
    """
    device = verify_device_token(database_path, raw_token)
    if device is None:
        raise ClientAuthError("presence refresh refused: the credential did not verify")
    with connect(database_path) as connection:
        # C2 (mandatory, Amendment v2.1's own additional note): the outcome is derived from
        # THIS ONE atomic conditional UPDATE's `rowcount`, checked and emitted in the SAME transaction —
        # never from a separate, earlier read. `verify_device_token` above already confirmed the device
        # was not revoked, but that read happened in ITS OWN transaction, moments before this one opens;
        # a revoke landing in that window is exactly the TOCTOU race the previous implementation missed,
        # because it emitted a success audit and returned `device.id` unconditionally, regardless of
        # whether this UPDATE actually matched anything — a durable FALSE record.
        cursor = connection.execute(
            "UPDATE client_devices SET last_activity_at = CURRENT_TIMESTAMP WHERE id = ? "
            "AND revoked_at IS NULL",
            (device.id,),
        )
        if cursor.rowcount == 0:
            # A follow-up SELECT here is NOT a second, independent check — it is inside the SAME open
            # transaction as the UPDATE above (no commit between them), so it can only observe what this
            # connection's own write already made true; nothing else can revoke/delete the row in
            # between (that is precisely what "same transaction" buys, and precisely what the Amendment
            # note requires: comparing rowcount and emitting the outcome in the same transaction, not a
            # separate check that could itself race). Used ONLY to distinguish the outcome for the
            # caller — the decision NOT to write already happened, atomically, in the UPDATE itself.
            row = connection.execute(
                "SELECT 1 FROM client_devices WHERE id = ?", (device.id,)
            ).fetchone()
            state = "revoked" if row is not None else "not_found"
            raise PresenceRefreshRefused(
                device_id=device.id, state=state,
                message=f"presence refresh refused: device {device.id} is {state} (0 rows updated)",
            )
        # No success audit fires above this line when the update matched nothing — the previous
        # behaviour's exact defect.
        emit_audit_and_event(
            connection,
            action_type="client_device.presence_refreshed",
            target_type="client_device",
            target_id=device.id,
            actor_id=None,
            reason=None,
            event_type="client_device.presence_refreshed",
            summary=f"presence refreshed for device {device.id}",
        )
    return device.id


def list_devices(database_path: Path) -> list[ClientDevice]:
    """All devices, newest id last. Never selects token_hash into a listable object."""
    with connect(database_path) as connection:
        rows = connection.execute(
            "SELECT id, name, created_at, last_seen_at, revoked_at, device_class, platform, "
            "capabilities, app_version FROM client_devices ORDER BY id"
        ).fetchall()
    return [_row_to_device(r) for r in rows]


def revoke_device(database_path: Path, *, device_id: int, actor_id: int) -> ClientDevice:
    """Revoke a device (its token stops verifying). Raises DeviceNotFoundError on an
    unknown or already-revoked id. Emits an owner-attributed audit-and-event."""
    with connect(database_path) as connection:
        cursor = connection.execute(
            "UPDATE client_devices SET revoked_at = CURRENT_TIMESTAMP "
            "WHERE id = ? AND revoked_at IS NULL",
            (device_id,),
        )
        if cursor.rowcount == 0:
            raise DeviceNotFoundError(f"device not found or already revoked: {device_id}")
        emit_audit_and_event(
            connection,
            action_type="client_device.revoked",
            target_type="client_device",
            target_id=device_id,
            actor_id=actor_id,
            reason=None,
            event_type="client_device.revoked",
            summary=f"client device revoked: {device_id}",
        )
        row = connection.execute(
            "SELECT id, name, created_at, last_seen_at, revoked_at, device_class, platform, "
            "capabilities, app_version FROM client_devices WHERE id = ?",
            (device_id,),
        ).fetchone()
    return _row_to_device(row)


class TierChangeRefused(ClientAuthError):
    """A promote/demote was refused — the message names the exact reason."""


def promote_device_tier(database_path: Path, *, device_id: int, actor_id: int | None, reason: str) -> None:
    """grant one device durable ELIGIBILITY TO MINT T1 proofs by setting
    `tier_ceiling = 1`. This is the mint gate at `/api/chat` — NOT the read ceiling, which stays with
    the runtime funnel principal; every read still consumes a single-use per-request proof
. Durable until `demote_device_tier`.

    The C2 pattern (see `record_activity`): one atomic conditional UPDATE, outcome derived from ITS
    rowcount, audit emitted in the SAME transaction — no write without the audit row, no audit row
    without the write. No device-class guard: the runtime funnel device is already T1 from
    `runtime-pair` and lands in the already-at-T1 refusal naturally.
    """
    if not reason or not reason.strip():
        raise TierChangeRefused("a non-empty --reason is required: a tier grant without its reasoning is unauditable")
    with connect(database_path) as connection:
        cursor = connection.execute(
            "UPDATE client_devices SET tier_ceiling = 1 "
            "WHERE id = ? AND revoked_at IS NULL AND tier_ceiling != 1",
            (device_id,),
        )
        if cursor.rowcount == 0:
            row = connection.execute(
                "SELECT revoked_at, tier_ceiling FROM client_devices WHERE id = ?", (device_id,)
            ).fetchone()
            if row is None:
                raise TierChangeRefused(f"promote refused: device not found: {device_id}")
            if row["revoked_at"] is not None:
                raise TierChangeRefused(f"promote refused: device {device_id} is revoked — a dead credential must not gain reach")
            raise TierChangeRefused(f"promote refused: device {device_id} is already at T1")
        emit_audit_and_event(
            connection,
            action_type="device.tier_promoted",
            target_type="client_device",
            target_id=device_id,
            actor_id=actor_id,
            reason=reason,
            event_type="device.tier_promoted",
            summary=f"device {device_id} promoted: tier_ceiling 3 -> 1 (reason in audit)",  # an earlier fix
        )


def demote_device_tier(
    database_path: Path, *, device_id: int, actor_id: int | None, reason: str,
) -> None:
    """the deliberate revoke — `tier_ceiling` back to 3. THE guard lives here: if a
    runtime credential resolves and names
    this device, demotion is refused — demoting the funnel principal clamps EVERY owner read, and
    that is `runtime-pair` tooling's decision, not this command's. A revoked device may be demoted
    (cleaning up a lost phone's grant must never be blocked).
    """
    if not reason or not reason.strip():
        raise TierChangeRefused("a non-empty --reason is required: a tier change without its reasoning is unauditable")
    from ledger_memory.reader import read_reader_token

    runtime_token = read_reader_token(database_path)
    if runtime_token:
        runtime_device = verify_device_token(database_path, runtime_token)
        if runtime_device is not None and runtime_device.id == device_id:
            raise TierChangeRefused(
                f"demote refused: device {device_id} is the runtime funnel principal — demoting it "
                "clamps every owner read; manage it with runtime-pair tooling instead"
            )
    with connect(database_path) as connection:
        cursor = connection.execute(
            "UPDATE client_devices SET tier_ceiling = 3 WHERE id = ? AND tier_ceiling != 3",
            (device_id,),
        )
        if cursor.rowcount == 0:
            row = connection.execute(
                "SELECT tier_ceiling FROM client_devices WHERE id = ?", (device_id,)
            ).fetchone()
            if row is None:
                raise TierChangeRefused(f"demote refused: device not found: {device_id}")
            raise TierChangeRefused(f"demote refused: device {device_id} is already at tier 3")
        emit_audit_and_event(
            connection,
            action_type="device.tier_demoted",
            target_type="client_device",
            target_id=device_id,
            actor_id=actor_id,
            reason=reason,
            event_type="device.tier_demoted",
            summary=f"device {device_id} demoted: tier_ceiling -> 3 (reason in audit)",  # an earlier fix
        )


def device_tier_ceilings(database_path: Path) -> dict[int, int]:
    """id -> tier_ceiling for display surfaces. A dedicated read instead of a
    `ClientDevice` field — originally because adding the attribute would have woken the dead
    `hasattr` branch in `v1_service._get_principal_ceiling`; an earlier fix has since RETIRED that
    branch (the function is an honest constant now), and this stays a dedicated read on its own
    merits: listing surfaces need ids-to-ceilings, not a wider `ClientDevice`."""
    with connect(database_path) as connection:
        rows = connection.execute("SELECT id, tier_ceiling FROM client_devices").fetchall()
    return {int(r["id"]): int(r["tier_ceiling"]) for r in rows}


def is_owner(database_path: Path, principal: ClientPrincipal) -> bool:
    """FAIL-CLOSED STUB. PA-3a is schema runway only; the authenticated chokepoint
    (PA-3b) does not exist yet. A forged-row probe (tier_ceiling=-1, principal_kind='banana',
    subject_actor_id=99999) caused the previous implementation to return True because it
    trusted principal.device_id without re-resolving a credential in this database,
    without binding the principal to the database it was authenticated against, and
    without verifying the subject holds the store-scoped owner role. Returning False
    would be just as wrong — a caller would read it as "authoritatively not the owner"
    when the truth is "this cannot be determined yet". Identity is not authorization
    even once implemented."""
    raise OwnershipUnavailable(
        "ownership cannot be determined: missing a credential re-resolved in this database, "
        "an authenticated context that cannot be minted from an id, and verification that "
        "the subject holds the store-scoped owner role (PA-3b not implemented)"
    )


def refresh_runtime_presence(database_path: Path) -> int | None:
    """Refresh the local reader's presence (``last_activity_at``) in this database. Fail-soft: no reader, a reader that
    no longer verifies, or any other failure returns None rather than raising."""
    from ledger_memory.reader import read_reader_token

    token = read_reader_token(database_path)
    if not token:
        return None
    try:
        return record_activity(database_path, raw_token=token)
    except Exception:
        return None
