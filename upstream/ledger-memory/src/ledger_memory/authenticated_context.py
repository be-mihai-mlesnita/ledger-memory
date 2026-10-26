"""an earlier fix / PA-3b — a read context whose authority is a CREDENTIAL, not a Python object.

## The threat boundary, stated explicitly

**In-process Python is NOT a boundary this module can defend, and it does not claim to.** Module
privacy, `frozen=True`, and private sentinels are conventions the interpreter offers for correctness,
not authority. Anything running in this process can reach `_MINT`, call `object.__new__`, and
`object.__setattr__` past a frozen dataclass. An earlier version of this module treated a private
sentinel as credential proof; review defeated it three ways in under a minute, including the worst one:
a caller holding a *legitimate* low-tier context could mutate its own `device_id` to the owner's device
and read T1 content, without ever holding the owner's token.

So the design changed rather than the sentinel getting stronger. **The context carries the bearer
token, and `tiered_read` re-verifies that token against the database on every single call.** Authority
now comes from something the attacker must actually possess, not from the shape of an object. Mutating
`device_id` gains nothing: the device id is DERIVED from the token at verification time and the
mutated field is never read.

If untrusted Python must run alongside this code, that needs process isolation — no in-process
mechanism substitutes. This module's boundary is: **a caller who does not hold a valid device token
cannot read content, however freely it manipulates Python objects.**
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from ledger_memory.client_auth import ClientDevice, verify_device_token


@dataclass(frozen=True)
class AuthenticatedContext:
    """A database path plus the bearer credential, and deliberately nothing else.

    No ceiling, no capabilities, no device id as authority: every one of those is re-resolved from the
    database on each read, because a value carried on a session object is a value that cannot be
    revoked.

    `token` is compared by `repr=False` so a context landing in a log line or a traceback does not
    print the credential — the same discipline `PairingResult` follows.
    """

    database_path: Path
    token: str = field(repr=False)

    def resolve(self) -> ClientDevice | None:
        """Re-verify the credential in THIS database, right now. None if it no longer verifies.

        This is the whole security model in one method: not "was this object minted by us?" but "does
        this credential still verify against this database at this instant?" A revoked device returns
        None here, which is why revocation takes effect on the very next call rather than the next
        session.
        """
        return verify_device_token(Path(self.database_path), self.token)


def authenticate(database_path, *, raw_token: str) -> AuthenticatedContext | None:
    """Verify a credential and return a context bound to it, or None.

    None — never an exception — for an unknown, empty, or revoked credential: those are ordinary
    authentication outcomes, and the caller's job is to refuse, not to handle an error.

    Note that a caller CAN construct `AuthenticatedContext(path, token)` directly, and that is fine:
    doing so requires already holding a valid token, which is exactly the thing being protected. There
    is nothing left to forge. That is why the refusing `__init__` is gone — it was defending a property
    that no longer carries any weight.
    """
    if not raw_token:
        return None
    if verify_device_token(Path(database_path), raw_token) is None:
        return None
    return AuthenticatedContext(database_path=Path(database_path), token=raw_token)


def is_authentic(obj: object) -> bool:
    """True only if `obj` carries a credential that verifies RIGHT NOW against its own database.

    Deliberately not a type check. A hostile object that duck-types `database_path` and `token` is
    welcome to try: it will be refused unless the token it carries genuinely verifies, and if it does
    verify then the caller holds a real credential and is a legitimate principal.
    """
    database_path = getattr(obj, "database_path", None)
    token = getattr(obj, "token", None)
    if database_path is None or not isinstance(token, str) or not token:
        return False
    try:
        return verify_device_token(Path(database_path), token) is not None
    except Exception:
        # An unreadable or non-database path is not an authentic context. Fail closed.
        return False


__all__ = ["AuthenticatedContext", "authenticate", "is_authentic"]
