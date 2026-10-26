"""Shared semantic readiness report (NEM-1297).

Four things ask "is the semantic lane healthy?" and three answer from CORE METADATA ALONE, so they all
reported health while recall was switched off:

```
semantic-index-status : fresh=yes, indexed=1842, expected=1842
semantic-index-integrity : recommendation=no_action_needed
ledger-dogfood-check.sh : index_selfheal: semantic=fresh
semantic_lane_blocked (the truth): BLOCKED - receipt/count mismatch
```

A core-only `fresh` result may never be rendered as overall health. The authoritative answer is
`semantic_lane_blocked`; this module aggregates the three signals and surfaces the lane decision.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class SemanticReadiness:
    core_fresh: bool
    binding_ok: bool
    lane_open: bool
    reason: str | None
    pending_rows: int | None = None
    failed_rows: int | None = None
    state: str = "degraded"
    model_fingerprint: str | None = None
    worker_running: bool = False


def readiness(database_path: Path, sidecar_path: Path) -> SemanticReadiness:
    try:
        from ledger_memory.embeddings import (
            get_embedding_backend, is_legacy_embedding_backend, resolve_embedding_backend_name,
        )
        backend_name = resolve_embedding_backend_name(database_path, strict=True)
        if backend_name == "none":
            return SemanticReadiness(False, False, False, None, 0, 0, state="disabled")
        if is_legacy_embedding_backend(backend_name):
            return SemanticReadiness(False, False, False,
                "legacy embedding backend unavailable; run ledger semantic enable")
        backend = get_embedding_backend(database_path)
        model = backend.model_name
        active_model = model() if callable(model) else model

        from ledger_memory.semantic_index import get_semantic_index_status
        # review fix: `get_semantic_index_status` takes the DATABASE only. The delegated
        # version passed the sidecar too, and the broad `except` below turned the resulting TypeError
        # into "unmeasurable" — fail-closed, so nothing unsafe shipped, but the real cause was hidden
        # for five delegation cycles because the symptom ("lane_open=False") looked like the contract.
        core_fresh = get_semantic_index_status(database_path).fresh

        from ledger_memory.semantic_binding import binding_violation
        binding_ok = binding_violation(database_path, sidecar_path, active_model=active_model) is None

        from ledger_memory.semantic_search import semantic_lane_blocked
        lane_blocked_reason = semantic_lane_blocked(database_path, sidecar_path)
        if lane_blocked_reason is not None:
            lane_blocked_reason = "semantic index gate unavailable"
        from ledger_memory.semantic_pending import pending_embeddings
        unfinished = pending_embeddings(database_path, sidecar_path, backend=backend,
                                        include_failed=True)
        pending_rows = sum(item.attempts < 3 for item in unfinished)
        failed_rows = sum(item.attempts >= 3 for item in unfinished)
        if lane_blocked_reason is None and unfinished:
            lane_blocked_reason = "semantic projection has unfinished revisions"
        lane_open = lane_blocked_reason is None
        reason = None if lane_open else lane_blocked_reason
        state = "degraded" if failed_rows else "indexing" if pending_rows else "ready" if lane_open else "degraded"
        fingerprint = None
        worker_running = False
        if backend_name == "bge-m3":
            from ledger_memory.embedding_worker import (
                WorkerProtocolError, read_worker_key, worker_profile, worker_request, worker_lifecycle,
            )
            fingerprint = active_model.removeprefix("bge-m3:onnx:")
            profile = worker_profile()
            key = read_worker_key(profile)
            try:
                if key is None:
                    raise WorkerProtocolError("worker credential unavailable")
                worker_request(profile.address, family=profile.family, authkey=key,
                    model_fingerprint=fingerprint, texts=[], probe_only=True,
                    timeout=2, authentication_timeout=2)
                worker_running = True
            except WorkerProtocolError:
                lane_open = False
                state = "degraded"
                reason = "embedding worker readiness unavailable"
                from ledger_memory.model_artifacts import download_in_progress, model_cache_path
                if download_in_progress(model_cache_path(profile.root)):
                    state = "downloading"
                    reason = "embedding artifacts downloading"
                elif key is not None:
                    try:
                        lifecycle = worker_lifecycle(profile.address, family=profile.family,
                            authkey=key, model_fingerprint=fingerprint)
                    except WorkerProtocolError:
                        lifecycle = "unavailable"
                    worker_running = lifecycle in ("warming", "ready")
                    if lifecycle == "warming":
                        state = "warming"
                        reason = "embedding worker warming"

        return SemanticReadiness(
            core_fresh=core_fresh,
            binding_ok=binding_ok,
            lane_open=lane_open,
            reason=reason,
            pending_rows=pending_rows,
            failed_rows=failed_rows,
            state=state,
            model_fingerprint=fingerprint,
            worker_running=worker_running,
        )
    except Exception:  # noqa: BLE001
        return SemanticReadiness(
            core_fresh=False,
            binding_ok=False,
            lane_open=False,
            reason="readiness measurement unavailable",
        )
