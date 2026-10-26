from __future__ import annotations

"""portable, checksummed DB package + verified restore.

A **true binary archive** — fully reversible. `package_database` VACUUMs the DB into a temp file (to
defragment + drop free pages), then compresses that file's exact bytes with stdlib `lzma`/xz (preset
9|EXTREME). Decompressing gives back a byte-faithful database, so EVERYTHING round-trips — including FTS5
virtual tables and their index data (a SQL-text `.dump` does NOT round-trip FTS5; the binary copy does).
A SHA-256 sidecar is written.

`restore_database` is FAIL-CLOSED and triple-guarded: (1) the archive's SHA-256 is checked against the
sidecar / an expected hash (mismatch → refuse); (2) `lzma.decompress` raises on a corrupt archive (xz's
own container CRC); (3) the rebuilt DB must pass `PRAGMA integrity_check`. It refuses to overwrite an
existing target unless forced, and leaves no DB behind on failure. This is the primitive the future
off-machine backup (synced / Google Drive / Gen8 via ssh) and the chat restore menu will call.
"""

import hashlib
import lzma
import os
import sqlite3
import tempfile
from dataclasses import dataclass
from pathlib import Path

from ledger_memory.fs_safety import exclusive_write_bytes, path_present

_LZMA_PRESET = 9 | lzma.PRESET_EXTREME
_ARCHIVE_SUFFIX = ".db.xz"
_MAX_SAME_SECOND = 9  # one base name + `_2`..`_9`; single digits keep name order == age order


@dataclass(frozen=True)
class PackageResult:
    archive_path: str
    sha256_path: str
    sha256: str
    uncompressed_bytes: int
    compressed_bytes: int
    ratio: float


@dataclass(frozen=True)
class RestoreResult:
    output_db_path: str
    sha256_verified: bool
    integrity_ok: bool
    table_count: int
    replaced: bool = False


class ChecksumMismatchError(Exception):
    """The archive's SHA-256 does not match the expected hash — refuse to restore."""


class SourceIntegrityError(Exception):
    """The source database failed PRAGMA integrity_check — refuse to certify a corrupt source."""


class RestoreTargetBusyError(Exception):
    """The restore target has a live ``-wal`` (a writer is active or it was not cleanly closed). Restoring only the
    main file would let the stale WAL replay over the fresh image — refuse until the target is quiesced."""


class UnverifiedArchiveError(Exception):
    """A restore was requested without a verifiable checksum (no ``.sha256`` sidecar and no ``--sha256``) and without
    the explicit ``allow_unverified`` opt-out. The restore path is fail-closed by default."""


class UnsafeLabelError(Exception):
    """A backup label would escape the output directory."""


class BackupCollisionError(Exception):
    """A backup with the same label+timestamp already exists; refusing to overwrite it."""


def _validate_label(label: str) -> None:
    """a backup label is a plain filename prefix, never a path. Reject separators, traversal, and
    absolute/empty labels so the archive can never be written outside --output-dir."""
    if not label or label in (".", "..") or "/" in label or "\\" in label or "\x00" in label:
        raise UnsafeLabelError(f"unsafe backup label (must be a plain name): {label!r}")


def _assert_source_integrity(database_path: Path) -> None:
    """Fail-closed if the SOURCE is corrupt — VACUUM INTO can walk past corruption and silently drop
    unreachable data into a structurally-clean file, producing a 'green' backup of a corrupt source. The
    restore-side check validates the rebuilt file, not fidelity to the original, so the source must be
    checked HERE before it is certified as a backup."""
    conn = sqlite3.connect(str(database_path))
    try:
        result = conn.execute("PRAGMA integrity_check").fetchone()[0]
    except sqlite3.DatabaseError as exc:
        result = f"unreadable ({exc})"  # not a database / malformed image
    finally:
        conn.close()
    if result != "ok":
        raise SourceIntegrityError(
            f"source database failed integrity_check: {result}; refusing to package a corrupt source"
        )


def _vacuum_to_bytes(database_path: Path) -> bytes:
    """VACUUM INTO a temp file (defrag), return its exact bytes — a byte-faithful copy of the whole DB."""
    with tempfile.TemporaryDirectory() as tmp:
        vacuumed = Path(tmp) / "vacuumed.db"
        source = sqlite3.connect(str(database_path))
        try:
            source.execute("VACUUM INTO ?", (str(vacuumed),))
        finally:
            source.close()
        return vacuumed.read_bytes()


