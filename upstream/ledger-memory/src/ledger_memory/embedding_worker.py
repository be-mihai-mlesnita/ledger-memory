"""Bounded JSON transport primitives for the user-local embedding service.

The service supervisor is added separately; importing this module starts nothing.
"""
from __future__ import annotations

import json
import math
import multiprocessing
import os
import time
import queue
import threading
import hashlib
import secrets
import stat
import sys
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from multiprocessing.connection import Client, Listener, deliver_challenge, answer_challenge

MAX_FRAME = 1024 * 1024
MAX_TEXTS = 32
MAX_CHARACTERS = 65536
DIMENSIONS = 1024


class WorkerProtocolError(RuntimeError):
    """Untrusted transport content does not satisfy the embedding contract."""


class EmbeddingServiceUnavailable(WorkerProtocolError):
    """The service could not be reached, started or given its model before any text was sent.

    It says nothing about the text being embedded, so it must never count against a record's retry budget."""


class WorkerTeardownError(WorkerProtocolError):
    """Owned resources could not be reaped; automatic service restart is unsafe."""


@dataclass(frozen=True)
class WorkerProfile:
    root: Path
    key: Path
    address: str
    family: str


def worker_profile(*, root: Path | None = None) -> WorkerProfile:
    """Resolve without creating anything; stores never choose the service identity."""
    if root is None:
        if os.name == "nt":
            base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
        elif sys.platform == "darwin":
            base = Path.home() / "Library" / "Caches"
        else:
            base = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
        root = base / "ledger-memory"
    root = Path(os.path.abspath(root))
    if os.name == "nt":
        identity = hashlib.sha256(os.path.normcase(str(root)).encode()).hexdigest()[:24]
        address, family = "\\\\.\\pipe\\ledger-memory-" + identity, "AF_PIPE"
    else:
        address, family = str(root / "worker.sock"), "AF_UNIX"
        if len(os.fsencode(address)) > 103:
            raise WorkerProtocolError("worker profile path is too long for a local socket")
    return WorkerProfile(root, root / "auth.key", address, family)


def _private_directory(path: Path) -> None:
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or (hasattr(path, "is_junction") and path.is_junction()):
        raise WorkerProtocolError("worker profile must be a real directory")
    if os.name != "nt" and (info.st_uid != os.getuid() or info.st_mode & 0o077):
        raise WorkerProtocolError("worker profile must be owner-private (0700)")


def read_worker_key(profile: WorkerProfile) -> bytes | None:
    """Read only an existing private credential; never repair or rotate it."""
    try:
        _private_directory(profile.root)
    except FileNotFoundError:
        return None
    try:
        info = profile.key.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise WorkerProtocolError("worker key must be a single-link regular file")
    if os.name != "nt" and (info.st_uid != os.getuid() or info.st_mode & 0o077):
        raise WorkerProtocolError("worker key must be owner-private (0600)")
    fd = os.open(profile.key, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                 | getattr(os, "O_BINARY", 0) | getattr(os, "O_NONBLOCK", 0))
    try:
        opened = os.fstat(fd)
        if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
            raise WorkerProtocolError("worker key changed while opening")
        if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1:
            raise WorkerProtocolError("worker key must be a single-link regular file")
        if os.name != "nt" and (opened.st_uid != os.getuid() or opened.st_mode & 0o077):
            raise WorkerProtocolError("worker key must be owner-private (0600)")
        value = os.read(fd, 33)
        if len(value) != 32 or opened.st_size != 32:
            raise WorkerProtocolError("worker key must contain exactly 32 bytes")
        return value
    finally:
        os.close(fd)


def prepare_worker_profile(profile: WorkerProfile, *, timeout: float = 5) -> bytes:
    """Explicit startup boundary; Windows inherits the OS user's profile ACL."""
    from .fs_safety import exclusive_write_bytes
    from .locking import exclusive

    profile.root.mkdir(parents=True, exist_ok=True, mode=0o700)
    _private_directory(profile.root)
    with exclusive(profile.root / "launch.lock", timeout=timeout):
        key = read_worker_key(profile)
        if key is None:
            exclusive_write_bytes(profile.key, secrets.token_bytes(32))
            key = read_worker_key(profile)
        if key is None:
            raise WorkerProtocolError("worker credential creation failed")
        return key


