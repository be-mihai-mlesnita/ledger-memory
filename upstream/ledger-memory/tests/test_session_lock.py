"""The lifetime shared session lock: shared holders coexist, an exclusive attempt is refused while one exists."""
import os
import time
import threading
import pytest
from pathlib import Path

from ledger_memory import locking

pytestmark = pytest.mark.skipif(os.name == "nt", reason="POSIX flock")


def test_session_lock_path_is_beside_the_database(tmp_path):
    db = tmp_path / "data" / "store.sqlite"
    lock_path = locking.session_lock_path(db)
    assert lock_path == db.parent / ".ledger.sessions.lock"


def test_shared_holders_coexist(tmp_path):
    p = tmp_path / "lockfile"
    with locking.shared(p, timeout=0):
        with locking.shared(p, timeout=0):
            pass  # both entered


def test_exclusive_refused_while_shared_held(tmp_path):
    p = tmp_path / "lockfile"
    with locking.shared(p, timeout=0):
        with pytest.raises(TimeoutError):
            locking.exclusive(p, timeout=0).__enter__()


def test_shared_waits_for_exclusive_and_times_out(tmp_path):
    p = tmp_path / "lockfile"
    exc = None

    def try_shared():
        nonlocal exc
        try:
            with locking.shared(p, timeout=0.2):
                pass
        except TimeoutError as e:
            exc = e

    with locking.exclusive(p, timeout=0):
        t = threading.Thread(target=try_shared)
        start = time.monotonic()
        t.start()
        t.join()
        elapsed = time.monotonic() - start

    assert exc is not None
    assert isinstance(exc, TimeoutError)
    assert elapsed >= 0.15


def test_shared_released_on_exit(tmp_path):
    p = tmp_path / "lockfile"
    # normal exit
    with locking.shared(p, timeout=0):
        pass
    with locking.exclusive(p, timeout=0):
        pass

    # exit via exception
    class Boom(Exception):
        pass

    try:
        with locking.shared(p, timeout=0):
            raise Boom()
    except Boom:
        pass
    with locking.exclusive(p, timeout=0):
        pass


def test_shared_refuses_symlink(tmp_path):
    target = tmp_path / "real"
    target.write_text("x")
    link = tmp_path / "link"
    link.symlink_to(target)

    with pytest.raises(OSError):
        with locking.shared(link, timeout=0):
            pass

    # target should be untouched (still a regular file)
    assert target.is_file()
    assert target.read_text() == "x"
