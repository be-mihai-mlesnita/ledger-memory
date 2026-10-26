from __future__ import annotations

"""pluggable chunker contract + deterministic strategies.

A ``Chunker`` turns document text into ordered ``ChunkPiece``s deterministically: identical
``(text, params, backend)`` yields a byte-identical piece list, so a chunk set is rebuildable
 and its ``content_sha256`` values are stable. Two strategies ship:

- ``BoundaryChunker`` — the baseline: greedily pack whole sentences up to ``max_chars``.
- ``SemanticChunker`` — option c (decision #40): embed each sentence and cut where adjacent cosine
  similarity drops below ``threshold`` (or where ``max_chars`` would be exceeded).

This module is pure — no DB, no I/O beyond the injected embedding backend. The default backend is
``get_embedding_backend``, which returns the ``DeterministicFakeBackend`` unless
``LEDGER_EMBEDDING_BACKEND=bge-m3``; tests use isolated synthetic embedding fixtures.
"""

import hashlib
import json
import math
import re
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from ledger_memory.embeddings import EmbeddingBackend, get_embedding_backend

# regex-v1: split on sentence-terminator + whitespace, after splitting on blank-line paragraph breaks.
# Recorded verbatim in every recipe as "splitter": "regex-v1"; changing it changes the projection.
_SENTENCE_BOUNDARY = re.compile(r"(?<=[.!?])\s+")
_PARAGRAPH_BREAK = re.compile(r"\n\s*\n")


@dataclass(frozen=True)
class ChunkPiece:
    index: int
    content: str


@runtime_checkable
class Chunker(Protocol):
    @property
    def name(self) -> str: ...

    @property
    def projection_version(self) -> str: ...

    def recipe(self) -> dict: ...

    def chunk(self, text: str) -> list[ChunkPiece]: ...


def _split_sentences(text: str) -> list[str]:
    """Deterministic, stdlib-only sentence split (splitter id ``regex-v1``).

    Paragraphs first (blank-line separated), then sentence terminators within each. Whitespace-only
    fragments are dropped; each survivor is stripped. Empty/whitespace input yields ``[]``.
    """
    sentences: list[str] = []
    for paragraph in _PARAGRAPH_BREAK.split(text):
        for fragment in _SENTENCE_BOUNDARY.split(paragraph):
            stripped = fragment.strip()
            if stripped:
                sentences.append(stripped)
    return sentences


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a < 1e-12 or norm_b < 1e-12:
        return 0.0
    return dot / (norm_a * norm_b)


def _pack(pieces: list[str]) -> list[ChunkPiece]:
    return [ChunkPiece(index=i, content=c) for i, c in enumerate(pieces)]


def _recipe_hash(recipe: dict) -> str:
    """A short, stable digest of the full recipe (sorted-key JSON).

    Folded into ``projection_version`` so that two sets produced by DIFFERENT parameters (e.g. a
    different ``threshold`` or ``max_chars``) get DIFFERENT projection versions — making re-chunking
    detectable and keeping the ``chunk_document`` idempotency key 1:1 with the recipe.
    """
    return hashlib.sha1(json.dumps(recipe, sort_keys=True).encode("utf-8")).hexdigest()[:8]


class BoundaryChunker:
    """Baseline: greedily pack whole sentences up to ``max_chars`` (never split mid-sentence unless a
    lone sentence already exceeds the cap, in which case it is its own chunk)."""

    def __init__(self, max_chars: int = 1000) -> None:
        self.max_chars = max_chars

    @property
    def name(self) -> str:
        return "boundary"

    @property
    def projection_version(self) -> str:
        return f"boundary-{_recipe_hash(self.recipe())}"

    def recipe(self) -> dict:
        return {"strategy": "boundary", "max_chars": self.max_chars, "splitter": "regex-v1"}

    def chunk(self, text: str) -> list[ChunkPiece]:
        sentences = _split_sentences(text)
        if not sentences:
            return []
        chunks: list[str] = []
        current = ""
        for sentence in sentences:
            if not current:
                current = sentence
            elif len(current) + 1 + len(sentence) <= self.max_chars:
                current = f"{current} {sentence}"
            else:
                chunks.append(current)
                current = sentence
        if current:
            chunks.append(current)
        return _pack(chunks)


class SemanticChunker:
    """Option c: embed each sentence, start a new chunk where adjacent cosine similarity drops below
    ``threshold`` (a topic shift) OR where appending would exceed ``max_chars``."""

    def __init__(
        self,
        backend: EmbeddingBackend | None = None,
        threshold: float = 0.5,
        max_chars: int = 1500,
    ) -> None:
        self.backend = backend or get_embedding_backend()
        self.threshold = threshold
        self.max_chars = max_chars

    @property
    def name(self) -> str:
        return "semantic"

    @property
    def projection_version(self) -> str:
        # Human-readable strategy + embedder identity, plus a recipe hash so threshold/max_chars
        # changes yield a distinct projection.
        return f"semantic-{self.backend.model_name}-{_recipe_hash(self.recipe())}"

    def recipe(self) -> dict:
        return {
            "strategy": "semantic",
            "threshold": self.threshold,
            "max_chars": self.max_chars,
            "embedder": self.backend.model_name,
            "splitter": "regex-v1",
        }

    def chunk(self, text: str) -> list[ChunkPiece]:
        sentences = _split_sentences(text)
        if len(sentences) <= 1:
            return _pack(sentences)
        vectors = self.backend.embed(sentences)
        chunks: list[str] = []
        current = sentences[0]
        for i in range(1, len(sentences)):
            similar = _cosine(vectors[i - 1], vectors[i]) >= self.threshold
            fits = len(current) + 1 + len(sentences[i]) <= self.max_chars
            if similar and fits:
                current = f"{current} {sentences[i]}"
            else:
                chunks.append(current)
                current = sentences[i]
        chunks.append(current)
        return _pack(chunks)
