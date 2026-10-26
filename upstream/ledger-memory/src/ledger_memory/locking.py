"""Small cross-process locks: an exclusive one for operations that must not run twice at once (first start, upgrade, reader
creation), and a shared one that every session holds for its lifetime (`shared`). Advisory: ``fcntl.flock`` on POSIX,
``msvcrt.locking`` / ``LockFileEx`` on Windows. The exclusive lock is re-entrant for the owning thread."""
from __future__ import annotations

import errno
import math
import os
import stat
import threading
import time
from contextlib import contextmanager
from pathlib import Path

_WINDOWS = os.name == "nt"
msvcrt = None
try:                                    # POSIX
    import fcntl
except ImportError:                     # Windows
    fcntl = None
    try:
        import msvcrt
    except ImportError:
        msvcrt = None

_REGISTRY_LOCK = threading.Lock()
_PATH_LOCKS: dict[str, threading.RLock] = {}
_ACTIVE_OWNERS: dict[str, tuple[int, int]] = {}
_LOCAL = threading.local()


def _windows_shared_probe(fd: int) -> bool:
    """Use a real shared byte lock; CRT read-lock modes are still exclusive."""
    import ctypes

    class Overlapped(ctypes.Structure):
        _fields_ = [("Internal", ctypes.c_size_t), ("InternalHigh", ctypes.c_size_t),
                    ("Offset", ctypes.c_uint32), ("OffsetHigh", ctypes.c_uint32),
                    ("hEvent", ctypes.c_void_p)]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    lock = kernel.LockFileEx
    lock.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32,
                     ctypes.c_uint32, ctypes.c_uint32, ctypes.POINTER(Overlapped)]
    lock.restype = ctypes.c_int32
    unlock = kernel.UnlockFileEx
    unlock.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32,
                       ctypes.c_uint32, ctypes.POINTER(Overlapped)]
    unlock.restype = ctypes.c_int32
    handle = msvcrt.get_osfhandle(fd)
    state = Overlapped()
    # FAIL_IMMEDIATELY without EXCLUSIVE: shared observers never block one another.
    if not lock(handle, 1, 0, 1, 0, ctypes.byref(state)):
        error = ctypes.get_last_error()
        if error == 33:  # ERROR_LOCK_VIOLATION, not an arbitrary access failure.
            return True
        raise OSError(error, "Windows lifecycle-lock probe unavailable")
    if not unlock(handle, 0, 1, 0, ctypes.byref(state)):
        raise OSError(ctypes.get_last_error(), "Windows lifecycle-lock release unavailable")
    return False


def is_held(lock_path: Path | str) -> bool:
    """Observe an existing lock without creating files or changing its bytes.

    This is a point-in-time observation, not an authorization to replace an
    owner. Errors remain errors rather than being reported as an idle lock.
    """
    if fcntl is None and (not _WINDOWS or msvcrt is None):
        raise RuntimeError("shared lifecycle-lock observation is unavailable on this platform")
    path = Path(os.path.abspath(lock_path))
    path = path.parent.resolve() / path.name
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise OSError("lock path must be a single-link regular file")
    key = os.path.normcase(str(path))
    with _REGISTRY_LOCK:
        owner = _ACTIVE_OWNERS.get(key)
        if owner is not None:
            if owner != (info.st_dev, info.st_ino):
                raise OSError("owned lock path changed during observation")
            return True
    fd = os.open(path, os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
                     | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0))
    try:
        opened = os.fstat(fd)
        if ((opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino)
                    or not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1):
            raise OSError("lock path changed during observation")
        if fcntl is None:
            return _windows_shared_probe(fd)
        try:
            # Shared probes coexist; only a real exclusive owner blocks them.
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in (errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK):
                return True
            raise
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)


@contextmanager
def exclusive(lock_path: Path | str, *, timeout: float = 120.0):
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
        raise ValueError("timeout must be finite and non-negative")
    if not math.isfinite(timeout) or timeout < 0:
        raise ValueError("timeout must be finite and non-negative")
    if fcntl is None and msvcrt is None:
        raise RuntimeError("OS locking is unavailable")
    deadline = time.monotonic() + timeout
    path = Path(os.path.abspath(lock_path))
    if path.is_symlink():
        raise OSError("lock path must not be a symlink")
    # Canonical parents make OS aliases share one thread lock (notably /var on macOS).
    path = path.parent.resolve() / path.name
    key = os.path.normcase(str(path))
    with _REGISTRY_LOCK:
        local_lock = _PATH_LOCKS.setdefault(key, threading.RLock())
    if not local_lock.acquire(timeout=max(0.0, deadline - time.monotonic())):
        raise TimeoutError("timeout acquiring thread lock")
    try:
        held = getattr(_LOCAL, "held", None)
        if held is None:
            held = _LOCAL.held = set()
        if key in held:
            yield
            return
        if path.is_symlink():
            raise OSError("lock path must not be a symlink")
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
                     | getattr(os, "O_BINARY", 0), 0o600)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise OSError("lock path must be a regular file")
            while True:
                try:
                    if fcntl is not None:
                        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    else:
                        os.lseek(fd, 0, os.SEEK_SET)
                        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                    break
                except OSError as exc:
                    if exc.errno not in (errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK):
                        raise
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError("timeout acquiring OS lock") from exc
                    time.sleep(min(0.05, remaining))
            held.add(key)
            try:
                with _REGISTRY_LOCK:
                    identity = os.fstat(fd)
                    _ACTIVE_OWNERS[key] = (identity.st_dev, identity.st_ino)
                yield
            finally:
                held.remove(key)
                with _REGISTRY_LOCK:
                    _ACTIVE_OWNERS.pop(key, None)
                if fcntl is not None:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                else:
                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        finally:
            os.close(fd)
    finally:
        local_lock.release()


