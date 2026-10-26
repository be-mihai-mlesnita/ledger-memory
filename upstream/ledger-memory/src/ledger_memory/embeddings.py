"""Embedding backend protocol and factory.

Core is stdlib-only. `get_embedding_backend` resolves the backend by precedence
: the `LEDGER_EMBEDDING_BACKEND` env var, else the per-db `embedding.backend`
config, else `none` (semantic search off). `bge-m3` constructs an inert client
for the authenticated user-local ONNX service. Legacy server configuration is
recognized only to explain the explicit opt-in upgrade, never to contact it.

DeterministicFakeBackend produces reproducible unit-length vectors via sha256.
It is a test fixture; its output has no semantic meaning.
"""

from __future__ import annotations

import hashlib
import math
import os
import struct
from typing import Protocol, runtime_checkable


class SemanticSearchNotConfiguredError(RuntimeError):
    """Raised instead of silently embedding with the deterministic test backend."""


@runtime_checkable
class EmbeddingBackend(Protocol):
    # model_name: identity string recorded in semantic_index_entries.
    @property
    def model_name(self) -> str: ...

    # dimensions: length of each returned float vector; must be consistent per instance.
    @property
    def dimensions(self) -> int: ...

    def embed(self, texts: list[str]) -> list[list[float]]: ...


class DeterministicFakeBackend:
    def __init__(self, dimensions: int = 64) -> None:
        self._dimensions = dimensions

    @property
    def model_name(self) -> str:
        return "ledger-fake-v1"

    @property
    def dimensions(self) -> int:
        return self._dimensions

    def embed(self, texts: list[str]) -> list[list[float]]:
        result = []
        for text in texts:
            digest = hashlib.sha256(text.encode("utf-8")).digest()
            raw = (digest * ((self._dimensions * 4 // 32) + 1))[: self._dimensions * 4]
            ints = struct.unpack_from(f"<{self._dimensions}I", raw)
            floats = [i / 4294967296.0 for i in ints]
            norm = math.sqrt(sum(f * f for f in floats))
            if norm < 1e-12:
                result.append(floats)
            else:
                result.append([f / norm for f in floats])
        return result


def resolve_embedding_backend_name(database_path=None, *, strict: bool = False) -> str:
    """which embedding backend to use, by precedence:

    1. the ``LEDGER_EMBEDDING_BACKEND`` env var (explicit override — e.g. the ``store`` wrapper),
    2. else the per-db ``embedding.backend`` config (so a consumer like the scheduler tick can pick a real backend
       without an env var),
    3. else ``"none"`` (semantic search off).

    The config read is defensive (lazy import; any error — uninitialized db, missing config table — falls through
    to the default) and only happens when the env is unset, so the env path keeps zero overhead and zero behavior
    change."""
    name = os.environ.get("LEDGER_EMBEDDING_BACKEND")
    if name:
        return name
    if database_path is not None:
        try:
            from ledger_memory.config import get_config

            configured = get_config(database_path, "embedding.backend")
            if configured and configured.strip():
                return configured.strip()
        except Exception:
            if strict:
                raise
    return "none"


def get_embedding_backend(database_path=None) -> EmbeddingBackend:
    backend_name = resolve_embedding_backend_name(database_path)
    if backend_name == "none":
        raise SemanticSearchNotConfiguredError(
            "semantic search is not configured: run ledger semantic enable"
        )
    if backend_name == "fake":
        return DeterministicFakeBackend()
    if backend_name == "bge-m3":
        from ledger_memory.adapters.onnx_bge import ONNXServiceBackend
        return ONNXServiceBackend()
    if is_legacy_embedding_backend(backend_name):
        raise SemanticSearchNotConfiguredError(
            "legacy semantic backend is unavailable; keyword search remains available; "
            "run ledger semantic enable to opt into local bge-m3"
        )
    raise ValueError(
        f"Unknown embedding backend value: {backend_name!r}. "
        "Valid values: none, bge-m3, fake (tests only). (Set LEDGER_EMBEDDING_BACKEND or the 'embedding.backend' config.)"
    )


def is_legacy_embedding_backend(name: str) -> bool:
    """Recognize historical configuration without importing a server adapter."""
    return name == "lmstudio"
