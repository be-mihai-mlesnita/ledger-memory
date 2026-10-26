"""Synthetic worker protocol checks; never contact a model or live service."""
from __future__ import annotations

import importlib
import json
import multiprocessing
import os
import struct
import tempfile
import time
import uuid
import functools
import threading
from pathlib import Path
from types import SimpleNamespace
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager

import pytest


@pytest.fixture
def short_profile_root():
    with tempfile.TemporaryDirectory(prefix="lp-", dir="/tmp" if os.name != "nt" else None) as directory:
        yield Path(directory)


def test_multiple_stores_share_one_model_worker(short_profile_root, monkeypatch):
    worker = importlib.import_module("ledger_memory.embedding_worker")
    tmp_path = short_profile_root
    home = tmp_path / "user"
    home.mkdir()
    monkeypatch.setattr(worker.Path, "home", lambda: home)
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
    monkeypatch.setenv("LEDGER_HOME", str(tmp_path / "store-one"))
    first = worker.worker_profile()
    monkeypatch.setenv("LEDGER_HOME", str(tmp_path / "store-two"))
    second = worker.worker_profile()
    assert first == second
    assert not first.root.exists()
    assert worker.read_worker_key(first) is None
    assert not first.root.exists()
    assert str(home) in str(first.root)


def test_worker_profile_key_is_private_immutable_and_shared(short_profile_root):
    worker = importlib.import_module("ledger_memory.embedding_worker")
    tmp_path = short_profile_root
    profile = worker.worker_profile(root=tmp_path / "profile")
    first = worker.prepare_worker_profile(profile)
    second = worker.prepare_worker_profile(profile)
    assert len(first) == 32
    assert first == second == worker.read_worker_key(profile)
    if os.name != "nt":
        assert profile.root.stat().st_mode & 0o777 == 0o700
        assert profile.key.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("kind", ["symlink", "short", "hardlink", "directory", "public"])
def test_worker_profile_refuses_unsafe_key_without_replacing_it(short_profile_root, kind):
    worker = importlib.import_module("ledger_memory.embedding_worker")
    tmp_path = short_profile_root
    profile = worker.worker_profile(root=tmp_path / "profile")
    profile.root.mkdir(mode=0o700)
    target = tmp_path / "target"
    target.write_bytes(b"x" * 32)
    if kind == "symlink":
        profile.key.symlink_to(target)
    elif kind == "hardlink":
        os.link(target, profile.key)
    elif kind == "directory":
        profile.key.mkdir()
    else:
        profile.key.write_bytes(b"short" if kind == "short" else b"x" * 32)
        profile.key.chmod(0o644 if kind == "public" else 0o600)
    if kind == "public" and os.name == "nt":
        pytest.skip("Windows relies on inherited profile ACL, not POSIX mode bits")
    before = profile.key.lstat()
    with pytest.raises(worker.WorkerProtocolError):
        worker.prepare_worker_profile(profile)
    assert profile.key.lstat().st_ino == before.st_ino
    assert target.read_bytes() == b"x" * 32


def test_worker_profile_refuses_symlink_directory(short_profile_root):
    worker = importlib.import_module("ledger_memory.embedding_worker")
    tmp_path = short_profile_root
    actual = tmp_path / "actual"
    actual.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(actual, target_is_directory=True)
    with pytest.raises(worker.WorkerProtocolError, match="real directory"):
        worker.prepare_worker_profile(worker.worker_profile(root=alias))
    assert list(actual.iterdir()) == []


def test_concurrent_worker_profile_creation_keeps_one_key(short_profile_root):
    worker = importlib.import_module("ledger_memory.embedding_worker")
    profile = worker.worker_profile(root=short_profile_root / "profile")
    with ThreadPoolExecutor(max_workers=8) as pool:
        keys = list(pool.map(lambda _: worker.prepare_worker_profile(profile), range(16)))
    assert len(set(keys)) == 1
    assert worker.read_worker_key(profile) == keys[0]


def test_worker_profile_rejects_overlong_socket_path(tmp_path):
    worker = importlib.import_module("ledger_memory.embedding_worker")
    if os.name == "nt":
        pytest.skip("named pipes do not have the POSIX socket path bound")
    with pytest.raises(worker.WorkerProtocolError, match="too long"):
        worker.worker_profile(root=tmp_path / ("x" * 110))
    assert not (tmp_path / ("x" * 110)).exists()


def test_profile_entry_holds_service_lock_and_records_only_actual_warm(short_profile_root, monkeypatch):
    worker = importlib.import_module("ledger_memory.embedding_worker")
    from ledger_memory.locking import exclusive
    profile = worker.worker_profile(root=short_profile_root / "profile")
    seen = []
    def service(address, **options):
        assert address == profile.address
        assert options["authkey"] == worker.read_worker_key(profile)
        assert options["model_lock_path"] == profile.root / "model.lock"
        with ThreadPoolExecutor(max_workers=1) as pool:
            def competing_service():
                with pytest.raises(TimeoutError):
                    with exclusive(profile.root / "service.lock", timeout=0):
                        pytest.fail("second service acquired lifetime lock")
            pool.submit(competing_service).result(timeout=2)
        assert worker.read_worker_state(profile) == {}
        assert options["before_model_start"]()
        assert "warm_fingerprint" not in worker.read_worker_state(profile)
        options["on_warm"]()
        receipt = worker.read_worker_state(profile)
        assert receipt["warm_fingerprint"] == options["model_fingerprint"]
        assert receipt["state"] == "warm"
        assert len(receipt["starts"]) == 1
        seen.append(True)
        return {"completed": 0}
    monkeypatch.setattr(worker, "serve_worker", service)
    assert worker.run_user_worker(profile=profile) == {"completed": 0}
    assert seen == [True]
    assert worker.read_worker_state(profile)["state"] == "idle"
    assert len(worker.read_worker_key(profile)) == 32


