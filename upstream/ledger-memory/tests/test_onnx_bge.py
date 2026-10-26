"""Offline checks for all-span tokenization and normalized segment aggregation."""
from __future__ import annotations

import importlib
import math
import types
import sys
from pathlib import Path

import pytest


def test_factory_constructs_inert_shared_backend(monkeypatch):
    embeddings = importlib.import_module("ledger_memory.embeddings")
    backend = importlib.import_module("ledger_memory.adapters.onnx_bge")
    worker = importlib.import_module("ledger_memory.embedding_worker")
    monkeypatch.setenv("LEDGER_EMBEDDING_BACKEND", "bge-m3")
    monkeypatch.setattr(worker, "ensure_worker", lambda **kwargs: pytest.fail("factory started a service"))
    result = embeddings.get_embedding_backend()
    assert isinstance(result, backend.ONNXServiceBackend)
    assert result.dimensions == 1024
    assert result.model_name.startswith("bge-m3:onnx:")


def test_legacy_factory_refuses_without_loading_old_adapter(monkeypatch):
    embeddings = importlib.import_module("ledger_memory.embeddings")
    monkeypatch.setenv("LEDGER_EMBEDDING_BACKEND", "lmstudio")
    # Importing an old adapter would fail instead of accidentally contacting it.
    monkeypatch.setitem(sys.modules, "ledger_memory.adapters.lmstudio", None)
    with pytest.raises(embeddings.SemanticSearchNotConfiguredError, match="semantic enable"):
        embeddings.get_embedding_backend()


def test_proxy_batches_respect_serialized_byte_and_count_limits():
    backend = importlib.import_module("ledger_memory.adapters.onnx_bge")
    worker = importlib.import_module("ledger_memory.embedding_worker")
    # Astral Unicode expands to twelve ASCII JSON bytes per source character.
    source = ["\U0001f642" * 65536, "\U0001f642" * 65536, *(["same"] * 40), ""]
    batches = backend.request_batches(source, "synthetic-model")
    assert len(batches) >= 3, "fixture did not exceed both request limits"
    assert [text for batch in batches for text in batch] == source
    assert all(0 < len(batch) <= 32 for batch in batches)
    assert all(len(worker.encode_request(batch, model_fingerprint="synthetic-model")) <= worker.MAX_FRAME
               for batch in batches)
    assert backend.request_batches([], "synthetic-model") == []
    with pytest.raises(worker.WorkerProtocolError):
        backend.request_batches(["x" * 65537], "synthetic-model")


class _CharacterTokenizer:
    def __init__(self):
        self.truncation_disabled = False
        self.padding_disabled = False

    def no_truncation(self):
        self.truncation_disabled = True

    def no_padding(self):
        self.padding_disabled = True

    def encode(self, text, *, add_special_tokens):
        assert self.truncation_disabled, "test tokenizer would otherwise silently truncate"
        assert self.padding_disabled, "padding must be controlled by the inference batch"
        assert add_special_tokens
        return types.SimpleNamespace(ids=[0, *(ord(character) for character in text), 2])


def test_shared_proxy_preserves_all_spans_and_source_vector_ownership(monkeypatch):
    backend = importlib.import_module("ledger_memory.adapters.onnx_bge")
    worker = importlib.import_module("ledger_memory.embedding_worker")
    profile = types.SimpleNamespace(address="synthetic", family="synthetic", root=Path("synthetic"))
    started, observed = [], []
    monkeypatch.setattr(worker, "worker_profile", lambda: profile)
    monkeypatch.setattr(worker, "ensure_worker", lambda **kwargs: started.append(True) or profile)
    monkeypatch.setattr(worker, "read_worker_key", lambda profile: b"x" * 32)
    monkeypatch.setattr(backend.ONNXServiceBackend, "_get_tokenizer", lambda self: _CharacterTokenizer())
    def request(address, **options):
        assert options["authkey"] == b"x" * 32
        observed.extend(options["texts"])
        return [[float(index == ord(text[0])) for index in range(1024)] for text in options["texts"]]
    monkeypatch.setattr(worker, "worker_request", request)
    proxy = backend.ONNXServiceBackend()
    assert started == [], "construction must not start a service or load a model"
    assert proxy.embed([]) == []
    assert started == []
    source = ["a" * 17000, "b" * 9000]
    vectors = proxy.embed(source)
    assert "".join(observed) == "".join(source)
    assert len(observed) > len(source)
    assert len(vectors) == len(source)
    assert all(len(vector) == 1024 for vector in vectors)
    assert vectors[0][ord("a")] == pytest.approx(1)
    assert vectors[1][ord("b")] == pytest.approx(1)
    assert started == [True]