def package_database(
    database_path: Path,
    *,
    output_dir: Path,
    label: str = "ledger",
    timestamp: str,
) -> PackageResult:
    """Package *database_path* into ``<output_dir>/<label>-<timestamp>.db.xz`` + a ``.sha256`` sidecar."""
    database_path = Path(database_path)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    _validate_label(label)  # a label is a plain name, never a path

    _assert_source_integrity(database_path)  # never certify a corrupt source as a good backup
    raw = _vacuum_to_bytes(database_path)
    compressed = lzma.compress(raw, preset=_LZMA_PRESET)

    archive_path = output_dir / f"{label}-{timestamp}{_ARCHIVE_SUFFIX}"
    if archive_path.resolve().parent != output_dir.resolve():  # belt-and-suspenders containment
        raise UnsafeLabelError(f"backup label escapes output_dir: {label!r}")
    # never silently overwrite a prior snapshot on a label/timestamp collision, AND treat a
    # pre-existing SYMLINK at either path (incl. a broken one, which Path.exists() misses) as a collision — a planted
    # `<archive>`/`.sha256` symlink must never redirect the write outside output_dir.
    # timestamps are to the second, so a second backup inside the same second (a retry
    # after a failed run — an earlier fix hit it) was refused. A slot held by REAL files now moves the new backup to the next
    # free `_2`..`_9` suffix (`_` sorts after `.`, so it also sorts as newer); the prior snapshot is still never touched.
    # A symlink at ANY candidate path still refuses outright — never stepped around.
    for n in range(1, _MAX_SAME_SECOND + 1):
        candidate = output_dir / f"{label}-{timestamp}{'' if n == 1 else f'_{n}'}{_ARCHIVE_SUFFIX}"
        candidate_sha = Path(f"{candidate}.sha256")
        if candidate.is_symlink() or candidate_sha.is_symlink():
            raise BackupCollisionError(f"refusing to write a backup at {candidate}: a planted symlink holds that path")
        if not path_present(candidate) and not path_present(candidate_sha):
            archive_path, sha256_path = candidate, candidate_sha
            break
    else:
        raise BackupCollisionError(
            f"refusing to overwrite an existing backup at {archive_path}: {_MAX_SAME_SECOND} backups already hold "
            f"label {label!r} at {timestamp}"
        )
    digest = hashlib.sha256(compressed).hexdigest()
    # write both artifacts as fresh regular files (O_EXCL|O_NOFOLLOW) so a TOCTOU-raced symlink can't
    # be followed.
    exclusive_write_bytes(archive_path, compressed)
    exclusive_write_bytes(sha256_path, f"{digest}\n".encode("utf-8"))

    return PackageResult(
        archive_path=str(archive_path),
        sha256_path=str(sha256_path),
        sha256=digest,
        uncompressed_bytes=len(raw),
        compressed_bytes=len(compressed),
        ratio=round(len(raw) / max(1, len(compressed)), 2),
    )


def _newest_archive(directory: Path) -> Path | None:
    archives = sorted(
        Path(directory).glob(f"*{_ARCHIVE_SUFFIX}"), key=lambda p: (p.stat().st_mtime, p.name)
    )
    return archives[-1] if archives else None