def test_profile_entry_refuses_non_socket_endpoint(short_profile_root, monkeypatch):
    worker = importlib.import_module("ledger_memory.embedding_worker")
    if os.name == "nt":
        pytest.skip("filesystem endpoint cleanup is POSIX-only")
    profile = worker.worker_profile(root=short_profile_root / "profile")
    worker.prepare_worker_profile(profile)
    endpoint = Path(profile.address)
    endpoint.write_bytes(b"do not replace")
    monkeypatch.setattr(worker, "serve_worker", lambda *a, **k: pytest.fail("unsafe endpoint reached service"))
    with pytest.raises(worker.WorkerProtocolError, match="socket"):
        worker.run_user_worker(profile=profile)
    assert endpoint.read_bytes() == b"do not replace"


def test_profile_restart_budget_survives_service_exit(short_profile_root, monkeypatch):
    worker = importlib.import_module("ledger_memory.embedding_worker")
    profile = worker.worker_profile(root=short_profile_root / "profile")
    allowed = []
    def service(address, **options):
        allowed.append(options["before_model_start"]())
        return {"completed": 0}
    monkeypatch.setattr(worker, "serve_worker", service)
    for _ in range(4):
        worker.run_user_worker(profile=profile)
    assert allowed == [True, True, True, False]
    state = worker.read_worker_state(profile)
    assert len(state["starts"]) == 3
    assert state["reason"] == "restart_budget_exhausted"


def test_profile_restart_budget_refuses_rollback_and_expires_old_starts(short_profile_root, monkeypatch):
    worker = importlib.import_module("ledger_memory.embedding_worker")
    profile = worker.worker_profile(root=short_profile_root / "profile")
    now = [100.0]
    allowed = []
    monkeypatch.setattr(worker.time, "time", lambda: now[0])
    def service(address, **options):
        allowed.append(options["before_model_start"]())
        return {"completed": 0}
    monkeypatch.setattr(worker, "serve_worker", service)
    worker.run_user_worker(profile=profile)
    now[0] = 99.0
    worker.run_user_worker(profile=profile)
    assert allowed == [True, False]
    assert worker.read_worker_state(profile)["reason"] == "restart_clock_rollback"
    now[0] = 161.0
    worker.run_user_worker(profile=profile)
    assert allowed == [True, False, True]
    assert worker.read_worker_state(profile)["starts"] == [161.0]
    assert "reason" not in worker.read_worker_state(profile)


def test_worker_receipt_refuses_symlink_without_reading_target(short_profile_root):
    worker = importlib.import_module("ledger_memory.embedding_worker")
    profile = worker.worker_profile(root=short_profile_root / "profile")
    worker.prepare_worker_profile(profile)
    target = short_profile_root / "target"
    target.write_bytes(b'{"state":"warm"}')
    (profile.root / "worker.json").symlink_to(target)
    with pytest.raises(worker.WorkerProtocolError, match="single-link regular"):
        worker.read_worker_state(profile)
    assert target.read_bytes() == b'{"state":"warm"}'


def test_worker_state_inspection_creates_nothing(short_profile_root):
    worker = importlib.import_module("ledger_memory.embedding_worker")
    profile = worker.worker_profile(root=short_profile_root / "absent")
    assert worker.read_worker_state(profile) == {}
    assert not profile.root.exists()


@pytest.mark.parametrize("value", ["0", "-1", "inf", "1.5", "86401"])
def test_worker_idle_override_is_bounded_before_startup(short_profile_root, monkeypatch, value):
    worker = importlib.import_module("ledger_memory.embedding_worker")
    profile = worker.worker_profile(root=short_profile_root / "absent")
    monkeypatch.setenv("LEDGER_WORKER_IDLE_SECONDS", value)
    monkeypatch.setattr(worker, "serve_worker", lambda *a, **k: pytest.fail("invalid idle value started service"))
    with pytest.raises(worker.WorkerProtocolError, match="LEDGER_WORKER_IDLE_SECONDS"):
        worker.run_user_worker(profile=profile)
    assert not profile.root.exists()


def test_worker_idle_override_reaches_supervisor(short_profile_root, monkeypatch):
    worker = importlib.import_module("ledger_memory.embedding_worker")
    profile = worker.worker_profile(root=short_profile_root / "profile")
    monkeypatch.setenv("LEDGER_WORKER_IDLE_SECONDS", "45")
    observed = []
    def service(address, **options):
        observed.append(options["idle_seconds"])
        return {"completed": 0}
    monkeypatch.setattr(worker, "serve_worker", service)
    worker.run_user_worker(profile=profile)
    assert observed == [45]


def test_clean_startup_failure_does_not_permanently_block_restart(short_profile_root, monkeypatch):
    worker = importlib.import_module("ledger_memory.embedding_worker")
    profile = worker.worker_profile(root=short_profile_root / "profile")
    def fail(*args, **kwargs):
        raise RuntimeError("synthetic clean startup failure")
    monkeypatch.setattr(worker, "serve_worker", fail)
    with pytest.raises(RuntimeError, match="clean startup"):
        worker.run_user_worker(profile=profile)
    assert worker.read_worker_state(profile)["state"] == "degraded"
    monkeypatch.setattr(worker, "serve_worker", lambda *a, **k: {"completed": 0})
    assert worker.run_user_worker(profile=profile) == {"completed": 0}


def test_incomplete_teardown_blocks_next_service_entry(short_profile_root, monkeypatch):
    worker = importlib.import_module("ledger_memory.embedding_worker")
    profile = worker.worker_profile(root=short_profile_root / "profile")
    def fail(*args, **kwargs):
        raise worker.WorkerTeardownError("synthetic owned child unreaped")
    monkeypatch.setattr(worker, "serve_worker", fail)
    with pytest.raises(worker.WorkerProtocolError, match="unreaped"):
        worker.run_user_worker(profile=profile)
    assert worker.read_worker_state(profile)["state"] == "blocked"
    monkeypatch.setattr(worker, "serve_worker", lambda *a, **k: pytest.fail("blocked entry started service"))
    with pytest.raises(worker.WorkerProtocolError, match="operator review"):
        worker.run_user_worker(profile=profile)