def ensure_worker(*, profile: WorkerProfile | None = None, timeout: float = 120) -> WorkerProfile:
    """Explicit enabled-call boundary: authenticate reuse, or launch under OS locks.

    A failed probe never proves death. A busy service lock means wait for that
    owner, not replace it. The detached entry owns both its service lock and a
    child-retained model lock, so simultaneous launch requests cannot load twice.
    """
    from .locking import exclusive
    from .model_artifacts import _manifest
    from .worker_spawn import spawn_embedding_worker

    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or not 0 < timeout <= 120:
        raise ValueError("worker startup deadline must be positive and at most 120 seconds")
    deadline = time.monotonic() + timeout
    profile = profile if profile is not None else worker_profile()
    key = prepare_worker_profile(profile, timeout=min(5, timeout))
    _, fingerprint = _manifest(None)

    def remaining():
        value = deadline - time.monotonic()
        if value <= 0:
            raise WorkerProtocolError("worker startup deadline exceeded")
        return value

    def probe(budget):
        worker_request(profile.address, family=profile.family, authkey=key,
            model_fingerprint=fingerprint, texts=[], timeout=budget,
            authentication_timeout=min(5, budget))

    try:
        probe(min(5, remaining()))
        return profile
    except WorkerProtocolError:
        pass
    launches = 0
    next_launch = 0.0
    while True:
        if launches < 3 and time.monotonic() >= next_launch:
            with exclusive(profile.root / "launch.lock", timeout=min(5, remaining())):
                if read_worker_state(profile).get("state") == "blocked":
                    raise WorkerProtocolError("worker teardown was incomplete; operator review required")
                try:
                    with exclusive(profile.root / "service.lock", timeout=0):
                        remaining()
                        spawn_embedding_worker()
                        launches += 1
                        next_launch = time.monotonic() + 1
                except TimeoutError:
                    pass  # Ownership, not a failed probe, decides whether to launch.
        # Release launch.lock before the entry prepares its private credential.
        try:
            probe(remaining())
            return profile
        except WorkerProtocolError:
            time.sleep(min(0.05, remaining()))


def read_worker_state(profile: WorkerProfile) -> dict:
    """Content-free maintenance receipt, never a liveness or model-readiness proof."""
    try:
        _private_directory(profile.root)
        path = profile.root / "worker.json"
        info = path.lstat()
    except FileNotFoundError:
        return {}
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise WorkerProtocolError("worker receipt must be a single-link regular file")
    if os.name != "nt" and (info.st_uid != os.getuid() or info.st_mode & 0o077):
        raise WorkerProtocolError("worker receipt must be owner-private")
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                 | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0))
    try:
        opened = os.fstat(fd)
        if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino) or opened.st_size > 16384:
            raise WorkerProtocolError("worker receipt changed or exceeds its limit")
        raw = os.read(fd, 16385)
        if len(raw) > 16384:
            raise WorkerProtocolError("worker receipt exceeds its limit")
        return _decode(raw)
    finally:
        os.close(fd)