def restore_database(
    archive_path: Path,
    output_db_path: Path,
    *,
    expected_sha256: str | None = None,
    force: bool = False,
    allow_unverified: bool = False,
) -> RestoreResult:
    """Rebuild a DB from a ``.db.xz`` archive. Fail-closed: checksum + xz-CRC + integrity_check.

    The checksum is **mandatory by default**: with no ``.sha256`` sidecar and no *expected_sha256*,
    restore raises ``UnverifiedArchiveError`` unless *allow_unverified* is set (the CLI ``--allow-unverified`` escape
    hatch — not recommended, especially for store restores). Also raises ``ChecksumMismatchError`` on a hash mismatch,
    ``FileExistsError`` if *output_db_path* exists and not *force*, ``lzma.LZMAError`` on a corrupt archive, and leaves
    no DB behind on a failed restore.
    """
    archive_path = Path(archive_path)
    output_db_path = Path(output_db_path)

    compressed = archive_path.read_bytes()
    actual_sha256 = hashlib.sha256(compressed).hexdigest()

    # a PRESENT checksum is mandatory content. An empty or whitespace-only sidecar used to read as "" —
    # neither None (so the no-checksum refusal was skipped) nor truthy (so no comparison ran) — and a tampered
    # archive restored unverified. Accept `<64 hex>` optionally followed by a file name (shasum format).
    def _checksum_token(text: str, origin: str) -> str:
        parts = text.strip().split()
        token = parts[0] if parts else ""
        if len(token) != 64 or any(c not in "0123456789abcdefABCDEF" for c in token):
            raise ChecksumMismatchError(f"checksum {origin} is empty or malformed")
        return token.lower()

    if expected_sha256 is not None:
        expected_sha256 = _checksum_token(expected_sha256, "argument")
    else:
        sidecar = Path(f"{archive_path}.sha256")
        if sidecar.exists():
            # utf-8-sig: a byte-order mark written by some editors is not part of the hash.
            expected_sha256 = _checksum_token(sidecar.read_text(encoding="utf-8-sig"), f"file {sidecar}")

    # the restore path is fail-closed by default — refuse a checksum-less restore unless explicitly
    # opted in. xz-CRC + integrity_check prove the container/structure, not that this is the intended trusted artifact.
    if expected_sha256 is None and not allow_unverified:
        raise UnverifiedArchiveError(
            f"refusing to restore {archive_path}: no .sha256 sidecar and no expected hash given. Supply a verified "
            "checksum, or pass allow_unverified=True to override — NOT recommended, "
            "especially for memory restores."
        )

    sha256_verified = False
    if expected_sha256:
        # case/space-insensitive: a correct hash pasted in uppercase must not spuriously block a restore.
        if actual_sha256 != expected_sha256:
            raise ChecksumMismatchError(
                f"archive sha256 mismatch: expected {expected_sha256}, got {actual_sha256}"
            )
        sha256_verified = True

    if output_db_path.is_symlink():
        # a restore target must be a regular file. A symlink here (incl. a broken one, which
        # Path.exists() misses — so an exists()-only no-clobber check let restore proceed WITHOUT --force) would let
        # the restore replace/redirect the link; refuse it outright.
        raise FileExistsError(f"refusing to restore over a symlink: {output_db_path} (remove it first)")
    # every target-side refusal happens BEFORE the swap, so a refusal never follows a replaced database.
    for side in (Path(f"{output_db_path}-wal"), Path(f"{output_db_path}-shm")):
        if side.is_symlink():
            raise FileExistsError(f"refusing to restore: {side} is a symlink (remove it first)")
    if output_db_path.exists():
        # a live `-wal` means a writer is/was active; restoring only the main file while the WAL
        # bundle survives lets the stale WAL replay over the fresh image (silent revert/corruption). Refuse until
        # the target is quiesced — regardless of force; the store-rollback floor must never race a live writer.
        target_wal = Path(f"{output_db_path}-wal")
        if target_wal.exists() and target_wal.stat().st_size > 0:
            raise RestoreTargetBusyError(
                f"refusing to restore over {output_db_path}: it has a live -wal (a writer is active or it was not "
                "cleanly closed). Stop every process using the memory (stop writers + `PRAGMA wal_checkpoint(TRUNCATE)`) before restoring.")
        if not force:
            raise FileExistsError(f"refusing to overwrite existing database: {output_db_path} (use force)")

    raw = lzma.decompress(compressed)  # raises LZMAError on a corrupt archive (xz container CRC)

    # Restore ATOMICALLY: write + verify a staging file, then os.replace() onto the target only after it
    # passes — so a failed/forced restore can NEVER destroy an existing good DB at output_db_path.
    output_db_path.parent.mkdir(parents=True, exist_ok=True)
    staging = output_db_path.parent / f"{output_db_path.name}.restoring"
    # Path.write_bytes FOLLOWS a symlink — a pre-existing staging symlink would let restore write the
    # DB bytes into the symlink's target and then os.replace the symlink onto --out (clobbering another file, leaving
    # --out a symlink). Refuse any pre-existing staging path and create it exclusively (O_EXCL|O_NOFOLLOW) so we only
    # ever write/verify/replace a fresh REGULAR file.
    if staging.exists() or staging.is_symlink():
        raise RuntimeError(f"refusing to restore: a stale staging path exists (remove it first): {staging}")
    _staging_fd = os.open(staging, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0), 0o600)
    try:
        with os.fdopen(_staging_fd, "wb") as _staging_fh:
            _staging_fh.write(raw)  # byte-faithful: the whole DB, FTS5 and all
    except BaseException:
        staging.unlink(missing_ok=True)
        raise
    conn = sqlite3.connect(str(staging))
    try:
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        table_count = conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table'"
        ).fetchone()[0]
        conn.close()
    except Exception:
        conn.close()
        staging.unlink(missing_ok=True)  # never leave a partial file behind
        raise

    if integrity != "ok":
        staging.unlink(missing_ok=True)
        raise ValueError(f"restored database failed integrity_check: {integrity}")

    os.replace(staging, output_db_path)  # atomic swap; the existing target survives until this point
    # drop any stale sidecars so a leftover -wal/-shm can't replay onto the freshly-restored image.
    # C3: only regular files are unlinked (symlinks were refused before the swap — an earlier fix — and a
    # link planted after it is left alone rather than followed or reported as a failed restore).
    for side in (Path(f"{output_db_path}-wal"), Path(f"{output_db_path}-shm")):
        if side.exists() and not side.is_symlink():
            side.unlink()

    return RestoreResult(
        output_db_path=str(output_db_path),
        sha256_verified=sha256_verified,
        integrity_ok=True,
        table_count=int(table_count),
        replaced=True,
    )