def test_startup_reuses_only_authenticated_service(short_profile_root, monkeypatch):
    worker = importlib.import_module("ledger_memory.embedding_worker")
    launcher = importlib.import_module("ledger_memory.worker_spawn")
    profile = worker.worker_profile(root=short_profile_root / "profile")
    key = worker.prepare_worker_profile(profile)
    calls = []
    def request(address, **options):
        assert address == profile.address
        assert options["authkey"] == key
        assert options["texts"] == []
        calls.append(options["model_fingerprint"])
        return []
    monkeypatch.setattr(worker, "worker_request", request)
    monkeypatch.setattr(launcher, "spawn_embedding_worker", lambda: pytest.fail("authenticated service was replaced"))
    assert worker.ensure_worker(profile=profile, timeout=2) == profile
    assert len(calls) == 1


def test_probe_failure_does_not_replace_lock_owning_service(short_profile_root, monkeypatch):
    worker = importlib.import_module("ledger_memory.embedding_worker")
    launcher = importlib.import_module("ledger_memory.worker_spawn")
    from ledger_memory.locking import exclusive
    profile = worker.worker_profile(root=short_profile_root / "profile")
    worker.prepare_worker_profile(profile)
    locked, release = threading.Event(), threading.Event()
    def hold():
        with exclusive(profile.root / "service.lock", timeout=1):
            locked.set()
            release.wait(3)
    thread = threading.Thread(target=hold)
    thread.start()
    try:
        assert locked.wait(1)
        def refuse(*a, **k):
            raise worker.WorkerProtocolError("synthetic stalled service")
        monkeypatch.setattr(worker, "worker_request", refuse)
        monkeypatch.setattr(launcher, "spawn_embedding_worker", lambda: pytest.fail("live owner replaced after timeout"))
        with pytest.raises(worker.WorkerProtocolError, match="deadline"):
            worker.ensure_worker(profile=profile, timeout=0.15)
    finally:
        release.set()
        thread.join(2)
    assert not thread.is_alive()


def test_startup_uses_fixed_launcher_then_authenticates(short_profile_root, monkeypatch):
    worker = importlib.import_module("ledger_memory.embedding_worker")
    launcher = importlib.import_module("ledger_memory.worker_spawn")
    profile = worker.worker_profile(root=short_profile_root / "profile")
    spawned, authenticated = [], []
    def request(address, **options):
        if not spawned:
            raise worker.WorkerProtocolError("synthetic no endpoint")
        authenticated.append(True)
        return []
    monkeypatch.setattr(worker, "worker_request", request)
    monkeypatch.setattr(launcher, "spawn_embedding_worker", lambda: spawned.append(True) or 123)
    assert worker.ensure_worker(profile=profile, timeout=2) == profile
    assert spawned == [True]
    assert authenticated == [True]


def test_startup_recovers_when_previous_owner_exits_mid_wait(short_profile_root, monkeypatch):
    worker = importlib.import_module("ledger_memory.embedding_worker")
    launcher = importlib.import_module("ledger_memory.worker_spawn")
    from ledger_memory.locking import exclusive
    profile = worker.worker_profile(root=short_profile_root / "profile")
    worker.prepare_worker_profile(profile)
    held, release = threading.Event(), threading.Event()
    spawned, probes = [], []
    def owner():
        with exclusive(profile.root / "service.lock", timeout=1):
            held.set()
            release.wait(3)
    thread = threading.Thread(target=owner)
    thread.start()
    try:
        assert held.wait(1)
        def request(*args, **kwargs):
            probes.append(True)
            if len(probes) == 2:
                release.set()
                thread.join(1)
                assert not thread.is_alive()
                with exclusive(profile.root / "service.lock", timeout=0):
                    pass  # Positive proof that the measured ownership has changed.
            if not spawned:
                raise worker.WorkerProtocolError("synthetic endpoint unavailable")
            return []
        monkeypatch.setattr(worker, "worker_request", request)
        monkeypatch.setattr(launcher, "spawn_embedding_worker", lambda: spawned.append(True) or 123)
        assert worker.ensure_worker(profile=profile, timeout=0.5) == profile
        assert spawned == [True]
        assert len(probes) >= 3
    finally:
        release.set()
        thread.join(2)
    assert not thread.is_alive()


