"""Local dense embedding primitives; optional runtime dependencies load only on use."""
from __future__ import annotations

import math
import os
from pathlib import Path


def request_batches(texts: list[str], fingerprint: str) -> list[list[str]]:
    """Preserve every segment while bounding the actual encoded request bytes."""
    from ..embedding_worker import WorkerProtocolError, encode_request

    batches = []
    current = []
    for text in texts:
        candidate = [*current, text]
        try:
            encode_request(candidate, model_fingerprint=fingerprint)
        except WorkerProtocolError:
            if not current:
                raise
            batches.append(current)
            candidate = [text]
            encode_request(candidate, model_fingerprint=fingerprint)
        current = candidate
    if current:
        batches.append(current)
    return batches


def normalized_weighted_mean(vectors: list[list[float]], weights: list[int]) -> list[float]:
    if not vectors or not weights or len(vectors) != len(weights):
        raise ValueError("vectors and weights must have matching nonzero counts")
    dimensions = len(vectors[0])
    if dimensions == 0:
        raise ValueError("empty embedding")
    if any(type(weight) is not int or weight <= 0 for weight in weights):
        raise ValueError("segment weights must be positive integers")
    for vector in vectors:
        if len(vector) != dimensions:
            raise ValueError("embedding dimensions differ")
        if any(isinstance(value, bool) or not isinstance(value, (int, float))
               or not math.isfinite(value) for value in vector):
            raise ValueError("embedding values must be finite numbers")
    total = sum(weights)
    mean = [0.0] * dimensions
    for vector, weight in zip(vectors, weights):
        fraction = weight / total
        for index, value in enumerate(vector):
            mean[index] += fraction * value
    norm = math.hypot(*mean)
    if not math.isfinite(norm) or norm == 0:
        raise ValueError("embedding mean has invalid norm")
    return [value / norm for value in mean]


def segment_text(text: str, tokenizer, *, max_tokens: int = 8192,
                 max_chars: int = 65536) -> list[tuple[str, list[int]]]:
    if not isinstance(text, str):
        raise ValueError("text must be a string")
    if type(max_tokens) is not int or max_tokens <= 0:
        raise ValueError("max_tokens must be a positive integer")
    if type(max_chars) is not int or max_chars <= 0:
        raise ValueError("max_chars must be a positive integer")
    tokenizer.no_truncation()
    tokenizer.no_padding()
    if not text:
        ids = tokenizer.encode("", add_special_tokens=True).ids
        if len(ids) > max_tokens:
            raise ValueError("special tokens exceed the token budget")
        return [("", ids)]
    segments = []
    start = 0
    while start < len(text):
        best_end = start + 1
        best_ids = tokenizer.encode(text[start:best_end], add_special_tokens=True).ids
        if len(best_ids) > max_tokens:
            raise ValueError("one character exceeds the token budget")
        low, high = start + 1, min(len(text), start + max_chars) + 1
        # Token counts need not be monotone. Keep only candidates actually measured
        # to fit; a conservative split is fine, losing source spans is not.
        while low < high:
            middle = (low + high) // 2
            ids = tokenizer.encode(text[start:middle], add_special_tokens=True).ids
            if len(ids) <= max_tokens:
                best_end, best_ids = middle, ids
                low = middle + 1
            else:
                high = middle
        segments.append((text[start:best_end], best_ids))
        start = best_end
    return segments