def run_user_worker(*, profile: WorkerProfile | None = None):
    """Fixed detached entry: one service lock and a child-retained model lock."""
    from .fs_safety import atomic_replace_via_nofollow_temp
    from .locking import exclusive
    from .model_artifacts import _manifest, model_bundle_path

    profile = profile if profile is not None else worker_profile()
    try:
        idle_seconds = int(os.environ.get("LEDGER_WORKER_IDLE_SECONDS", "300"))
    except ValueError as exc:
        raise WorkerProtocolError("LEDGER_WORKER_IDLE_SECONDS must be an integer from 1 to 86400") from exc
    if not 1 <= idle_seconds <= 86400:
        raise WorkerProtocolError("LEDGER_WORKER_IDLE_SECONDS must be an integer from 1 to 86400")
    key = prepare_worker_profile(profile)
    with exclusive(profile.root / "service.lock", timeout=0):
        state = read_worker_state(profile)
        if state.get("state") == "blocked":
            raise WorkerProtocolError("worker teardown was incomplete; operator review required")
        if profile.family == "AF_UNIX":
            endpoint = Path(profile.address)
            try:
                info = endpoint.lstat()
            except FileNotFoundError:
                info = None
            if info is not None:
                if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
                    raise WorkerProtocolError("worker endpoint must be an owned socket")
                # Only lock ownership proves the previous service no longer owns it.
                endpoint.unlink()
        _, fingerprint = _manifest(None)

        def publish():
            atomic_replace_via_nofollow_temp(profile.root / "worker.json",
                json.dumps(state, sort_keys=True, allow_nan=False).encode())

        def before_start():
            # Durable wall-clock window across supervisor exits. A clock rollback
            # refuses startup rather than granting a new budget; request deadlines
            # remain monotonic. Forward wall-clock corrections expire old entries.
            now = time.time()
            starts = state.get("starts", [])
            if not isinstance(starts, list) or len(starts) > 3 or any(
                type(value) not in (int, float) or not math.isfinite(value) or value < 0
                for value in starts
            ):
                raise WorkerProtocolError("worker restart receipt is invalid")
            if any(value > now for value in starts):
                state.update(state="degraded", reason="restart_clock_rollback")
                publish()
                return False
            starts = [value for value in starts if now - value < 60]
            if len(starts) >= 3:
                state.update(starts=starts, state="degraded", reason="restart_budget_exhausted")
                publish()
                return False
            state.update(starts=[*starts, now], state="warming", model_fingerprint=fingerprint)
            state.pop("reason", None)
            publish()
            return True

        def warmed():
            state.update(state="warm", warm_fingerprint=fingerprint)
            publish()

        try:
            result = serve_worker(profile.address, family=profile.family, authkey=key,
                model_fingerprint=fingerprint, model_root=model_bundle_path(profile.root),
                model_lock_path=profile.root / "model.lock", before_model_start=before_start,
                on_warm=warmed, idle_seconds=idle_seconds)
        except WorkerTeardownError:
            state.update(state="blocked", reason="service_teardown_requires_review")
            publish()
            raise
        except BaseException:
            state.update(state="degraded", reason="service_failed")
            publish()
            raise
        if state.get("state") == "warm":
            state["state"] = "idle"
            publish()
        return result


class _Signal:
    """Ownerless kernel notifications: a killed child cannot poison a mutex.

    Acquire/release also publishes shared-frame writes across processes. Losing
    a token to process death may lose that notification, but never blocks the
    parent's bounded deadline loop; the next generation can still be signaled.
    """

    def __init__(self, context):
        self._semaphore = context.Semaphore(0)

    def set(self):
        self._semaphore.release()

    def clear(self):
        for _ in range(1024):
            if not self._semaphore.acquire(False):
                return
        raise WorkerProtocolError("worker signal overflow")

    def is_set(self):
        if not self._semaphore.acquire(False):
            return False
        self._semaphore.release()
        return True

    def wait(self, timeout=None):
        acquired = self._semaphore.acquire(timeout=timeout)
        if acquired:
            self._semaphore.release()
        return acquired


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON field")
        result[key] = value
    return result


def _constant(value):
    raise ValueError("nonfinite JSON number")


def _decode(raw: bytes) -> dict:
    if not isinstance(raw, bytes) or len(raw) > MAX_FRAME:
        raise WorkerProtocolError("worker frame exceeds the byte limit")
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_object, parse_constant=_constant)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise WorkerProtocolError("worker frame is not valid JSON") from exc
    if not isinstance(value, dict):
        raise WorkerProtocolError("worker frame must contain an object")
    return value


