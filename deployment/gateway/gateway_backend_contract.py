"""Shared, store-free validation and private state I/O for the Luna gateway.

Secret detection is a conservative heuristic, not proof that text contains no secrets.
POSIX permissions are required; unsupported ACL platforms fail closed.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import math
import os
from pathlib import Path
import re
import secrets
import stat
import unicodedata
from urllib.parse import urlsplit

MAX_JSON = 2 * 1024 * 1024
MAX_PAYLOAD = 16 * 1024
SOURCES = {"user_said", "observed", "inferred", "document"}
LINKABLE = {"fact", "decision", "context_package", "document"}
DEFAULTS = {
    "remember": {"kind": "note", "confidence": None, "document_id": None},
    "revise": {"confidence": None, "document_id": None},
    "dispute": {"conflicts_with": None},
    "record_decision": {"status": "accepted", "fact_id": None},
    "link_records": {"summary": None},
    "create_handover": {"fact_ids": None, "decision_ids": None, "open_questions": None,
                        "risks": None, "next_actions": None},
    "ingest_document": {"uri": None, "media_type": "text/plain"},
}
REQUIRED = {
    "remember": {"statement", "source"},
    "revise": {"fact_id", "statement", "reason", "source"},
    "dispute": {"fact_id", "reason"},
    "record_decision": {"title", "summary", "rationale"},
    "link_records": {"source_type", "source_id", "target_type", "target_id", "relationship"},
    "create_handover": {"objective", "status_summary"},
    "ingest_document": {"text"},
}
SECRET = re.compile(
    r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----|"
    r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}|"
    r"\b(?:sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|AKIA[A-Z0-9]{16})\b|"
    r"\bBearer\s+[A-Za-z0-9._~+/-]{12,}=*|"
    r"\b(?:password|passwd|pwd|api[_ -]?key|client[_ -]?secret|access[_ -]?token|"
    r"refresh[_ -]?token|auth[_ -]?token|token|device[_ -]?code)\b[\"']?\s*[:=]\s*\S+",
    re.IGNORECASE,
)
LABELLED_SECRET = re.compile(
    r"(?<![\w-])[\"'`*\u2018\u2019\u201c\u201d]*"
    r"(?:password|passwd|pwd|passphrase|api[_ -]?key|client[_ -]?secret|"
    r"access[_ -]?token|refresh[_ -]?token|auth[_ -]?token|token|otp|"
    r"one[_ -]?time[_ -]?(?:code|password)|"
    r"(?:auth(?:entication|orization)?|verification|device|sign[- ]in|login)[_ -]+code)"
    r"\b[\"'`*\u2018\u2019\u201c\u201d]*(?:\s*(?P<assignment>[:=])\s*|\s+(?:is|was)\s+)(?=\S)",
    re.IGNORECASE,
)
PROSE_DESCRIPTION = re.compile(
    r"(?:required|optional|generated|encrypted|hashed|redacted|hidden|missing|unknown|"
    r"unavailable|invalid|expired|strong|weak)(?=$|\s|[.,;:!?](?:$|\s))",
    re.IGNORECASE,
)


def require(condition, code="invalid_input"):
    if not condition:
        raise ValueError(code)


def json_bytes(value):
    try:
        return json.dumps(value, sort_keys=True, ensure_ascii=True, allow_nan=False,
                          separators=(",", ":")).encode("utf-8")
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise ValueError("invalid_json") from None


def valid_id(value):
    return type(value) is int and 0 < value <= 9223372036854775807


def text(value, maximum=4000):
    require(type(value) is str and bool(value.strip()) and len(value) <= maximum
            and not any(ord(c) < 32 and c not in "\n\r\t" for c in value))


def document_words(value):
    value = unicodedata.normalize("NFKC", unicodedata.normalize("NFKC", value).casefold())
    return "".join(char if char.isalnum() else " " for char in value).split()


def document_project_words(project):
    if unicodedata.normalize("NFKC", project).casefold().strip() == "global":
        return set()
    required = {word for word in document_words(project) if word not in {"the", "and", "for", "project"}}
    require(required, "query_missing_project")
    return required


def document_query(query, approved_query, project="global"):
    """Constrain keyword fallback to approved terms, without SQL/FTS syntax."""
    text(query, 600)
    text(approved_query, 8258)
    text(project, 256)

    project_words = document_words(project)
    allowed = set(document_words(approved_query) + project_words)
    requested = list(dict.fromkeys(document_words(query)))
    # Only singularizing a trailing plural s is supported, not arbitrary prefixes
    # ("jun" must not stand in for "Juniper", nor "app" for "approval").
    require(requested and all(word in allowed or len(word) >= 3 and word + "s" in allowed
                              for word in requested), "query_outside_scope")
    require(document_project_words(project) <= set(requested), "query_missing_project")
    normalized = " ".join(requested)
    require(normalized and len(normalized) <= 600, "invalid_keyword_query")
    return normalized


def reject_secrets(value):
    if isinstance(value, dict):
        for item in value.values():
            reject_secrets(item)
    elif isinstance(value, list):
        for item in value:
            reject_secrets(item)
    elif isinstance(value, str):
        require(not SECRET.search(value), "possible_secret")
        for match in LABELLED_SECRET.finditer(value):
            # Only unquoted prose after is/was gets an exception. Assignments,
            # quoted passphrases and grouped numeric codes remain blocked in full.
            require(not match["assignment"] and PROSE_DESCRIPTION.match(value, match.end()), "possible_secret")
        # Deliberately conservative: card-like runs, including invalid checksums.
        require(not re.search(r"(?<!\d)(?:\d[ -]?){12,18}\d(?!\d)", value), "possible_card")


def validate_write(operation, payload):
    """Return a fresh canonical argument dictionary; never grant owner impersonation."""
    require(type(operation) is str and operation in REQUIRED)
    require(type(payload) is dict and all(type(k) is str for k in payload))
    require(REQUIRED[operation] <= payload.keys()
            and not payload.keys() - REQUIRED[operation] - DEFAULTS[operation].keys())
    require(len(json_bytes(payload)) <= MAX_PAYLOAD, "payload_too_large")
    args = {**DEFAULTS[operation], **payload}
    for key, value in args.items():
        if value is None:
            require(key in DEFAULTS[operation] and DEFAULTS[operation][key] is None)
        elif key in {"fact_id", "document_id", "conflicts_with", "source_id", "target_id"}:
            require(valid_id(value))
        elif key in {"fact_ids", "decision_ids"}:
            require(type(value) is list and len(value) <= 128 and all(valid_id(x) for x in value))
        elif key == "confidence":
            require(type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 1)
        else:
            text(value, 16000 if key == "text" else 40 if key == "kind" else 4000)
    if operation in {"remember", "revise"}:
        require(args["source"] in SOURCES)
        require((args["source"] == "document") == (args["document_id"] is not None))
    if operation == "dispute":
        require(args["conflicts_with"] != args["fact_id"])
    if operation == "record_decision":
        require(args["status"] in {"proposed", "accepted", "rejected", "superseded"})
    if operation == "link_records":
        require(args["source_type"] in LINKABLE and args["target_type"] in LINKABLE)
        require((args["source_type"], args["source_id"]) != (args["target_type"], args["target_id"]))
    if operation == "ingest_document":
        require(args["media_type"] in {"text/plain", "text/markdown", "application/json"})
        if args["uri"] is not None:
            uri = urlsplit(args["uri"])
            # A URI is attribution only. Never resolve it, fetch it, or open a path.
            require(bool(uri.scheme) and uri.scheme.lower() not in {"file", "data", "javascript"}
                    and uri.username is None and uri.password is None
                    and not any(c.isspace() for c in args["uri"]))
            require(uri.scheme.lower() not in {"http", "https"} or bool(uri.netloc))
    reject_secrets(args)
    require(len(json_bytes(args)) <= MAX_PAYLOAD, "payload_too_large")
    return json.loads(json_bytes(args))


def write_hash(operation, arguments):
    return hashlib.sha256(json_bytes({"operation": operation,
                                     "arguments": validate_write(operation, arguments)})).hexdigest()


def _request_name(request_id):
    text(request_id, 128)
    return hashlib.sha256(request_id.encode("utf-8")).hexdigest() + ".json"


def request_artifact(state: Path, request_id: str) -> Path:
    return Path(state) / "requests" / _request_name(request_id)


def receipt_path(state: Path, request_id: str) -> Path:
    return Path(state) / "receipts" / _request_name(request_id)


def check_stat(info, *, directory=False, private=True):
    require(stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode), "unsafe_path")
    require(directory or info.st_nlink == 1, "unsafe_hardlink")
    if private:
        require(info.st_uid == os.getuid() and not info.st_mode & 0o077, "unsafe_permissions")


@contextmanager
def directory_fd(path, *, private=True):
    """Walk all components without following links; pin the parent during each I/O."""
    require(os.name == "posix", "unsupported_platform_acl")
    path = Path(path)
    require(path.is_absolute() and ".." not in path.parts, "unsafe_path")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    fd = os.open("/", flags)
    try:
        for part in path.parts[1:]:
            following = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = following
        check_stat(os.fstat(fd), directory=True, private=private)
        yield fd
    finally:
        os.close(fd)


def _leaf(parent, name, *, missing=False):
    try:
        info = os.stat(name, dir_fd=parent, follow_symlinks=False)
    except FileNotFoundError:
        require(missing, "missing_file")
        return
    check_stat(info)


def store_identity(store):
    """Metadata only: identify the private database without opening its contents."""
    with directory_fd(store) as parent:
        info = os.stat("memory.db", dir_fd=parent, follow_symlinks=False)
        check_stat(info)
        return {"device": info.st_dev, "inode": info.st_ino}


def validate_store_identity(identity):
    require(type(identity) is dict and set(identity) == {"device", "inode"}
            and type(identity["device"]) is int and identity["device"] >= 0
            and type(identity["inode"]) is int and identity["inode"] > 0, "invalid_store_identity")
    return dict(identity)


@contextmanager
def store_session(store):
    """Pure rc3 shared lease; no Ledger object, reader creation, or schema changes."""
    from ledger_memory import locking

    store = Path(store)
    with directory_fd(store) as parent:
        lock = locking.session_lock_path(store / "memory.db")
        _leaf(parent, lock.name, missing=True)
        with locking.shared(lock, timeout=120):
            _leaf(parent, lock.name)
            yield


def load_json(path, default=None):
    path = Path(path)
    with directory_fd(path.parent) as parent:
        try:
            _leaf(parent, path.name, missing=True)
            fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        except FileNotFoundError:
            return default
        with os.fdopen(fd, "rb") as stream:
            check_stat(os.fstat(stream.fileno()))
            data = stream.read(MAX_JSON + 1)
    require(len(data) <= MAX_JSON, "state_too_large")

    def pairs(items):
        out = {}
        for key, item in items:
            require(key not in out, "duplicate_json_key")
            out[key] = item
        return out

    try:
        return json.loads(data, object_pairs_hook=pairs,
                          parse_constant=lambda _: require(False, "invalid_json"))
    except (ValueError, UnicodeError, RecursionError):
        raise ValueError("invalid_json") from None


def save_json(path, value):
    path = Path(path)
    data = json_bytes(value)
    require(len(data) <= MAX_JSON, "state_too_large")
    with directory_fd(path.parent) as parent:
        _leaf(parent, path.name, missing=True)
        temporary = "." + path.name + "." + secrets.token_hex(16)
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
        try:
            with os.fdopen(fd, "wb") as stream:
                check_stat(os.fstat(stream.fileno()))
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            _leaf(parent, path.name, missing=True)
            os.replace(temporary, path.name, src_dir_fd=parent, dst_dir_fd=parent)
            os.fsync(parent)
        finally:
            try:
                os.unlink(temporary, dir_fd=parent)
            except FileNotFoundError:
                pass


def knowledge_fingerprint(store: Path) -> str | None:
    """No safe external-write/restore fingerprint is available. Caching is disabled."""
    return None