def test_startup_launch_cap_backoff_and_total_deadline(short_profile_root, monkeypatch):
    worker = importlib.import_module("ledger_memory.embedding_worker")
    launcher = importlib.import_module("ledger_memory.worker_spawn")
    profile = worker.worker_profile(root=short_profile_root / "profile")
    clock, launches = [0.0], []
    monkeypatch.setattr(worker.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(worker.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    def refuse(*args, **kwargs):
        raise worker.WorkerProtocolError("synthetic never-ready service")
    monkeypatch.setattr(worker, "worker_request", refuse)
    monkeypatch.setattr(launcher, "spawn_embedding_worker", lambda: launches.append(clock[0]) or 123)
    with pytest.raises(worker.WorkerProtocolError, match="deadline"):
        worker.ensure_worker(profile=profile, timeout=4.1)
    assert len(launches) == 3
    assert all(later - earlier >= 1 for earlier, later in zip(launches, launches[1:]))
    assert clock[0] == pytest.approx(4.1)


def test_oversized_frame_refused_before_decode(monkeypatch):
    worker = importlib.import_module("ledger_memory.embedding_worker")
    def refuse(*args, **kwargs):
        pytest.fail("oversized frame reached JSON deserialization")
    monkeypatch.setattr(worker.json, "loads", refuse)
    with pytest.raises(worker.WorkerProtocolError, match="frame"):
        worker.decode_request(b"x" * (1024 * 1024 + 1), expected_model="synthetic-model")
    seen = []
    class Connection:
        def recv_bytes(self, *, maxlength):
            seen.append(maxlength)
            raise OSError("synthetic oversized frame refused by transport")
    with pytest.raises(worker.WorkerProtocolError):
        worker.receive_request(Connection(), expected_model="synthetic-model")
    assert seen == [1024 * 1024]


@pytest.mark.parametrize("texts", [["x"] * 33, ["x" * 65537], [1], "not-a-list"])
def test_text_and_count_limits_refused(texts):
    worker = importlib.import_module("ledger_memory.embedding_worker")
    raw = json.dumps({"model_fingerprint": "synthetic-model", "texts": texts}).encode()
    with pytest.raises(worker.WorkerProtocolError):
        worker.decode_request(raw, expected_model="synthetic-model")


def test_request_roundtrip_and_model_binding():
    worker = importlib.import_module("ledger_memory.embedding_worker")
    raw = json.dumps({"model_fingerprint": "synthetic-model", "texts": ["hello", "ș🙂"]}).encode()
    assert worker.decode_request(raw, expected_model="synthetic-model") == ["hello", "ș🙂"]
    with pytest.raises(worker.WorkerProtocolError, match="model"):
        worker.decode_request(raw, expected_model="different-model")
    duplicate = b'{"model_fingerprint":"synthetic-model","texts":[],"texts":["hidden"]}'
    with pytest.raises(worker.WorkerProtocolError):
        worker.decode_request(duplicate, expected_model="synthetic-model")


@pytest.mark.parametrize("bad", [[], [[0.0] * 1023], [[float("nan")] * 1024], [[0.0] * 1024], [[True] * 1024]])
def test_response_shape_values_and_norm_refused(bad):
    worker = importlib.import_module("ledger_memory.embedding_worker")
    raw = json.dumps({"model_fingerprint": "synthetic-model", "vectors": bad}).encode()
    with pytest.raises(worker.WorkerProtocolError):
        worker.decode_response(raw, expected_model="synthetic-model", expected_count=1)


def test_response_roundtrip_is_json_not_pickle():
    worker = importlib.import_module("ledger_memory.embedding_worker")
    vectors = [[1.0, *([0.0] * 1023)]]
    raw = worker.encode_response(vectors, model_fingerprint="synthetic-model")
    assert isinstance(raw, bytes)
    assert json.loads(raw)["model_fingerprint"] == "synthetic-model"
    assert worker.decode_response(raw, expected_model="synthetic-model", expected_count=1) == vectors


def _synthetic_endpoint(address, family, key, mode, ready, observed):
    from multiprocessing.connection import Listener
    from multiprocessing import AuthenticationError
    with Listener(address, family=family, authkey=None if mode == "auth-stall" else key) as listener:
        ready.set()
        try:
            connection = listener.accept()
        except AuthenticationError:
            observed.set()
            return
        with connection:
            if mode == "auth-stall":
                observed.set()
                time.sleep(3600)
            elif mode == "partial":
                connection.recv_bytes(maxlength=1024 * 1024)
                os.write(connection.fileno(), struct.pack("!i", 100) + b"x")
                observed.set()
                time.sleep(3600)
            else:
                request = json.loads(connection.recv_bytes(maxlength=1024 * 1024))
                vectors = [[1.0, *([0.0] * 1023)] for _ in request["texts"]]
                connection.send_bytes(json.dumps({"model_fingerprint": request["model_fingerprint"],
                                                 "vectors": vectors}).encode())
                observed.set()


@contextmanager
def _endpoint(mode, key):
    context = multiprocessing.get_context("spawn")
    from ledger_memory.embedding_worker import _Signal
    ready, observed = _Signal(context), _Signal(context)
    with tempfile.TemporaryDirectory(prefix="ledger-ipc-") as directory:
        family = "AF_PIPE" if os.name == "nt" else "AF_UNIX"
        address = r"\\.\pipe\ledger-memory-test-" + uuid.uuid4().hex if os.name == "nt" else directory + "/s"
        process = context.Process(target=_synthetic_endpoint,
                                  args=(address, family, key, mode, ready, observed))
        process.start()
        try:
            assert ready.wait(5), f"synthetic endpoint never became ready; exit={process.exitcode}"
            yield address, family, observed
        finally:
            if process.is_alive():
                process.terminate()
            process.join(timeout=2)
            if process.is_alive():
                process.kill()
                process.join(timeout=2)
            assert not process.is_alive(), "synthetic endpoint was not reaped"
            process.close()


def test_wrong_authkey_refused():
    worker = importlib.import_module("ledger_memory.embedding_worker")
    with _endpoint("reply", b"a" * 32) as (address, family, observed):
        with pytest.raises(worker.WorkerProtocolError, match="auth"):
            worker.worker_request(address, family=family, authkey=b"b" * 32,
                                  model_fingerprint="synthetic-model", texts=["test"], timeout=5)
        assert observed.wait(1), "fixture did not reach the actual authentication gate"


def test_authentication_deadline():
    worker = importlib.import_module("ledger_memory.embedding_worker")
    with _endpoint("auth-stall", b"a" * 32) as (address, family, observed):
        before = time.monotonic()
        with pytest.raises(worker.WorkerProtocolError, match="authentication.*deadline"):
            worker.worker_request(address, family=family, authkey=b"a" * 32,
                model_fingerprint="synthetic-model", texts=["test"], timeout=5, authentication_timeout=1)
        assert observed.is_set(), "fixture did not accept the measured connection"
        assert time.monotonic() - before < 5


@pytest.mark.skipif(os.name == "nt", reason="POSIX byte-stream partial-frame fixture; named pipe native acceptance separate")
def test_partial_frame_deadline():
    worker = importlib.import_module("ledger_memory.embedding_worker")
    with _endpoint("partial", b"a" * 32) as (address, family, observed):
        before = time.monotonic()
        with pytest.raises(worker.WorkerProtocolError, match="deadline"):
            worker.worker_request(address, family=family, authkey=b"a" * 32,
                model_fingerprint="synthetic-model", texts=["test"], timeout=2, authentication_timeout=1)
        assert observed.is_set(), "fixture did not send the measured partial frame"
        assert time.monotonic() - before < 6


def test_client_receives_validated_vectors():
    worker = importlib.import_module("ledger_memory.embedding_worker")
    with _endpoint("reply", b"a" * 32) as (address, family, observed):
        result = worker.worker_request(address, family=family, authkey=b"a" * 32,
            model_fingerprint="synthetic-model", texts=["test", "second"], timeout=5)
        assert observed.wait(1)
        assert result == [[1.0, *([0.0] * 1023)]] * 2


class _SyntheticModel:
    dimensions = 1024
    model_name = "synthetic-model"

    def __init__(self, entered, delay, warm_failure=False):
        self.entered, self.delay = entered, delay
        self.warm_failure = warm_failure

    def embed(self, texts):
        if self.warm_failure and texts == ["warmup"]:
            raise RuntimeError("synthetic warm inference failure")
        if texts != ["warmup"]:
            self.entered.set()
            if self.delay:
                time.sleep(self.delay)
        return [[float(index == ord(text[0]) % 1024) for index in range(1024)] for text in texts]


def _synthetic_model_factory(root, loads, entered, delay, crash=False, warm_failure=False,
                             fail_first=False, load_gate=None):
    import sqlite3
    sqlite3.connect = _refuse_store_access
    # Only one model child exists at a time; do not put a killable owner's lock in the probe.
    loads.value += 1
    if load_gate is not None and not load_gate.wait(5):
        raise RuntimeError("synthetic model load gate timed out")
    if crash:
        raise RuntimeError("synthetic model load failure")
    return _SyntheticModel(entered, delay, warm_failure or (fail_first and loads.value == 1))


@pytest.mark.parametrize("abrupt", [False, True])
def test_model_lifetime_lock_prevents_second_load_until_first_child_exits(short_profile_root, abrupt):
    worker = importlib.import_module("ledger_memory.embedding_worker")
    context = multiprocessing.get_context("spawn")
    lock_path = short_profile_root / "model.lock"
    loads = context.RawValue("i", 0)
    entered = worker._Signal(context)
    factory = functools.partial(_synthetic_model_factory, loads=loads, entered=entered, delay=0)
    controls = [SimpleNamespace(warm_ready=worker._Signal(context), stop=worker._Signal(context),
        job_ready=worker._Signal(context), status=context.RawValue("i", 0)) for _ in range(3)]
    children = []
    try:
        for control in controls[:2]:
            child = context.Process(target=worker._model_loop,
                args=("synthetic", "synthetic-model", [], control, factory, lock_path))
            children.append(child)
            child.start()
            assert control.warm_ready.wait(5), f"model failed to report startup: {child.exitcode}"
        assert controls[0].status.value == 1
        assert controls[1].status.value == 3
        children[1].join(2)
        assert not children[1].is_alive()
        assert children[0].is_alive()
        assert loads.value == 1, "second model factory ran while first retained its OS lock"
        if abrupt:
            children[0].kill()
        else:
            controls[0].stop.set()
        children[0].join(3)
        assert not children[0].is_alive()
        third = context.Process(target=worker._model_loop,
            args=("synthetic", "synthetic-model", [], controls[2], factory, lock_path))
        children.append(third)
        third.start()
        assert controls[2].warm_ready.wait(5)
        assert controls[2].status.value == 1
        assert loads.value == 2, "OS lock was not released after actual child exit"
    finally:
        for control in controls:
            control.stop.set()
        for child in children:
            worker._stop_child(child)


def _service_main(address, family, key, factory, ready, shutdown, options):
    import sqlite3
    sqlite3.connect = _refuse_store_access
    from ledger_memory import embedding_worker as worker
    poison = options.pop("_poison_marker", None)
    if poison is not None:
        worker._server_transport = functools.partial(_poison_completion_transport, marker=poison)
    worker.serve_worker(address, family=family, authkey=key, model_fingerprint="synthetic-model",
                 model_root="synthetic-unused-root", model_factory=factory,
                 ready=ready, shutdown=shutdown, **options)


def _refuse_store_access(*args, **kwargs):
    raise AssertionError("embedding service attempted to open a store")


@contextmanager
def _service(*, delay=0, idle_seconds=0.5, request_timeout=3, crash=False, poison_done=False,
             warm_signal=None, warm_failure=False, fail_first=False, load_gate=None):
    context = multiprocessing.get_context("spawn")
    from ledger_memory.embedding_worker import _Signal
    ready, shutdown, entered = _Signal(context), _Signal(context), _Signal(context)
    loads = context.RawValue("i", 0)
    factory = functools.partial(_synthetic_model_factory, loads=loads, entered=entered, delay=delay,
                                crash=crash, warm_failure=warm_failure, fail_first=fail_first,
                                load_gate=load_gate)
    with tempfile.TemporaryDirectory(prefix="ledger-svc-") as directory:
        family = "AF_PIPE" if os.name == "nt" else "AF_UNIX"
        address = r"\\.\pipe\ledger-memory-test-" + uuid.uuid4().hex if os.name == "nt" else directory + "/s"
        options = dict(idle_seconds=idle_seconds, request_timeout=request_timeout, authentication_timeout=1)
        if warm_signal is not None:
            options["on_warm"] = warm_signal.set
        marker = context.RawValue("i", 0)
        if poison_done:
            options["_poison_marker"] = marker
        process = context.Process(target=_service_main,
            args=(address, family, b"a" * 32, factory, ready, shutdown, options))
        process.poison_observed = marker
        process.start()
        try:
            assert ready.wait(5), f"service did not bind its synthetic endpoint; exit={process.exitcode}"
            yield address, family, entered, loads, process
        finally:
            shutdown.set()
            process.join(timeout=5)
            if process.is_alive():
                process.terminate()
                process.join(timeout=2)
            if process.is_alive():
                process.kill()
                process.join(timeout=2)
            assert not process.is_alive(), "synthetic service did not exit"
            process.close()


def test_concurrent_clients_receive_only_their_vectors():
    worker = importlib.import_module("ledger_memory.embedding_worker")
    assert hasattr(worker, "serve_worker"), "shared service supervisor is missing"
    with _service(idle_seconds=2) as (address, family, entered, loads, process):
        def request(text):
            return worker.worker_request(address, family=family, authkey=b"a" * 32,
                model_fingerprint="synthetic-model", texts=[text], timeout=5)
        with ThreadPoolExecutor(max_workers=2) as pool:
            first, second = pool.submit(request, "alpha"), pool.submit(request, "zeta")
            a, z = first.result(timeout=6), second.result(timeout=6)
        assert entered.is_set()
        assert a[0][ord("a")] == 1 and z[0][ord("z")] == 1
        assert a != z
        assert loads.value == 1, "clients loaded multiple model instances"


def test_empty_probe_verifies_warm_model_without_inference():
    from ledger_memory import embedding_worker as worker
    with _service(idle_seconds=5) as (address, family, entered, loads, process):
        result = worker.worker_request(address, family=family, authkey=b"a" * 32,
            model_fingerprint="synthetic-model", texts=[], timeout=5, probe_only=True)
        assert result == []
        assert loads.value == 1
        assert not entered.is_set(), "health probe invoked model inference"
        vectors = worker.worker_request(address, family=family, authkey=b"a" * 32,
            model_fingerprint="synthetic-model", texts=["alpha"], timeout=5)
        assert vectors[0][ord("a")] == 1
        assert entered.is_set(), "inference observer cannot see real requests"


def test_empty_probe_does_not_restart_failed_model():
    from ledger_memory import embedding_worker as worker
    with _service(idle_seconds=5, warm_failure=True) as (address, family, entered, loads, process):
        for _ in range(2):
            with pytest.raises(worker.WorkerProtocolError):
                worker.worker_request(address, family=family, authkey=b"a" * 32,
                    model_fingerprint="synthetic-model", texts=[], timeout=5, probe_only=True)
        assert loads.value == 1, "health probe restarted the failed model"
        assert not entered.is_set()


def test_enabled_startup_recovers_after_passive_probe_reports_failed_model(tmp_path, monkeypatch):
    from ledger_memory import embedding_worker as worker, model_artifacts
    with _service(idle_seconds=5, fail_first=True) as (address, family, entered, loads, process):
        with pytest.raises(worker.WorkerProtocolError):
            worker.worker_request(address, family=family, authkey=b"a" * 32,
                model_fingerprint="synthetic-model", texts=[], timeout=5, probe_only=True)
        assert loads.value == 1
        profile = worker.WorkerProfile(tmp_path, tmp_path / "unused-key", address, family)
        monkeypatch.setattr(worker, "prepare_worker_profile", lambda *args, **kwargs: b"a" * 32)
        monkeypatch.setattr(model_artifacts, "_manifest", lambda value: (None, "synthetic-model"))
        assert worker.ensure_worker(profile=profile, timeout=5) == profile
        assert loads.value == 2, "explicit enabled startup did not recover failed generation"
        assert not entered.is_set(), "startup added non-warm inference"
        vectors = worker.worker_request(address, family=family, authkey=b"a" * 32,
            model_fingerprint="synthetic-model", texts=["alpha"], timeout=5)
        assert vectors[0][ord("a")] == 1
        assert entered.is_set()


def test_lifecycle_observes_actual_warming_then_ready_without_extra_inference():
    from ledger_memory import embedding_worker as worker
    gate = worker._Signal(multiprocessing.get_context("spawn"))
    with _service(idle_seconds=5, load_gate=gate, request_timeout=5) as (address, family, entered, loads, process):
        try:
            assert worker.worker_lifecycle(address, family=family, authkey=b"a" * 32,
                model_fingerprint="synthetic-model") == "warming"
            assert not entered.is_set()
        finally:
            gate.set()
        assert worker.worker_request(address, family=family, authkey=b"a" * 32,
            model_fingerprint="synthetic-model", texts=[], timeout=5, probe_only=True) == []
        assert worker.worker_lifecycle(address, family=family, authkey=b"a" * 32,
            model_fingerprint="synthetic-model") == "ready"
        assert loads.value == 1
        assert not entered.is_set()


def test_lifecycle_unavailable_does_not_restart_failed_generation():
    from ledger_memory import embedding_worker as worker
    with _service(idle_seconds=5, warm_failure=True) as (address, family, entered, loads, process):
        with pytest.raises(worker.WorkerProtocolError):
            worker.worker_request(address, family=family, authkey=b"a" * 32,
                model_fingerprint="synthetic-model", texts=[], timeout=5, probe_only=True)
        assert worker.worker_lifecycle(address, family=family, authkey=b"a" * 32,
            model_fingerprint="synthetic-model") == "unavailable"
        assert loads.value == 1
        assert not entered.is_set()


def test_lifecycle_observation_does_not_queue_behind_inference():
    from ledger_memory import embedding_worker as worker
    with _service(idle_seconds=5, delay=2, request_timeout=5) as (address, family, entered, loads, process):
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(worker.worker_request, address, family=family, authkey=b"a" * 32,
                model_fingerprint="synthetic-model", texts=["alpha"], timeout=5)
            assert entered.wait(3)
            assert worker.worker_lifecycle(address, family=family, authkey=b"a" * 32,
                model_fingerprint="synthetic-model") == "ready"
            assert not future.done(), "fixture did not hold inference during observation"
            assert future.result(timeout=5)[0][ord("a")] == 1


@pytest.mark.parametrize("value", [
    {"model_fingerprint": "wrong", "state": "ready"},
    {"model_fingerprint": "synthetic-model", "state": "invented"},
    {"model_fingerprint": "synthetic-model", "state": "ready", "extra": True},
])
def test_lifecycle_reply_refuses_unbound_unknown_or_extra_fields(value):
    import json
    from ledger_memory import embedding_worker as worker
    with pytest.raises(worker.WorkerProtocolError):
        worker.decode_lifecycle_response(json.dumps(value).encode(), expected_model="synthetic-model")


@pytest.mark.parametrize("operation,texts", [("status", ["text"]), ("lifecycle", ["text"]),
                                            ("restart", []), (None, [])])
def test_status_operation_refuses_other_operations_or_corpus(operation, texts):
    import json
    from ledger_memory import embedding_worker as worker
    raw = json.dumps({"model_fingerprint": "synthetic-model", "texts": texts,
                      "operation": operation}).encode()
    with pytest.raises(worker.WorkerProtocolError):
        worker.decode_request(raw, expected_model="synthetic-model")


@pytest.mark.parametrize("crash,warm_failure", [(False, False), (True, False), (False, True)])
def test_warm_callback_requires_successful_model_inference(crash, warm_failure):
    worker = importlib.import_module("ledger_memory.embedding_worker")
    warmed = worker._Signal(multiprocessing.get_context("spawn"))
    with _service(crash=crash, warm_failure=warm_failure, idle_seconds=2, request_timeout=2,
                  warm_signal=warmed) as (address, family, entered, loads, process):
        if crash or warm_failure:
            with pytest.raises(worker.WorkerProtocolError):
                worker.worker_request(address, family=family, authkey=b"a" * 32,
                    model_fingerprint="synthetic-model", texts=[], timeout=4)
            assert loads.value > 0, "failing-model arm never attempted an actual load"
            assert not warmed.is_set()
        else:
            assert worker.worker_request(address, family=family, authkey=b"a" * 32,
                model_fingerprint="synthetic-model", texts=[], timeout=4) == []
            assert warmed.is_set()
            assert loads.value == 1


def test_idle_exit_and_restart():
    worker = importlib.import_module("ledger_memory.embedding_worker")
    assert hasattr(worker, "serve_worker"), "shared service supervisor is missing"
    for _ in range(2):
        with _service(idle_seconds=0.3) as (address, family, entered, loads, process):
            response = worker.worker_request(address, family=family, authkey=b"a" * 32,
                model_fingerprint="synthetic-model", texts=["test"], timeout=5)
            assert response[0][ord("t")] == 1
            process.join(timeout=3)
            assert not process.is_alive(), "idle service ignored its exit interval"
            assert process.exitcode == 0
            assert loads.value == 1


def test_inflight_request_is_not_idle():
    worker = importlib.import_module("ledger_memory.embedding_worker")
    assert hasattr(worker, "serve_worker"), "shared service supervisor is missing"
    with _service(delay=0.8, idle_seconds=0.2) as (address, family, entered, loads, process):
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(worker.worker_request, address, family=family,
                authkey=b"a" * 32, model_fingerprint="synthetic-model", texts=["test"], timeout=5)
            assert entered.wait(3), "model did not reach measured in-flight work"
            time.sleep(0.4)
            assert process.is_alive(), "idle timer killed active inference"
            assert future.result(timeout=5)[0][ord("t")] == 1


def test_inference_timeout_kills_model_child():
    worker = importlib.import_module("ledger_memory.embedding_worker")
    assert hasattr(worker, "serve_worker"), "shared service supervisor is missing"
    with _service(delay=30, idle_seconds=1, request_timeout=1) as (address, family, entered, loads, process):
        before = time.monotonic()
        with pytest.raises(worker.WorkerProtocolError):
            worker.worker_request(address, family=family, authkey=b"a" * 32,
                model_fingerprint="synthetic-model", texts=["stall"], timeout=5)
        assert entered.is_set(), "fixture never reached actual model inference"
        assert time.monotonic() - before < 5


def test_crash_restarts_are_bounded():
    worker = importlib.import_module("ledger_memory.embedding_worker")
    with _service(crash=True, idle_seconds=2) as (address, family, entered, loads, process):
        for _ in range(2):
            with pytest.raises(worker.WorkerProtocolError):
                worker.worker_request(address, family=family, authkey=b"a" * 32,
                    model_fingerprint="synthetic-model", texts=["test"], timeout=5)
            assert loads.value == 3, "restart budget did not stop at three model starts"
        assert not entered.is_set(), "failed model reached inference unexpectedly"


def test_service_reclaims_authentication_stalls():
    from multiprocessing.connection import Client
    worker = importlib.import_module("ledger_memory.embedding_worker")
    with _service(idle_seconds=3, request_timeout=5) as (address, family, entered, loads, process):
        connections = []
        try:
            for _ in range(4):
                connection = Client(address, family=family, authkey=None)
                connections.append(connection)
                assert connection.poll(2), "server never reached authentication challenge"
                challenge = connection.recv_bytes(maxlength=256)
                assert challenge.startswith(b"#CHALLENGE#"), "fixture did not occupy an authenticating slot"
            time.sleep(1.2)
            result = worker.worker_request(address, family=family, authkey=b"a" * 32,
                model_fingerprint="synthetic-model", texts=["test"], timeout=1, authentication_timeout=0.8)
            assert result[0][ord("t")] == 1
            assert entered.is_set()
        finally:
            for connection in connections:
                connection.close()


def test_worker_never_opens_store_or_logs_text(capfd):
    worker = importlib.import_module("ledger_memory.embedding_worker")
    canary = "synthetic-private-text-canary-1469"
    with _service(idle_seconds=2) as (address, family, entered, loads, process):
        response = worker.worker_request(address, family=family, authkey=b"a" * 32,
            model_fingerprint="synthetic-model", texts=[canary], timeout=5)
        assert response[0][ord(canary[0])] == 1
        assert entered.is_set() and loads.value == 1
    output = capfd.readouterr()
    assert canary not in output.out
    assert canary not in output.err


@pytest.mark.skipif(os.name == "nt", reason="POSIX partial-byte-stream fixture; native pipe acceptance separate")
def test_service_reclaims_partial_frame_stalls():
    from multiprocessing.connection import Client
    worker = importlib.import_module("ledger_memory.embedding_worker")
    with _service(idle_seconds=3, request_timeout=2) as (address, family, entered, loads, process):
        connections = []
        try:
            for _ in range(4):
                connection = Client(address, family=family, authkey=b"a" * 32)
                connections.append(connection)
                os.write(connection.fileno(), struct.pack("!i", 100) + b"x")
            assert not entered.is_set(), "incomplete JSON reached inference"
            time.sleep(2.2)
            result = worker.worker_request(address, family=family, authkey=b"a" * 32,
                model_fingerprint="synthetic-model", texts=["test"], timeout=1, authentication_timeout=0.8)
            assert result[0][ord("t")] == 1
            assert entered.is_set() and loads.value == 1
        finally:
            for connection in connections:
                connection.close()


def _hold_signal_ownership(slot, marker):
    signal = slot.done
    if hasattr(signal, "_cond"):
        signal._cond.acquire()
    else:
        signal.set()
        assert signal._semaphore.acquire(False), "fault injection did not acquire signal token"
    marker.send_bytes(b"held")
    marker.close()
    time.sleep(30)


def test_reaped_signal_owner_does_not_freeze_supervision():
    worker = importlib.import_module("ledger_memory.embedding_worker")
    context = multiprocessing.get_context("spawn")
    slot = worker.make_slot(context)
    receive, send = context.Pipe(duplex=False)
    process = context.Process(target=_hold_signal_ownership, args=(slot, send))
    process.start()
    send.close()
    try:
        assert receive.poll(3), "child never acquired the actual shared signal"
        assert receive.recv_bytes(maxlength=16) == b"held"
        worker._stop_child(process)
    finally:
        receive.close()
        if not process._closed:
            if process.is_alive():
                process.kill()
            process.join(timeout=2)
            process.close()
    finished = threading.Event()
    errors = []
    def clear():
        try:
            worker._clear_slot(slot)
        except Exception as exc:
            errors.append(type(exc).__name__)
        finally:
            finished.set()
    thread = threading.Thread(target=clear, daemon=True)
    thread.start()
    assert finished.wait(0.5), "reaped child left a lock that freezes the supervisor"
    assert errors == [], errors
    slot.done.set()
    assert slot.done.is_set(), "a fresh signal cannot be published after owner death"
    slot.done.clear()
    assert not slot.done.is_set()


def test_finalizer_attempts_every_owned_child_after_one_reap_failure(monkeypatch):
    worker = importlib.import_module("ledger_memory.embedding_worker")
    real_context = multiprocessing.get_context("spawn")
    queued, closed = threading.Event(), threading.Event()
    created, stopped, permissions = [], [], []
    class Connection:
        def close(self): pass
    class Listener:
        count = 0
        def __init__(self, *args, **kwargs): pass
        def accept(self):
            self.count += 1
            if self.count == 1:
                return Connection()
            queued.set()  # First connection is already in the real supervisor queue.
            closed.wait(2)
            raise OSError("synthetic listener closed")
        def close(self): closed.set()
    class Process:
        def __init__(self, target):
            self.kind = "model" if target.__name__ == "_model_loop" else "transport"
            created.append(self.kind)
        def start(self): pass
        def is_alive(self): return True
    class Context:
        def __getattr__(self, name): return getattr(real_context, name)
        def Process(self, *, target, args): return Process(target)
    class Shutdown:
        calls = 0
        def is_set(self):
            self.calls += 1
            assert queued.wait(2), "synthetic listener never queued a connection"
            return self.calls > 1
        def wait(self, timeout): pass
    def stop(process):
        stopped.append(process.kind)
        if process.kind == "transport":
            raise worker.WorkerProtocolError("synthetic unreapable transport")
    monkeypatch.setattr(worker, "Listener", Listener)
    monkeypatch.setattr(worker.os, "chmod", lambda path, mode: permissions.append((path, mode)))
    monkeypatch.setattr(worker.multiprocessing, "get_context", lambda method: Context())
    monkeypatch.setattr(worker, "_stop_child", stop)
    with pytest.raises(worker.WorkerProtocolError):
        worker.serve_worker("synthetic", family="AF_PIPE" if os.name == "nt" else "AF_UNIX",
            authkey=b"a" * 32, model_fingerprint="synthetic-model", model_root="unused",
            shutdown=Shutdown())
    assert created == ["model", "transport"]
    assert stopped == ["transport", "model"], "one failed reap skipped another owned child"
    assert permissions == ([] if os.name == "nt" else [("synthetic", 0o600)])


class _PoisonCompletion:
    def __init__(self, signal, marker):
        self.signal, self.marker = signal, marker

    def set(self):
        if not self.marker.value:
            if hasattr(self.signal, "_cond"):
                self.signal._cond.acquire()
            self.marker.value = os.getpid()
            time.sleep(30)  # Child is killed during completion publication.
        else:
            self.signal.set()


def _poison_completion_transport(connection, authkey, fingerprint, slot, marker):
    from ledger_memory.embedding_worker import _server_transport
    slot.done = _PoisonCompletion(slot.done, marker)
    _server_transport(connection, authkey, fingerprint, slot)


@pytest.mark.skipif(os.name == "nt", reason="POSIX read-only grandchild PID liveness; Windows native acceptance separate")
def test_service_serves_next_request_after_completion_owner_dies():
    worker = importlib.import_module("ledger_memory.embedding_worker")
    with _service(idle_seconds=3, request_timeout=1, poison_done=True) as (address, family, entered, loads, process):
        first = worker.worker_request(address, family=family, authkey=b"a" * 32,
            model_fingerprint="synthetic-model", texts=["alpha"], timeout=5)
        assert first[0][ord("a")] == 1
        until = time.monotonic() + 2
        while not process.poison_observed.value and time.monotonic() < until:
            time.sleep(0.01)
        assert process.poison_observed.value > 0, "completion-owner death path was not reached"
        poisoned_pid = process.poison_observed.value
        def still_alive():
            try:
                os.kill(poisoned_pid, 0)
            except ProcessLookupError:
                return False
            return True
        until = time.monotonic() + 3
        while still_alive() and time.monotonic() < until:
            time.sleep(0.01)
        assert not still_alive(), "completion-publishing transport was not actually reaped on deadline"
        second = worker.worker_request(address, family=family, authkey=b"a" * 32,
            model_fingerprint="synthetic-model", texts=["zeta"], timeout=2, authentication_timeout=1)
        assert second[0][ord("z")] == 1
        assert entered.is_set() and loads.value == 1
