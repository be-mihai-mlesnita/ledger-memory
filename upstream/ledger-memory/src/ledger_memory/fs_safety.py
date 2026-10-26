"""No-follow, link-aware filesystem write helpers.

`Path.write_text`/`write_bytes` FOLLOW a pre-existing symlink at the destination, and `Path.exists`
returns False for a BROKEN symlink — so a planted symlink can redirect a "safe" write outside its
intended directory, or defeat an `exists`-based no-clobber/collision guard. These helpers create the
destination as a fresh REGULAR file with `O_EXCL|O_NOFOLLOW` (so a symlink can never be followed, even
under a TOCTOU race) and expose a link-aware presence check for no-clobber guards.

This module is the single place that gets symlink-safe writes right; callers on the store-update floor
(backup/restore, the service-mode lock) and derived-artifact writers (projections, export sidecars)
route through it rather than re-deriving the O_EXCL|O_NOFOLLOW dance.
"""
from __future__ import annotations

import os
from pathlib import Path


def path_present(path: Path) -> bool:
    """Link-aware existence: True if *path* is ANY filesystem entry, INCLUDING a broken symlink.

    `Path.exists()` follows symlinks and returns False for a dangling one, so an `exists()`-only
    no-clobber/collision guard can be bypassed by pre-planting a broken symlink. Use this instead.
    """
    return path.exists() or path.is_symlink()


def exclusive_write_bytes(path: Path, data: bytes, *, mode: int = 0o600) -> None:
    """Write *data* to *path* as a fresh REGULAR file, refusing any pre-existing path (incl. a symlink).

    Opens with `O_CREAT|O_EXCL|O_NOFOLLOW` so a planted symlink at *path* can never be followed and a
    pre-existing file is never silently overwritten — for create-once artifacts (backup archive + its
    `.sha256` sidecar, export sidecars). On any failure the partial file is removed.
    """
    if path_present(path):
        raise FileExistsError(f"refusing to write {path}: a file or symlink already exists there")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0), mode)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
    except BaseException:
        path.unlink(missing_ok=True)  # never leave a partial file behind
        raise


def overwrite_write_bytes_nofollow(path: Path, data: bytes, *, mode: int = 0o600) -> None:
    """Write *data* to *path*, refusing a SYMLINK but allowing overwrite of an existing REGULAR file.

    `O_CREAT|O_TRUNC|O_NOFOLLOW` — the `O_NOFOLLOW` makes the open fail (ELOOP) if *path* is a symlink,
    so a planted symlink can't redirect the write, while a stale REGULAR file is legitimately replaced.
    For lock files (the service-mode lock) that intentionally overwrite a stale regular lock.
    """
    if path.is_symlink():
        raise FileExistsError(f"refusing to write {path}: it is a symlink")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0), mode)
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)


def append_bytes_nofollow(path: Path, data: bytes, *, mode: int = 0o600) -> None:
    """Append *data* to *path*, refusing a symlink at the destination OR at its parent directory.

    Opens the parent with `O_DIRECTORY|O_NOFOLLOW` first, then the file relative to it via
    `dir_fd` with `O_APPEND|O_CREAT|O_NOFOLLOW` (fresh file created `0o600` if absent). A symlink
    at the final component OR at the immediate parent fails (ELOOP/ENOTDIR) rather than appending
    through it. Honest limit (review, 2026-08-07): components ABOVE the immediate parent follow
    the same threat model as the sibling primitives here — not walked. For append-only ledgers
    (a content-free receipt log) where the whole-file primitives above don't fit — an earlier fix:
    adding it here instead of open-coding the dance at the call site is the fix-the-class move.
    """
    parent = Path(path).parent
    parent_fd = os.open(parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0))
    try:
        fd = os.open(
            Path(path).name,
            os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0),
            mode,
            dir_fd=parent_fd,
        )
        with os.fdopen(fd, "ab") as handle:
            handle.write(data)
    finally:
        os.close(parent_fd)


def atomic_replace_via_nofollow_temp(path: Path, data: bytes, *, mode: int = 0o600) -> None:
    """Atomically write *data* to *path* via a no-follow `<name>.tmp` sibling, then `os.replace` — regenerating
    *path* in place.

    A pre-existing temp that is a SYMLINK is refused (it could redirect the write outside the directory); a stale
    REGULAR temp from a prior crashed run is removed and reused (preserving the prior atomic-overwrite contract — the
    next run must not break). The temp is then (re)created `O_EXCL|O_NOFOLLOW`, so a temp planted in the unlink→create
    window can't be followed; `os.replace` moves the regular temp onto *path* (overwriting it is the intended swap).
    For derived artifacts regenerated in place (markdown projections). On failure the temp is removed.
    """
    tmp = path.with_name(f"{path.name}.tmp")
    if tmp.is_symlink():
        raise FileExistsError(f"refusing to write via {tmp}: it is a symlink")
    if tmp.exists():
        tmp.unlink()  # a stale REGULAR temp from a prior crash — clear it so the (re)create below can reuse the name
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0), mode)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