def decode_request(raw: bytes, *, expected_model: str) -> list[str]:
    value = _decode(raw)
    fields = set(value)
    if fields not in ({"model_fingerprint", "texts"}, {"model_fingerprint", "texts", "operation"}):
        raise WorkerProtocolError("worker request fields are invalid")
    if "operation" in value and (value["operation"] not in ("status", "lifecycle") or value["texts"] != []):
        raise WorkerProtocolError("worker status request is invalid")
    if value["model_fingerprint"] != expected_model:
        raise WorkerProtocolError("worker model fingerprint mismatch")
    texts = value["texts"]
    if not isinstance(texts, list) or len(texts) > MAX_TEXTS:
        raise WorkerProtocolError("worker text count exceeds the limit")
    if any(not isinstance(text, str) or len(text) > MAX_CHARACTERS for text in texts):
        raise WorkerProtocolError("worker text type or character limit is invalid")
    return texts


def receive_request(connection, *, expected_model: str) -> list[str]:
    try:
        raw = connection.recv_bytes(maxlength=MAX_FRAME)
    except (OSError, EOFError) as exc:
        raise WorkerProtocolError("worker frame could not be received within the byte limit") from exc
    return decode_request(raw, expected_model=expected_model)


def encode_request(texts: list[str], *, model_fingerprint: str, probe_only: bool = False) -> bytes:
    if type(probe_only) is not bool:
        raise ValueError("probe_only must be boolean")
    value = {"model_fingerprint": model_fingerprint, "texts": texts}
    if probe_only:
        value["operation"] = "status"
    raw = json.dumps(value,
                     separators=(",", ":"), allow_nan=False).encode("utf-8")
    decode_request(raw, expected_model=model_fingerprint)
    return raw


def decode_response(raw: bytes, *, expected_model: str, expected_count: int) -> list[list[float]]:
    value = _decode(raw)
    if set(value) != {"model_fingerprint", "vectors"}:
        raise WorkerProtocolError("worker response fields are invalid")
    if value["model_fingerprint"] != expected_model:
        raise WorkerProtocolError("worker model fingerprint mismatch")
    vectors = value["vectors"]
    if (type(expected_count) is not int or not 0 <= expected_count <= MAX_TEXTS
            or not isinstance(vectors, list) or len(vectors) != expected_count):
        raise WorkerProtocolError("worker response count mismatch")
    for vector in vectors:
        if not isinstance(vector, list) or len(vector) != DIMENSIONS:
            raise WorkerProtocolError("worker response dimensions mismatch")
        try:
            valid = all(type(number) in (int, float) and math.isfinite(number) for number in vector)
            norm = math.hypot(*vector) if valid else 0
        except (TypeError, OverflowError):
            valid, norm = False, 0
        if not valid or not math.isclose(norm, 1.0, rel_tol=1e-3, abs_tol=1e-3):
            raise WorkerProtocolError("worker response contains invalid vector values or norm")
    return vectors


