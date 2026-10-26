"""the migration catalog is a committed manifest, not a directory listing.

`_load_migrations` used to enumerate whatever `importlib.resources` exposed from the editable checkout,
so "available migrations" was a listing of a *working tree*. Any `.sql` file appearing there — committed or
not, finished or not — immediately changed every consumer database's pending set, and `_database_ready`
gates 177 call sites on that. Three of the last four migrations wedged the owner's store this way,
including one that existed only as an untracked file in a single checkout.

The lock makes availability a reviewed artifact. Two failure modes, graded differently on purpose:

* an **unlisted** `.sql` file in the directory is a hygiene problem — reported by `catalog_status`, and
  otherwise ignored. It must never become pending, because a developer with an in-progress migration
  getting their store wedged *is* the incident this exists to prevent.
* a **listed** file whose bytes do not match its recorded hash is an integrity problem — `load_catalog`
  fails closed. The catalog is describing something other than what was reviewed, and applying that
  unattended is the risk the whole arc exists to remove.

Scope of the guarantee, stated precisely because the stronger phrasings are false: **an unlisted SQL file
cannot alter availability.** This is NOT a claim that only committed migrations are visible, nor that an
editable checkout is immutable — an actor able to edit a migration can edit the lock beside it. Sealing
that would need a released artifact rather than a source tree.
"""
from __future__ import annotations

import hashlib
import re
from importlib import resources

LOCK_NAME = "MIGRATIONS.lock"

# `NNN_name.sql`, basename only. A path separator in a lock entry is a rejection, not a path to resolve.
_ENTRY_NAME = re.compile(r"^\d{3}_[A-Za-z0-9_.-]+\.sql$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class MigrationCatalogError(RuntimeError):
    """The catalog cannot be trusted: a malformed lock, or a listed file that is not what it claims.

    Never caught-and-ignored by callers. A catalog that silently falls back to scanning the directory
    would reintroduce exactly the defect the lock removes.
    """


def _root():
    return resources.files("ledger_memory.migrations")


def parse_lock(text: str) -> list[tuple[str, str, bool]]:
    """[(name, sha256, auto_apply)] in file order. Strict: every rejection below was a way to smuggle in a
    surprise.

    the optional third column is the unattended-application flag. **Absent means false.** A
    lock written before the flag existed therefore marks everything manual, which is the safe direction —
    a migration must be opted IN to unattended application, never opted out of it.
    """
    entries: list[tuple[str, str]] = []
    seen: set[str] = set()
    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) not in (2, 3):
            raise MigrationCatalogError(
                f"{LOCK_NAME}:{lineno}: expected '<name> <sha256> [auto_apply]', got {line!r}")
        name, digest = parts[0], parts[1]
        flag = parts[2] if len(parts) == 3 else "manual"
        if flag not in ("auto", "manual"):
            raise MigrationCatalogError(
                f"{LOCK_NAME}:{lineno}: third field must be 'auto' or 'manual', got {flag!r}")
        auto_apply = flag == "auto"
        if "/" in name or "\\" in name or not _ENTRY_NAME.match(name):
            raise MigrationCatalogError(f"{LOCK_NAME}:{lineno}: not a migration basename: {name!r}")
        if not _SHA256.match(digest):
            raise MigrationCatalogError(f"{LOCK_NAME}:{lineno}: not a sha256: {digest!r}")
        if name in seen:
            raise MigrationCatalogError(f"{LOCK_NAME}:{lineno}: duplicate entry {name!r}")
        seen.add(name)
        entries.append((name, digest, auto_apply))
    if not entries:
        raise MigrationCatalogError(f"{LOCK_NAME}: no entries — refusing to treat an empty catalog as valid")
    return entries


def _read_lock() -> list[tuple[str, str]]:
    lock = _root() / LOCK_NAME
    if not lock.is_file():
        raise MigrationCatalogError(f"{LOCK_NAME} missing from the migrations package")
    return parse_lock(lock.read_text(encoding="utf-8"))


def load_catalog() -> list[tuple[str, str]]:
    """[(name, sql)] for the listed migrations, IN MANIFEST ORDER, hash-verified.

    The directory is never re-sorted and never consulted for ordering — the manifest is the order.
    """
    root = _root()
    loaded: list[tuple[str, str]] = []
    for name, expected, _auto in _read_lock():
        entry = root / name
        if not entry.is_file():
            raise MigrationCatalogError(f"{LOCK_NAME} lists {name!r}, which is not present")
        data = entry.read_bytes()
        actual = hashlib.sha256(data).hexdigest()
        if actual != expected:
            raise MigrationCatalogError(
                f"migration catalog integrity failure: {name} hash {actual} != locked {expected}"
            )
        loaded.append((name, data.decode("utf-8")))
    return loaded