@pytest.mark.parametrize("bad_reply", [False, True])
def test_proxy_uses_measured_token_weights_and_refuses_partial_reply(monkeypatch, bad_reply):
    backend = importlib.import_module("ledger_memory.adapters.onnx_bge")
    worker = importlib.import_module("ledger_memory.embedding_worker")
    profile = types.SimpleNamespace(address="synthetic", family="synthetic", root=Path("synthetic"))
    monkeypatch.setattr(worker, "worker_profile", lambda: profile)
    monkeypatch.setattr(worker, "ensure_worker", lambda **kwargs: profile)
    monkeypatch.setattr(worker, "read_worker_key", lambda profile: b"x" * 32)
    monkeypatch.setattr(backend.ONNXServiceBackend, "_get_tokenizer", lambda self: _CharacterTokenizer())
    source = "a" * 8190 + "b" * 1000
    observed = []
    def request(address, **options):
        observed.extend(options["texts"])
        vectors = [[float(index == (0 if text[0] == "a" else 1)) for index in range(1024)]
                   for text in options["texts"]]
        return vectors[:-1] if bad_reply else vectors
    monkeypatch.setattr(worker, "worker_request", request)
    proxy = backend.ONNXServiceBackend()
    if bad_reply:
        with pytest.raises(worker.WorkerProtocolError, match="count"):
            proxy.embed([source])
        assert "".join(observed) == source, "invalid reply arm did not send the complete source"
    else:
        vector = proxy.embed([source])[0]
        assert observed == ["a" * 8190, "b" * 1000]
        # Includes two special tokens in each independently measured segment.
        norm = math.hypot(8192, 1002)
        assert vector[0] == pytest.approx(8192 / norm)
        assert vector[1] == pytest.approx(1002 / norm)
        assert math.hypot(*vector) == pytest.approx(1)


def test_long_inputs_cover_every_span_without_truncation():
    backend = importlib.import_module("ledger_memory.adapters.onnx_bge")
    tokenizer = _CharacterTokenizer()
    text = "  alpha\n" + "ș🙂x" * 10000 + "\nLAST-SPAN  "
    segments = backend.segment_text(text, tokenizer, max_tokens=81, max_chars=100)
    assert len(segments) > 1
    assert "".join(span for span, _ in segments) == text
    assert all(len(ids) <= 81 and 0 < len(span) <= 100 for span, ids in segments)
    assert all(ids == [0, *(ord(character) for character in span), 2] for span, ids in segments)
    assert tokenizer.truncation_disabled and tokenizer.padding_disabled


def test_weighted_segment_aggregation_is_normalized():
    backend = importlib.import_module("ledger_memory.adapters.onnx_bge")
    vector = backend.normalized_weighted_mean([[1.0, 0.0], [0.0, 1.0]], [3, 4])
    assert vector == pytest.approx([0.6, 0.8])
    assert math.hypot(*vector) == pytest.approx(1.0)


@pytest.mark.parametrize("vectors,weights", [
    ([], []), ([[1.0]], []), ([[]], [1]), ([[1.0], [1.0, 2.0]], [1, 1]),
    ([[float("nan")]], [1]), ([[float("inf")]], [1]), ([[True]], [1]),
    ([[1.0]], [0]), ([[1.0]], [-1]), ([[1.0]], [True]),
    ([[1.0]], [0.5]), ([[0.0]], [1]), ([[1.0], [-1.0]], [1, 1]),
])
def test_aggregation_refuses_invalid_vectors_and_weights(vectors, weights):
    backend = importlib.import_module("ledger_memory.adapters.onnx_bge")
    with pytest.raises(ValueError):
        backend.normalized_weighted_mean(vectors, weights)


def test_segmentation_empty_and_impossible_budgets():
    backend = importlib.import_module("ledger_memory.adapters.onnx_bge")
    tokenizer = _CharacterTokenizer()
    assert backend.segment_text("", tokenizer) == [("", [0, 2])]
    with pytest.raises(ValueError):
        backend.segment_text("x", tokenizer, max_tokens=2)
    for invalid in (0, -1, True, 1.5):
        with pytest.raises(ValueError):
            backend.segment_text("x", tokenizer, max_chars=invalid)