def _setting(name: str, default: int, maximum: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except ValueError as exc:
        raise ValueError(f"{name} must be a positive integer no greater than {maximum}") from exc
    if not 1 <= value <= maximum:
        raise ValueError(f"{name} must be a positive integer no greater than {maximum}")
    return value


class ONNXServiceBackend:
    """Lightweight client; the shared service alone loads the ONNX model."""

    dimensions = 1024

    def __init__(self):
        from ..embedding_worker import worker_profile
        from ..model_artifacts import _manifest

        self._profile = worker_profile()
        self._manifest, self._fingerprint = _manifest(None)
        self.model_name = f"bge-m3:onnx:{self._fingerprint}"
        self._tokenizer = None

    def _get_tokenizer(self):
        if self._tokenizer is None:
            from ..model_artifacts import ModelArtifactError, _root, _stream, model_bundle_path

            root = _root(model_bundle_path(self._profile.root))
            name = self._manifest["tokenizer_file"]
            path = root / name
            _stream(path, self._manifest["files"][name])
            try:
                from tokenizers import Tokenizer
            except ImportError as exc:
                raise ModelArtifactError("semantic dependencies unavailable; install the semantic extra") from exc
            self._tokenizer = Tokenizer.from_file(str(path))
        return self._tokenizer

    def embed(self, texts: list[str]) -> list[list[float]]:
        from ..embedding_worker import (
            EmbeddingServiceUnavailable, WorkerProtocolError, decode_response, encode_response, ensure_worker,
            read_worker_key, worker_request,
        )
        from ..model_artifacts import ModelArtifactError

        if not isinstance(texts, list) or any(not isinstance(text, str) for text in texts):
            raise ValueError("embedding input must be a list of strings")
        if not texts:
            return []
        try:
            tokenizer = self._get_tokenizer()
        except (ModelArtifactError, OSError) as exc:
            raise EmbeddingServiceUnavailable("embedding model unavailable") from exc
        sources = [segment_text(text, tokenizer) for text in texts]
        flattened = [span for segments in sources for span, _ in segments]
        # Everything before a request is sent says nothing about the text: report it as the service being unavailable.
        try:
            profile = ensure_worker(profile=self._profile)
            key = read_worker_key(profile)
        except (WorkerProtocolError, ModelArtifactError, OSError, TimeoutError) as exc:
            raise EmbeddingServiceUnavailable("embedding service unavailable") from exc
        if key is None:
            raise EmbeddingServiceUnavailable("worker credential unavailable")
        vectors = []
        for batch in request_batches(flattened, self._fingerprint):
            reply = worker_request(profile.address, family=profile.family, authkey=key,
                model_fingerprint=self._fingerprint, texts=batch)
            # Keep count/dimension/norm guarantees even for an injected transport.
            raw = encode_response(reply, model_fingerprint=self._fingerprint)
            vectors.extend(decode_response(raw, expected_model=self._fingerprint, expected_count=len(batch)))
        result = []
        offset = 0
        for segments in sources:
            count = len(segments)
            result.append(normalized_weighted_mean(vectors[offset:offset + count],
                [len(ids) for _, ids in segments]))
            offset += count
        return result


class ONNXBGEBackend:
    """CPU-only model implementation, constructed inside a supervised model child."""

    dimensions = 1024

    def __init__(self, root: Path):
        from ..model_artifacts import ModelArtifactError, verify_model

        verified = verify_model(root)
        self._batch_size = _setting("LEDGER_EMBED_BATCH", 4, 4)
        threads = _setting("LEDGER_EMBED_THREADS", 2, 32)
        try:
            import numpy as np
            import onnxruntime as ort
            from tokenizers import Tokenizer
        except ImportError as exc:
            raise ModelArtifactError("semantic dependencies unavailable; install the semantic extra") from exc
        ort.disable_telemetry_events()
        options = ort.SessionOptions()
        options.intra_op_num_threads = threads
        options.inter_op_num_threads = 1
        options.log_severity_level = 3
        self._tokenizer = Tokenizer.from_file(str(verified.tokenizer_path))
        self._tokenizer.no_padding()
        self._tokenizer.no_truncation()
        self._pad = self._tokenizer.token_to_id("<pad>")
        if type(self._pad) is not int or self._pad < 0:
            raise ValueError("pinned tokenizer has no valid padding token")
        self._session = ort.InferenceSession(
            str(verified.model_path), sess_options=options, providers=["CPUExecutionProvider"])
        self._np = np
        self.model_name = f"bge-m3:onnx:{verified.model_fingerprint}"

    def _infer(self, batch: list[tuple[int, list[int]]]) -> list[list[float]]:
        np = self._np
        width = max(len(ids) for _, ids in batch)
        if width == 0 or len(batch) > 4 or len(batch) * width > 8192:
            raise ValueError("inference batch exceeds the resource budget")
        inputs = np.full((len(batch), width), self._pad, dtype=np.int64)
        mask = np.zeros((len(batch), width), dtype=np.int64)
        for index, (_, ids) in enumerate(batch):
            inputs[index, :len(ids)] = ids
            mask[index, :len(ids)] = 1
        outputs = self._session.run(["sentence_embedding"], {"input_ids": inputs, "attention_mask": mask})
        if len(outputs) != 1:
            raise ValueError("model returned an invalid output count")
        vectors = np.asarray(outputs[0], dtype=np.float64)
        if vectors.shape != (len(batch), self.dimensions) or not np.isfinite(vectors).all():
            raise ValueError("model returned invalid embedding dimensions or values")
        return [normalized_weighted_mean([vector], [1]) for vector in vectors.tolist()]

    def embed(self, texts: list[str], *, for_query: bool = False) -> list[list[float]]:
        vectors: list[list[list[float]]] = [[] for _ in texts]
        weights: list[list[int]] = [[] for _ in texts]
        batch: list[tuple[int, list[int]]] = []

        def flush():
            for (owner, ids), vector in zip(batch, self._infer(batch)):
                vectors[owner].append(vector)
                weights[owner].append(len(ids))
            batch.clear()

        for owner, text in enumerate(texts):
            for _, ids in segment_text(text, self._tokenizer):
                width = max([len(ids), *(len(existing) for _, existing in batch)])
                if batch and (len(batch) >= self._batch_size or (len(batch) + 1) * width > 8192):
                    flush()
                batch.append((owner, ids))
        if batch:
            flush()
        return [normalized_weighted_mean(parts, counts) for parts, counts in zip(vectors, weights)]