# Lifetime session lock: every process using a memory holds it SHARED; an in-place restore takes it EXCLUSIVELY
# and refuses when anyone holds it (review fix, 0.3.0-rc3).
def session_lock_path(database_path: Path | str) -> Path:
    """Return the path of the session lock file beside *database_path*."""
    return Path(database_path).parent / ".ledger.sessions.lock"


def _windows_shared_lock(fd: int):
    """Acquire a shared lock on byte 0 of *fd* (Windows)."""
    import ctypes

    class Overlapped(ctypes.Structure):
        _fields_ = [
            ("Internal", ctypes.c_size_t),
            ("InternalHigh", ctypes.c_size_t),
            ("Offset", ctypes.c_uint32),
            ("OffsetHigh", ctypes.c_uint32),
            ("hEvent", ctypes.c_void_p),
        ]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    lock = kernel.LockFileEx
    lock.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.POINTER(Overlapped),
    ]
    lock.restype = ctypes.c_int32
    unlock = kernel.UnlockFileEx
    unlock.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.POINTER(Overlapped),
    ]
    unlock.restype = ctypes.c_int32

    handle = msvcrt.get_osfhandle(fd)
    state = Overlapped()
    # LOCKFILE_FAIL_IMMEDIATELY = 1, no LOCKFILE_EXCLUSIVE_LOCK -> shared
    if not lock(handle, 1, 0, 1, 0, ctypes.byref(state)):
        error = ctypes.get_last_error()
        if error == 33:  # ERROR_LOCK_VIOLATION
            raise OSError(error, "shared lock violation")
        raise OSError(error, "Windows shared lock unavailable")

    def _unlock():
        if not unlock(handle, 0, 1, 0, ctypes.byref(state)):
            raise OSError(ctypes.get_last_error(), "Windows shared unlock failed")

    return _unlock


@contextmanager
def shared(lock_path: Path | str, *, timeout: float = 120.0):
    """
    Acquire a **shared** advisory lock on *lock_path*.

    Multiple shared holders (even within the same process) may hold the lock
    simultaneously.  An exclusive holder (from any process or thread) blocks
    new shared holders, and shared holders block new exclusive holders.
    The lock is advisory: a process that ignores this protocol is not stopped.

    Parameters
    ----------
    lock_path: Path | str
        Path to the lock file.  The file is created with mode 0o600 in a
        directory with mode 0o700 if it does not exist.  Symlinks are rejected.
    timeout: float, default 120.0
        Maximum seconds to wait for the lock.  ``timeout=0`` makes a single
        non-blocking attempt.  Must be finite and non-negative.

    Raises
    ------
    TimeoutError
        If the lock cannot be acquired within *timeout*.
    OSError
        If *lock_path* is a symlink or not a regular file, or on other OS errors.
    ValueError
        If *timeout* is not a finite non-negative number.
    RuntimeError
        If neither ``fcntl`` nor ``msvcrt`` is available.
    """
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
        raise ValueError("timeout must be finite and non-negative")
    if not math.isfinite(timeout) or timeout < 0:
        raise ValueError("timeout must be finite and non-negative")
    if fcntl is None and msvcrt is None:
        raise RuntimeError("OS locking is unavailable")

    deadline = time.monotonic() + timeout
    path = Path(os.path.abspath(lock_path))
    if path.is_symlink():
        raise OSError("lock path must not be a symlink")
    # Canonical parents make OS aliases share one lock (notably /var on macOS).
    path = path.parent.resolve() / path.name

    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(
        path,
        os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0),
        0o600,
    )
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError("lock path must be a regular file")

        if fcntl is not None:
            # POSIX: shared flock
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
                    break
                except OSError as exc:
                    if exc.errno not in (errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK):
                        raise
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError("timeout acquiring shared OS lock") from exc
                    time.sleep(min(0.05, remaining))
            try:
                yield
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
        else:
            # Windows: shared byte-range lock on byte 0 length 1
            while True:
                try:
                    unlock = _windows_shared_lock(fd)
                    break
                except OSError as exc:
                    if 33 not in (exc.errno, getattr(exc, "winerror", None)):  # ERROR_LOCK_VIOLATION
                        raise
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError("timeout acquiring shared OS lock") from exc
                    time.sleep(min(0.05, remaining))
            try:
                yield
            finally:
                unlock()
    finally:
        os.close(fd)