def catalog_status() -> tuple[str, list[str]]:
    """(status, names) for reporting. NEVER raises — a health line must not take the process down.

    `ok` | `unlisted:<n>` | `integrity-failure` | `unreadable`.
    """
    try:
        entries = _read_lock()
    except MigrationCatalogError as exc:
        return ("integrity-failure" if "integrity failure" in str(exc) else "unreadable"), [str(exc)]
    except Exception as exc:  # noqa: BLE001 — reporting must survive anything
        return "unreadable", [f"{type(exc).__name__}: {exc}"]

    listed = {name for name, _, _ in entries}
    try:
        root = _root()
        present = {p.name for p in root.iterdir() if p.name.endswith(".sql")}
    except Exception as exc:  # noqa: BLE001
        return "unreadable", [f"{type(exc).__name__}: {exc}"]

    bad: list[str] = []
    for name, expected, _auto in entries:
        try:
            entry = root / name
            if not entry.is_file():
                bad.append(f"{name}: listed but missing")
            elif hashlib.sha256(entry.read_bytes()).hexdigest() != expected:
                bad.append(f"{name}: hash mismatch")
        except Exception as exc:  # noqa: BLE001
            bad.append(f"{name}: {type(exc).__name__}")
    if bad:
        return "integrity-failure", bad

    # an `auto` entry that cannot be honoured must be VISIBLE, not silently downgraded.
    provenance, _reason = release_provenance_status()
    if provenance != "ok":
        stranded = [name for name, _d, auto in entries if auto]
        if stranded:
            return f"auto-without-provenance:{len(stranded)}", stranded

    unlisted = sorted(present - listed)
    if unlisted:
        # Hygiene, not integrity: reported so it cannot rot unseen, ignored so it cannot wedge anything.
        return f"unlisted:{len(unlisted)}", unlisted
    return "ok", []


def generate_lock() -> str:
    """The lock text for the migrations currently on disk. Called ONLY by `ledger migrations-lock --write`.

    Never invoked at runtime: a catalog that repairs itself silently records nothing and prevents nothing.
    """
    root = _root()
    names = sorted(p.name for p in root.iterdir() if p.name.endswith(".sql"))
    existing = {}
    try:
        existing = {n: a for n, _, a in _read_lock()}
    except Exception:  # noqa: BLE001 — first generation, or a lock being rebuilt from scratch
        pass
    lines = [
        "# ledger-memory migration catalog — order is authoritative, hashes are of raw bytes.",
        "# Third column: 'auto' = may be applied unattended; 'manual' (default) = never.",
    ]
    for name in names:
        digest = hashlib.sha256((root / name).read_bytes()).hexdigest()
        flag = "auto" if existing.get(name) else "manual"
        lines.append(f"{name}  {digest}  {flag}")
    return "\n".join(lines) + "\n"


def release_provenance_status() -> tuple[str, str]:
    """(status, reason) — may `auto` entries be honoured at all on this runtime?

    a reviewer's review of 2026-07-28 made a sealed/released runtime artifact — or a shared release-provenance
    gate on every auto-heal entry point — a **binding precondition** of the first migration ever marked
    `auto`. The remaining editable-install risk is that someone edits a listed migration *and* its lock
    together; the catalog does not claim to stop that, and a one-entry-point wrapper must not pretend to
.

    That precondition used to be a paragraph in a doc. It is now executable: until a mechanism exists and
    is named here, this returns `unavailable`, `auto` is treated as `manual`, and the state is reported
    rather than silent. A future author who marks a migration `auto` does not get unattended application —
    they get a refusal naming what must be built first. They cannot forget a thing that refuses to work.

    The slice that implements provenance replaces this body and MUST keep
    `test_the_gate_is_a_gate_not_a_constant_false` passing.
    """
    return ("unavailable",
            "no sealed-runtime or release-provenance gate exists; a migration cannot be applied "
            "unattended until one covers every auto-heal entry point")


def classify_pending(applied: set[str]) -> tuple[list[str], list[str]]:
    """(auto_applicable, manual_blockers) for the migrations not yet in *applied*.

    A lock proves a migration is **the reviewed one**; it does not prove it is **safe to apply
    unattended**. Migration 039 is the standing example — locked, declared non-additive, and carrying an
    unconditional `UPDATE client_devices SET capabilities = …`. Under a lock-only rule it would have
    self-applied at 02:07 with nobody watching.

    So auto-heal is opt-in per migration, and the caller must refuse the WHOLE batch when any pending
    migration is manual: applying the auto ones first would leave the database in a state no one reviewed,
    and would bury the blocker behind a partial success.
    """
    provenance, _reason = release_provenance_status()
    auto: list[str] = []
    manual: list[str] = []
    for name, _digest, auto_apply in _read_lock():
        if name in applied:
            continue
        # `auto` is honoured only when release provenance is available. Today it never is, so
        # an `auto` entry is inert rather than dangerous — the precondition enforces itself.
        (auto if (auto_apply and provenance == "ok") else manual).append(name)
    return auto, manual