def encode_response(vectors: list[list[float]], *, model_fingerprint: str) -> bytes:
    try:
        raw = json.dumps({"model_fingerprint": model_fingerprint, "vectors": vectors},
                         separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (ValueError, TypeError, UnicodeError) as exc:
        raise WorkerProtocolError("worker could not encode the response") from exc
    decode_response(raw, expected_model=model_fingerprint, expected_count=len(vectors))
    return raw


def decode_lifecycle_response(raw: bytes, *, expected_model: str) -> str:
    value = _decode(raw)
    if (set(value) != {"model_fingerprint", "state"}
            or value["model_fingerprint"] != expected_model
            or value["state"] not in ("warming", "ready", "unavailable")):
        raise WorkerProtocolError("worker lifecycle response is invalid")
    return value["state"]


def _client_transport(address, family, authkey, request, fingerprint, count,
                      buffer, length, status, authenticated, done):
    connection = None
    try:
        connection = Client(address, family=family, authkey=authkey)
        authenticated.set()
        connection.send_bytes(request)
        raw = connection.recv_bytes(maxlength=MAX_FRAME)
        if _decode(request).get("operation") == "lifecycle":
            decode_lifecycle_response(raw, expected_model=fingerprint)
        else:
            decode_response(raw, expected_model=fingerprint, expected_count=count)
        buffer[:len(raw)] = raw
        length.value = len(raw)
        status.value = 1
    except Exception:
        status.value = 3 if authenticated.is_set() else 2
    finally:
        if connection is not None:
            try:
                connection.close()
            except Exception:
                pass
        done.set()


def _stop_child(process):
    if process.pid is None:
        return
    if process.is_alive():
        process.terminate()
    process.join(timeout=1)
    if process.is_alive():
        process.kill()
        process.join(timeout=1)
    if process.is_alive():
        raise WorkerProtocolError("worker transport child could not be reaped")
    process.close()


def worker_request(address: str, *, authkey: bytes, model_fingerprint: str,
                    texts: list[str], timeout: float = 120,
                    authentication_timeout: float = 5, family: str | None = None,
                    probe_only: bool = False) -> list[list[float]]:
    raw = _exchange(address, authkey=authkey, model_fingerprint=model_fingerprint,
                    texts=texts, timeout=timeout, authentication_timeout=authentication_timeout,
                    family=family, probe_only=probe_only)
    return decode_response(raw, expected_model=model_fingerprint, expected_count=len(texts))


def worker_lifecycle(address: str, *, authkey: bytes, model_fingerprint: str,
                     timeout: float = 2, authentication_timeout: float = 2,
                     family: str | None = None) -> str:
    """Read authenticated supervisor state without waiting for model inference."""
    raw = _exchange(address, authkey=authkey, model_fingerprint=model_fingerprint,
                    texts=[], timeout=timeout, authentication_timeout=authentication_timeout,
                    family=family, observe_lifecycle=True)
    return decode_lifecycle_response(raw, expected_model=model_fingerprint)


def _exchange(address: str, *, authkey: bytes, model_fingerprint: str,
              texts: list[str], timeout: float, authentication_timeout: float,
              family: str | None, probe_only: bool = False,
              observe_lifecycle: bool = False) -> bytes:
    started = time.monotonic()
    for value, maximum in ((timeout, 120), (authentication_timeout, 5)):
        if (isinstance(value, bool) or not isinstance(value, (int, float))
                or not math.isfinite(value) or not 0 < value <= maximum):
            raise ValueError("worker deadlines must be finite positive values within their limits")
    if not isinstance(authkey, bytes) or len(authkey) != 32:
        raise ValueError("worker authentication key must contain 32 bytes")
    if not isinstance(model_fingerprint, str) or not 0 < len(model_fingerprint) <= 256:
        raise ValueError("worker model fingerprint is invalid")
    expected_family = "AF_PIPE" if os.name == "nt" else "AF_UNIX"
    family = family or expected_family
    if family != expected_family:
        raise ValueError("only the platform's local worker transport is allowed")
    raw = encode_request(texts, model_fingerprint=model_fingerprint, probe_only=probe_only)
    if observe_lifecycle:
        raw = json.dumps({"model_fingerprint": model_fingerprint, "texts": [],
                          "operation": "lifecycle"}, separators=(",", ":")).encode("utf-8")
        decode_request(raw, expected_model=model_fingerprint)
    context = multiprocessing.get_context("spawn")
    buffer = context.RawArray("B", MAX_FRAME)
    length, status = context.RawValue("i", 0), context.RawValue("i", 0)
    authenticated, done = _Signal(context), _Signal(context)
    process = context.Process(target=_client_transport, args=(
        address, family, authkey, raw, model_fingerprint, len(texts),
        buffer, length, status, authenticated, done))
    try:
        process.start()
        deadline = started + timeout
        authentication_deadline = started + min(timeout, authentication_timeout)
        while not done.is_set():
            now = time.monotonic()
            if not authenticated.is_set() and now >= authentication_deadline:
                raise WorkerProtocolError("worker authentication deadline exceeded")
            if now >= deadline:
                raise WorkerProtocolError("worker request deadline exceeded")
            if not process.is_alive() and not done.is_set():
                raise WorkerProtocolError("worker transport exited without a response")
            remaining = deadline - now
            if not authenticated.is_set():
                remaining = min(remaining, authentication_deadline - now)
            done.wait(timeout=min(0.02, max(0.0, remaining)))
        if status.value == 2:
            raise WorkerProtocolError("worker authentication failed")
        if status.value != 1:
            raise WorkerProtocolError("worker request or response validation failed")
        if not 0 < length.value <= MAX_FRAME:
            raise WorkerProtocolError("worker response frame length is invalid")
        return bytes(buffer[:length.value])
    finally:
        _stop_child(process)


def make_slot(context):
    return SimpleNamespace(
        input_buffer=context.RawArray("B", MAX_FRAME), output_buffer=context.RawArray("B", MAX_FRAME),
        input_length=context.RawValue("i", 0), output_length=context.RawValue("i", 0),
        error=context.RawValue("i", 0), authenticated=_Signal(context),
        request_ready=_Signal(context), reply_ready=_Signal(context), done=_Signal(context))


def _clear_slot(slot):
    for buffer, length in ((slot.input_buffer, slot.input_length), (slot.output_buffer, slot.output_length)):
        count = min(MAX_FRAME, max(0, length.value))
        buffer[:count] = bytes(count)
        length.value = 0
    slot.error.value = 0
    for event in (slot.authenticated, slot.request_ready, slot.reply_ready, slot.done):
        event.clear()


def _server_transport(connection, authkey, fingerprint, slot):
    try:
        deliver_challenge(connection, authkey)
        answer_challenge(connection, authkey)
        slot.authenticated.set()
        raw = connection.recv_bytes(maxlength=MAX_FRAME)
        decode_request(raw, expected_model=fingerprint)
        slot.input_buffer[:len(raw)] = raw
        slot.input_length.value = len(raw)
        slot.request_ready.set()
        slot.reply_ready.wait()
        if slot.error.value:
            connection.send_bytes(b'{"error":"unavailable"}')
        elif 0 < slot.output_length.value <= MAX_FRAME:
            connection.send_bytes(bytes(slot.output_buffer[:slot.output_length.value]))
    except Exception:
        pass
    finally:
        try:
            connection.close()
        except Exception:
            pass
        finally:
            slot.done.set()


def _model_loop(root, fingerprint, slots, control, model_factory, model_lock_path=None):
    # The model itself retains this lock, even if its supervisor dies. A new
    # supervisor must not infer orphan death from a failed endpoint probe or PID.
    from contextlib import nullcontext
    from .locking import exclusive

    try:
        lock = exclusive(model_lock_path, timeout=0) if model_lock_path is not None else nullcontext()
        with lock:
            _run_model(root, fingerprint, slots, control, model_factory)
    except (OSError, TimeoutError):
        control.status.value = 3
        control.warm_ready.set()


def _run_model(root, fingerprint, slots, control, model_factory):
    try:
        model = model_factory(root)
        if model.dimensions != DIMENSIONS or model.model_name.rsplit(":", 1)[-1] != fingerprint:
            raise WorkerProtocolError("model identity mismatch")
        warm = encode_response(model.embed(["warmup"]), model_fingerprint=fingerprint)
        decode_response(warm, expected_model=fingerprint, expected_count=1)
    except Exception:
        control.status.value = 2
        control.warm_ready.set()
        return
    control.status.value = 1
    control.warm_ready.set()
    while not control.stop.is_set():
        if not control.job_ready.wait(0.1):
            continue
        control.job_ready.clear()
        index = control.job_index.value
        if not 0 <= index < len(slots):
            return
        slot = slots[index]
        try:
            if not 0 < slot.input_length.value <= MAX_FRAME:
                raise WorkerProtocolError("model input frame length is invalid")
            texts = decode_request(bytes(slot.input_buffer[:slot.input_length.value]), expected_model=fingerprint)
            raw = encode_response(model.embed(texts), model_fingerprint=fingerprint)
            decode_response(raw, expected_model=fingerprint, expected_count=len(texts))
            slot.output_buffer[:len(raw)] = raw
            slot.output_length.value = len(raw)
        except Exception:
            slot.error.value = 3
        finally:
            control.job_done.set()


def _load_model(root):
    from .adapters.onnx_bge import ONNXBGEBackend
    return ONNXBGEBackend(root)


def serve_worker(address: str, *, family: str, authkey: bytes, model_fingerprint: str,
                 model_root, model_factory=None, ready=None, shutdown=None,
                 idle_seconds: float = 300, request_timeout: float = 120,
                 authentication_timeout: float = 5, model_lock_path=None,
                 before_model_start=None, on_warm=None):
    """Supervise four local transports and one model; no store is ever opened here."""
    if family != ("AF_PIPE" if os.name == "nt" else "AF_UNIX"):
        raise ValueError("worker service requires a local platform transport")
    if not isinstance(authkey, bytes) or len(authkey) != 32:
        raise ValueError("worker authentication key must contain 32 bytes")
    for value, maximum in ((idle_seconds, 86400), (request_timeout, 120), (authentication_timeout, 5)):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 < value <= maximum:
            raise ValueError("invalid worker service deadline")
    context = multiprocessing.get_context("spawn")
    shutdown = shutdown if shutdown is not None else _Signal(context)
    slots = [make_slot(context) for _ in range(4)]
    control = SimpleNamespace(warm_ready=_Signal(context), job_ready=_Signal(context),
        job_done=_Signal(context), stop=_Signal(context), job_index=context.RawValue("i", -1),
        status=context.RawValue("i", 0))
    accepted = queue.Queue(maxsize=4)
    stopping = threading.Event()
    active = {}
    pending = deque()
    starts = deque()
    model = None
    busy = None
    model_started = 0.0
    warm_seen = False
    completed = 0
    last_completed = time.monotonic()
    listener = Listener(address, family=family, authkey=None, backlog=4)
    if family == "AF_UNIX":
        try:
            os.chmod(address, 0o600)
        except BaseException:
            listener.close()
            raise

    def accept_connections():
        while not stopping.is_set():
            try:
                connection = listener.accept()
            except (OSError, EOFError):
                return
            try:
                accepted.put_nowait((connection, time.monotonic()))
            except queue.Full:
                connection.close()

    def stop_model():
        nonlocal model, busy, warm_seen
        if model is not None:
            _stop_child(model)
            model = None
        if busy is not None and busy in active:
            slots[busy].error.value = 3
            slots[busy].reply_ready.set()
        busy = None
        warm_seen = False
        control.job_ready.clear()
        control.job_done.clear()

    def start_model(now):
        nonlocal model, model_started, warm_seen
        while starts and now - starts[0] >= 60:
            starts.popleft()
        if len(starts) >= 3:
            return False
        if before_model_start is not None and not before_model_start():
            return False
        control.status.value = 0
        control.warm_ready.clear()
        control.job_ready.clear()
        control.job_done.clear()
        warm_seen = False
        model = context.Process(target=_model_loop,
            args=(model_root, model_fingerprint, slots, control, model_factory or _load_model,
                  model_lock_path))
        model.start()
        model_started = now
        starts.append(now)
        return True

    threading.Thread(target=accept_connections, daemon=True).start()
    try:
        start_model(time.monotonic())
        if ready is not None:
            ready.set()
        while not shutdown.is_set():
            now = time.monotonic()
            while len(active) < len(slots):
                try:
                    connection, arrival = accepted.get_nowait()
                except queue.Empty:
                    break
                index = next(index for index in range(len(slots)) if index not in active)
                _clear_slot(slots[index])
                process = context.Process(target=_server_transport,
                    args=(connection, authkey, model_fingerprint, slots[index]))
                try:
                    process.start()
                except BaseException:
                    _stop_child(process)
                    raise
                finally:
                    connection.close()
                active[index] = dict(process=process, started=arrival, queued=False)
            if model is not None:
                failed = not model.is_alive() or (control.warm_ready.is_set() and control.status.value != 1)
                expired = not control.warm_ready.is_set() and now - model_started >= request_timeout
                if failed or expired:
                    stop_model()
                    last_completed = now
                elif control.warm_ready.is_set() and not warm_seen:
                    if on_warm is not None:
                        on_warm()
                    warm_seen = True
                    last_completed = now
                elif busy is not None and control.job_done.is_set():
                    slots[busy].reply_ready.set()
                    busy = None
                    control.job_done.clear()
            for index, item in list(active.items()):
                slot = slots[index]
                elapsed = now - item["started"]
                expired = elapsed >= request_timeout or (not slot.authenticated.is_set() and elapsed >= authentication_timeout)
                if expired or slot.done.is_set() or not item["process"].is_alive():
                    if busy == index:
                        stop_model()
                    _stop_child(item["process"])
                    active.pop(index)
                    pending = deque(entry for entry in pending if entry != index)
                    _clear_slot(slot)
                    last_completed = now
                    completed += 1
                elif slot.request_ready.is_set() and not item["queued"]:
                    item["queued"] = True
                    request = _decode(bytes(slot.input_buffer[:slot.input_length.value]))
                    if request.get("operation") == "lifecycle":
                        state = ("unavailable" if model is None or not model.is_alive()
                                 else "ready" if warm_seen else "warming")
                        raw = json.dumps({"model_fingerprint": model_fingerprint, "state": state},
                                         separators=(",", ":")).encode("utf-8")
                        slot.output_buffer[:len(raw)] = raw
                        slot.output_length.value = len(raw)
                        slot.reply_ready.set()
                    else:
                        pending.append(index)
            if pending and model is None:
                # Status observes availability; explicit enabled-work startup may recover.
                retained = deque()
                for index in pending:
                    slot = slots[index]
                    request = _decode(bytes(slot.input_buffer[:slot.input_length.value]))
                    if request.get("operation") != "status":
                        retained.append(index)
                    else:
                        slot.error.value = 3
                        slot.reply_ready.set()
                pending = retained
            if pending and model is None:
                if not start_model(now):
                    for index in pending:
                        slots[index].error.value = 3
                        slots[index].reply_ready.set()
                    pending.clear()
            if pending and busy is None and model is not None and warm_seen:
                index = pending.popleft()
                slot = slots[index]
                texts = decode_request(bytes(slot.input_buffer[:slot.input_length.value]),
                                       expected_model=model_fingerprint)
                if not texts:
                    if model.is_alive():
                        raw = encode_response([], model_fingerprint=model_fingerprint)
                        slot.output_buffer[:len(raw)] = raw
                        slot.output_length.value = len(raw)
                    else:
                        slot.error.value = 3
                    slot.reply_ready.set()
                else:
                    busy = index
                    control.job_index.value = busy
                    control.job_ready.set()
            warming = model is not None and not warm_seen
            if not active and accepted.empty() and not warming and now - last_completed >= idle_seconds:
                break
            shutdown.wait(0.01)
    finally:
        stopping.set()
        failures = []
        try:
            listener.close()
        except BaseException as exc:
            failures.append(exc)
        owned = [item["process"] for item in active.values()]
        if model is not None:
            owned.append(model)
        for process in owned:
            try:
                _stop_child(process)
            except BaseException as exc:
                failures.append(exc)
        while not accepted.empty():
            try:
                connection, _ = accepted.get_nowait()
            except queue.Empty:
                break
            try:
                connection.close()
            except BaseException as exc:
                failures.append(exc)
        if failures:
            raise WorkerTeardownError("worker teardown incomplete; one or more owned resources remain") from failures[0]
    return {"completed": completed}


if __name__ == "__main__":
    try:
        run_user_worker()
    except Exception:
        # Detached worker stdio is inert; never print credential/text/paths.
        raise SystemExit(1)