def test_official_sentence_output_shape_norm_and_finite_values(tmp_path, monkeypatch):
    np = pytest.importorskip("numpy")
    backend = importlib.import_module("ledger_memory.adapters.onnx_bge")
    artifacts = importlib.import_module("ledger_memory.model_artifacts")
    order = []
    calls = []
    result_kind = ["good"]
    tokenizer = _CharacterTokenizer()
    tokenizer.token_to_id = lambda token: 1
    verified = artifacts.VerifiedModel(tmp_path, tmp_path / "model.onnx", tmp_path / "tokenizer.json", "synthetic-hash")
    def verify(root):
        assert Path(root) == tmp_path
        order.append("verify")
        return verified
    monkeypatch.setattr(artifacts, "verify_model", verify)
    class Session:
        def __init__(self, path, *, sess_options, providers):
            assert order == ["verify", "telemetry_disabled"]
            assert providers == ["CPUExecutionProvider"]
            assert sess_options.intra_op_num_threads == 2
            assert sess_options.inter_op_num_threads == 1
            order.append("session")
        def run(self, names, feeds):
            assert names == ["sentence_embedding"]
            assert feeds["input_ids"].dtype == np.int64
            assert feeds["attention_mask"].dtype == np.int64
            rows, width = feeds["input_ids"].shape
            assert rows <= 4 and rows * width <= 8192
            calls.append(feeds)
            result = np.zeros((rows, 1024), dtype=np.float32)
            for row in range(rows):
                result[row, int(feeds["input_ids"][row, 1])] = 1
            if result_kind[0] == "nan":
                result[:, 0] = np.nan
            elif result_kind[0] == "dimension":
                result = result[:, :1023]
            elif result_kind[0] == "zero":
                result[:] = 0
            return [result]
    monkeypatch.setitem(sys.modules, "onnxruntime", types.SimpleNamespace(
        disable_telemetry_events=lambda: order.append("telemetry_disabled"),
        SessionOptions=types.SimpleNamespace, InferenceSession=Session))
    monkeypatch.setitem(sys.modules, "tokenizers", types.SimpleNamespace(
        Tokenizer=types.SimpleNamespace(from_file=lambda path: tokenizer)))
    monkeypatch.delenv("LEDGER_EMBED_THREADS", raising=False)
    monkeypatch.delenv("LEDGER_EMBED_BATCH", raising=False)
    model = backend.ONNXBGEBackend(tmp_path)
    sources = ["a" * 10000, "short", "b" * 4000, "c" * 4000, "last"]
    vectors = model.embed(sources)
    assert len(vectors) == 5
    assert len(calls) > 1, "resource budget fixture did not exercise multiple batches"
    assert model.dimensions == 1024
    assert "synthetic-hash" in model.model_name
    assert all(len(vector) == 1024 and math.hypot(*vector) == pytest.approx(1) for vector in vectors)
    assert all(all(math.isfinite(value) for value in vector) for vector in vectors)
    assert [int(np.argmax(vector)) for vector in vectors] == [ord(text[0]) for text in sources]
    observed = []
    for feeds in calls:
        for ids, mask in zip(feeds["input_ids"], feeds["attention_mask"]):
            observed.extend(ids[1:int(mask.sum()) - 1].tolist())
    assert observed == [ord(character) for text in sources for character in text]
    for kind in ("nan", "dimension", "zero"):
        result_kind[0] = kind
        with pytest.raises(ValueError):
            model.embed(["invalid output fixture"])


def test_telemetry_disabled_before_session_creation(tmp_path, monkeypatch):
    import sys
    import types
    pytest.importorskip("numpy")
    backend = importlib.import_module("ledger_memory.adapters.onnx_bge")
    artifacts = importlib.import_module("ledger_memory.model_artifacts")
    order = []
    monkeypatch.setattr(artifacts, "verify_model", lambda root: artifacts.VerifiedModel(
        tmp_path, tmp_path / "model.onnx", tmp_path / "tokenizer.json", "synthetic-hash"))
    ort = types.ModuleType("onnxruntime")
    ort.disable_telemetry_events = lambda: order.append("telemetry_disabled")
    ort.SessionOptions = type("SessionOptions", (), {})
    class InferenceSession:
        def __init__(self, path, *, sess_options, providers):
            order.append("session")
    ort.InferenceSession = InferenceSession
    tokenizers = types.ModuleType("tokenizers")
    class Tokenizer:
        @staticmethod
        def from_file(path):
            return Tokenizer()
        def no_padding(self):
            pass
        def no_truncation(self):
            pass
        def token_to_id(self, token):
            return 1
    tokenizers.Tokenizer = Tokenizer
    monkeypatch.setitem(sys.modules, "onnxruntime", ort)
    monkeypatch.setitem(sys.modules, "tokenizers", tokenizers)
    backend.ONNXBGEBackend(tmp_path)
    assert order == ["telemetry_disabled", "session"]


@pytest.mark.parametrize("failure", ["worker", "artifact", "credential"])
def test_pre_request_failures_report_service_unavailable(tmp_path, monkeypatch, failure):
    """Nothing about the text was tried, so the caller must not charge it to the record."""
    backend_module = importlib.import_module("ledger_memory.adapters.onnx_bge")
    worker = importlib.import_module("ledger_memory.embedding_worker")
    artifacts = importlib.import_module("ledger_memory.model_artifacts")
    monkeypatch.setattr(worker, "worker_profile", lambda **kwargs: worker.WorkerProfile(
        tmp_path, tmp_path / "auth.key", str(tmp_path / "s.sock"), "AF_UNIX"))
    backend = backend_module.ONNXServiceBackend()
    tokenizer = _CharacterTokenizer()
    tokenizer.no_truncation()
    tokenizer.no_padding()
    backend._tokenizer = tokenizer
    if failure == "worker":
        def down(**kwargs):
            raise worker.WorkerProtocolError("synthetic restart budget exhausted")
        monkeypatch.setattr(worker, "ensure_worker", down)
    elif failure == "artifact":
        backend._tokenizer = None
        def missing(self):
            raise artifacts.ModelArtifactError("synthetic missing model")
        monkeypatch.setattr(backend_module.ONNXServiceBackend, "_get_tokenizer", missing)
    else:
        monkeypatch.setattr(worker, "ensure_worker", lambda **kwargs: kwargs["profile"])
        monkeypatch.setattr(worker, "read_worker_key", lambda profile: None)
    with pytest.raises(worker.EmbeddingServiceUnavailable):
        backend.embed(["synthetic text"])
