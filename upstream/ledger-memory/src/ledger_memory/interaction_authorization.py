"""stage 1362a — presence becomes a request-scoped interaction authorization.

## What this module is (Contract Part A, Amendment v2.1)

`tiered_read`'s presence clamp used to ask "is this device present?" — a question answered from an
ambient timestamp (`client_devices.last_activity_at`) that, once fresh, authorized EVERY read for the
whole idle window, not just the one interaction that produced it. This module answers a narrower
question instead: "did a verified owner interaction authorize *this* read?" — and the answer is
consumed exactly once, by exactly the read it authorizes.

**A1 — a first-class typed proof.** :class:`InteractionAuthorization` carries issuer/subject/evidence
kind/surface/purpose/request id — no booleans, no caller-supplied ceilings, and never bearer material.

**Amendment v2.1 #3 — a verifier-owned registry, not a typed dataclass.** Any caller can construct a
dataclass; that is not proof of anything. What actually gates T1 is a cryptographically random opaque
nonce (`secrets.token_urlsafe`) minted into a process-local, LOCK-PROTECTED registry keyed to
issuer/subject/surface/purpose/request id. `consume_interaction_authorization` is the ONLY way to turn
a nonce back into an :class:`InteractionAuthorization`, and it does so with a single lock-guarded
`dict.pop` — one atomic lookup-and-consume, so two racing consumers can never both win. A caller that
builds an `InteractionAuthorization` by hand, or presents a nonce this registry never minted (or already
consumed), gets nothing.

**Amendment v2.1 #4 — expiry is a cleanup backstop, not the lifetime.** The real lifetime is one-time
atomic consumption plus the minting caller's own response-completion cleanup (`evict_interaction_authorization`,
called from `client_api`'s request handler in a `finally` block). `ABANDONED_PROOF_BACKSTOP_SECONDS` is
the registry's OWN monotonic deadline for a proof whose request never reaches that cleanup (a crash, an
unhandled exception) — it is intentionally NOT depended upon as the primary security boundary, exactly
as Part D pins for the (separately staged) voice bound: unmeasured figures do not become security
constants.

## What this module is not

Not an identity framework. It does not know
what a "device" or "an owner" is — it matches opaque labels a caller supplies. The caller that decides
those labels (stage 1: `client_api`'s `/api/chat` handler, and ONLY there) is what actually enforces "an
owner subject with a T1-capable credential, verified in the funnel database" (Amendment v2.1 #2) — see
`client_api._mint_chat_interaction_authorization`.

Not durable: the registry is an in-process dict. A process restart drops every unconsumed proof, which
is correct — a proof outlives neither the request it was minted for nor the process that minted it.
"""
from __future__ import annotations

import hashlib
import secrets
import threading
import time
from contextvars import ContextVar
from dataclasses import dataclass

# A1's full evidence-kind vocabulary. Stage 1362a mints ONLY `EVIDENCE_KIND_DEVICE_REQUEST`; the other
# two are named here (not invented later) so 1362b/1362c extend this SAME typed shape rather than each
# growing an incompatible one — a reviewer's staging warning is "don't build the identity framework THIS
# slice", not "don't name the vocabulary the framework will eventually need".
EVIDENCE_KIND_DEVICE_REQUEST = "device_request"
EVIDENCE_KIND_TELEGRAM_OWNER_MESSAGE = "telegram_owner_message"
EVIDENCE_KIND_TELEGRAM_OWNER_VOICE = "telegram_owner_voice"
_EVIDENCE_KINDS = frozenset(
    {EVIDENCE_KIND_DEVICE_REQUEST, EVIDENCE_KIND_TELEGRAM_OWNER_MESSAGE, EVIDENCE_KIND_TELEGRAM_OWNER_VOICE}
)

# A1's one purpose this stage mints for. A caller-supplied purpose would let any minting caller widen
# what a proof is good for; stage 1 has exactly one legitimate purpose, so it is a constant, not a
# parameter anyone passes in.
PURPOSE_MATERIALIZE_T1 = "materialize_t1"

# ledger is a single-owner system (Non-goals: "no general identity framework"). Stage 1 mints proofs for
# exactly one subject; this is a fixed label the mint and consume sides agree on, never a foreign key
# into `actors` or anything else that would need this module to know what an actor is.
OWNER_SUBJECT = "owner"

# The surfaces whose ingress MINTS. `tiered_read` fails CLOSED on these when no valid proof reaches it:
# a refused mint (T2/T3 device, revoked device, no bearer token, or a mint that errored) must not be
# indistinguishable from an ingress that does not mint yet, or the gate is advisory — fail it and you
# get the legacy ambient clamp, which is the confused-deputy path this whole slice exists to close.
# Two independent adversarial lenses reproduced exactly that on 2026-08-14 before it shipped.
#
# A surface joins this set in the SAME slice that teaches its ingress to mint — chat joined in
# 1362b, 1362c adds voice. Adding a surface here without a minting ingress silently darkens it; adding a
# minting ingress without listing it here silently reopens the fall-through. The pairing is the
# invariant, and `tests/test_interaction_authorization.py` pins both directions.
MINTING_SURFACES = frozenset({"api-chat", "telegram"})

# Amendment v2.1 #4 — a CLEANUP BACKSTOP for an abandoned proof (the minting request crashed or raised
# before its own response-completion cleanup ran), NOT the security boundary. The real lifetime is
# one-time atomic consumption + response-completion cleanup (see the module docstring). This figure is
# UNMEASURED — Part D's rule for the voice bound applies here too ("the voice bound is measured, not
# guessed"; the independent variable must be proven to have varied, an earlier fix's lesson). 60s is a
# conservative placeholder pending a real measurement pass over the app/API path, not a tuned constant;
# it must never be read as "how long a proof is allowed to authorize reads" — that answer is "one".
ABANDONED_PROOF_BACKSTOP_SECONDS = 60

# chat owner-message evidence backstop — bounds how long queued evidence may wait before the
# executor claims it. This is a CLEANUP bound for the in-memory evidence registry, NOT a proof lifetime
# (the proof is minted at claim time, immediately before converse). 30 minutes allows for legitimately
# long queue waits behind slow replies; the figure is revisited with measured queue depths (Part D).
TELEGRAM_EVIDENCE_BACKSTOP_SECONDS = 1800

# The clock seam (mirrors `client_auth._now`'s `time.time` wrapper): tests advance this to exercise the
# backstop without a real sleep. Referenced as a MODULE GLOBAL by `_Registry` and `_ChatEvidenceRegistry`
# so monkeypatching `interaction_authorization._monotonic` reaches both singletons.
_monotonic = time.monotonic


def _hash_nonce(raw_nonce: str) -> str:
    """The audit-safe id for a nonce. Mandatory tightening #3: 'the raw nonce stays request-local and
    is NEVER audited — audit only its hash/id.' sha256 is one-way and this is the ONLY place a nonce is
    ever hashed for that purpose, so a proof_id can never be reversed back into bearer material."""
    return hashlib.sha256(raw_nonce.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class InteractionAuthorization:
    """A1's typed proof — the METADATA a successful consume returns. Deliberately carries no nonce
    field: constructing this object (which any caller CAN do — dataclasses do not gate construction)
    grants nothing on its own, exactly as constructing an `AuthenticatedContext` grants nothing without
    a token that independently verifies. Only `consume_interaction_authorization` finding a live,
    matching registry entry ever produces one of these."""

    issuer: str
    subject: str
    evidence_kind: str
    surface: str
    purpose: str
    request_id: str
    proof_id: str  # sha256(nonce) — audit-safe, never reversible to the nonce
    issued_at: float  # time.monotonic() at mint


@dataclass(frozen=True)
class ProofHandle:
    """Unverified request-local material carried BESIDE the credential (mirrors
    `AuthenticatedContext.token`): raw material a caller presents, not a claim it can trust about
    itself. `about_me_recall._recall_via_chokepoint` reads one of these off the current-request context
    var and passes it into `tiered_read`, which is the only thing that can turn it into anything."""

    nonce: str
    request_id: str


@dataclass(frozen=True)
class ConsumeResult:
    """The atomic consume's outcome: exactly one of `authorization` (success) or `reason` (a
    distinguishable refusal — Part B2: 'each distinguishable in the audit'). Never both."""

    authorization: InteractionAuthorization | None
    reason: str | None


REASON_UNKNOWN_OR_ALREADY_CONSUMED = "unknown_or_already_consumed"
REASON_EXPIRED_BACKSTOP = "expired_backstop"
REASON_ISSUER_MISMATCH = "issuer_mismatch"
REASON_SUBJECT_MISMATCH = "subject_mismatch"
REASON_SURFACE_MISMATCH = "surface_mismatch"
REASON_PURPOSE_MISMATCH = "purpose_mismatch"
REASON_REQUEST_ID_MISMATCH = "request_id_mismatch"


@dataclass(frozen=True)
class _Entry:
    issuer: str
    subject: str
    evidence_kind: str
    surface: str
    purpose: str
    request_id: str
    proof_id: str
    issued_at: float
    deadline: float  # time.monotonic() value; the ABANDONED_PROOF_BACKSTOP_SECONDS cleanup bound


# the context tool's per-reply T1 allowance. Minted ONLY by the two ingress sites that already minted a
# verified `PURPOSE_MATERIALIZE_T1` proof for the same request, never by the tool; bound to the tool's own surface
# (`<surface>:tool`) and this purpose, so the briefing cannot spend it and the tool cannot spend the briefing's proof.
PURPOSE_TOOL_READ_T1 = "tool_read_t1"
TOOL_SURFACE_SUFFIX = ":tool"
MAX_PROOF_USES = 5  # equals context_tool.HARD_MAX_READS; a test pins that the two agree


class _Registry:
    """The verifier-owned nonce registry (Amendment v2.1 #3). Process-local, lock-protected, never
    durable (see module docstring). `mint` is the only place a raw nonce is ever returned; every other
    caller only ever sees `proof_id` (its hash) via a consumed :class:`InteractionAuthorization`."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._entries: dict[str, _Entry] = {}
        self._uses: dict[str, int] = {}

    def mint(
        self, *, issuer: str, subject: str, evidence_kind: str, surface: str, purpose: str, request_id: str,
        uses: int = 1,
    ) -> str:
        if isinstance(uses, bool) or not isinstance(uses, int) or not 1 <= uses <= MAX_PROOF_USES:
            raise ValueError(f"uses must be an int in 1..{MAX_PROOF_USES}, got {uses!r}")
        if evidence_kind not in _EVIDENCE_KINDS:
            raise ValueError(f"unknown evidence_kind: {evidence_kind!r}")
        now = _monotonic()
        raw_nonce = secrets.token_urlsafe(32)
        entry = _Entry(
            issuer=issuer, subject=subject, evidence_kind=evidence_kind, surface=surface,
            purpose=purpose, request_id=request_id, proof_id=_hash_nonce(raw_nonce),
            issued_at=now, deadline=now + ABANDONED_PROOF_BACKSTOP_SECONDS,
        )
        with self._lock:
            self._entries[raw_nonce] = entry
            self._uses[raw_nonce] = uses
        return raw_nonce

    def consume(
        self, raw_nonce: str, *, issuer: str, subject: str, surface: str, purpose: str, request_id: str,
    ) -> ConsumeResult:
        if not raw_nonce:
            return ConsumeResult(None, REASON_UNKNOWN_OR_ALREADY_CONSUMED)
        # ONE lock-guarded operation: the single atomic lookup-and-consume Amendment v2.1 #3 requires.
        # Two threads racing on the SAME nonce cannot both retrieve `entry` — the loser's pop returns
        # None, which is indistinguishable from "never minted" or "already consumed", on purpose (an
        # attacker replaying a captured nonce learns nothing about which case it was).
        with self._lock:
            entry = self._entries.get(raw_nonce)
            if entry is None:
                return ConsumeResult(None, REASON_UNKNOWN_OR_ALREADY_CONSUMED)
            # The backstop only ever REFUSES; it never extends a proof's life past one-time consumption.
            if _monotonic() > entry.deadline:
                self._entries.pop(raw_nonce, None)
                self._uses.pop(raw_nonce, None)
                return ConsumeResult(None, REASON_EXPIRED_BACKSTOP)
            if entry.issuer != issuer:
                self._entries.pop(raw_nonce, None)
                self._uses.pop(raw_nonce, None)
                return ConsumeResult(None, REASON_ISSUER_MISMATCH)
            if entry.subject != subject:
                self._entries.pop(raw_nonce, None)
                self._uses.pop(raw_nonce, None)
                return ConsumeResult(None, REASON_SUBJECT_MISMATCH)
            if entry.surface != surface:
                self._entries.pop(raw_nonce, None)
                self._uses.pop(raw_nonce, None)
                return ConsumeResult(None, REASON_SURFACE_MISMATCH)
            if entry.purpose != purpose:
                self._entries.pop(raw_nonce, None)
                self._uses.pop(raw_nonce, None)
                return ConsumeResult(None, REASON_PURPOSE_MISMATCH)
            if entry.request_id != request_id:
                self._entries.pop(raw_nonce, None)
                self._uses.pop(raw_nonce, None)
                return ConsumeResult(None, REASON_REQUEST_ID_MISMATCH)
            # Full match — decrement uses, pop when it reaches 0.
            uses_left = self._uses[raw_nonce] - 1
            if uses_left == 0:
                self._entries.pop(raw_nonce, None)
                self._uses.pop(raw_nonce, None)
            else:
                self._uses[raw_nonce] = uses_left
        return ConsumeResult(
            InteractionAuthorization(
                issuer=entry.issuer, subject=entry.subject, evidence_kind=entry.evidence_kind,
                surface=entry.surface, purpose=entry.purpose, request_id=entry.request_id,
                proof_id=entry.proof_id, issued_at=entry.issued_at,
            ),
            None,
        )

    def evict(self, raw_nonce: str) -> None:
        """Response-completion cleanup (Amendment v2.1 #4's PRIMARY lifetime, alongside one-time
        consumption): the minting caller discards an unconsumed proof once its OWN request is done, so
        it never lingers until the backstop. A no-op if already consumed or unknown — safe to call
        unconditionally from a `finally` block."""
        if not raw_nonce:
            return
        with self._lock:
            self._entries.pop(raw_nonce, None)
            self._uses.pop(raw_nonce, None)


@dataclass(frozen=True)
class _ChatEvidenceEntry:
    update_id: int
    recorded_at: float
    deadline: float


class _ChatEvidenceRegistry:
    """In-memory, lock-protected, one-time-take registry for chat owner-message evidence.

    Evidence travels from the poll thread (where `is_owner` is verified) to the executor thread
    (where the proof is minted) keyed by `task_id`. No nonce, no bearer material — only the
    `update_id` (chat's monotonic update sequence) and a timestamp. A second record for the
    same `task_id` raises `ValueError` — the slot is never overwritten (dev mode reuses one scratch task
    across turns; the caller degrades the duplicate to no evidence). Expired entries are refused and
    popped on `take`.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._entries: dict[int, _ChatEvidenceEntry] = {}

    def record(self, *, task_id: int, update_id: int) -> None:
        """Store evidence for `task_id`. Overwrite not allowed — raises `ValueError` if `task_id`
        already has evidence. Prunes expired entries on every call."""
        now = _monotonic()
        deadline = now + TELEGRAM_EVIDENCE_BACKSTOP_SECONDS
        with self._lock:
            if task_id in self._entries:
                raise ValueError(f"evidence already recorded for task_id={task_id}")
            # Prune expired
            expired = [tid for tid, e in self._entries.items() if now > e.deadline]
            for tid in expired:
                self._entries.pop(tid, None)
            self._entries[task_id] = _ChatEvidenceEntry(
                update_id=update_id, recorded_at=now, deadline=deadline
            )

    def take(self, task_id: int) -> int | None:
        """Atomically take and remove evidence for `task_id`. Returns the `update_id`, or `None`
        when absent, already taken, or older than the backstop (expired entries are popped and refused)."""
        now = _monotonic()
        with self._lock:
            entry = self._entries.pop(task_id, None)
        if entry is None:
            return None
        if now > entry.deadline:
            return None
        return entry.update_id


_CHAT_EVIDENCE_REGISTRY = _ChatEvidenceRegistry()
_REGISTRY = _Registry()

# The current-request proof, carried from `client_api`'s mint call site to `about_me_recall`'s consume
# call site WITHOUT threading a new parameter through `converse.py` / `build_converse_messages` /
# `recall_about_me` — none of which has any other reason to know a proof exists (they are not in this
# stage's "Files To Change", and adding a parameter to each would be exactly the kind of framework-wide
# change the staging note forbids). A `ContextVar` is the standard mechanism for exactly this shape of
# problem: ambient, request-scoped data threaded through a synchronous call chain the request owns.
# Safe under `ThreadingHTTPServer`'s one-thread-per-connection model — each request's `do_POST` and
# everything it calls synchronously (`stream_chat` -> `run_converse` -> `build_brain_context` -> ... ->
# `tiered_read`, all on ONE thread, well before `invoke_model` could ever hand work to another thread)
# runs in that connection's own thread, and a `ContextVar.set()` in a freshly spawned thread is NOT
# visible to any other thread's `.get()` — Python does not propagate context across `threading.Thread`
# boundaries the way `asyncio` propagates it across tasks.
_current_proof: ContextVar[ProofHandle | None] = ContextVar(
    "ledger_interaction_authorization_current_proof", default=None
)

_current_tool_allowance: ContextVar[ProofHandle | None] = ContextVar(
    "ledger_interaction_authorization_tool_allowance", default=None
)


def mint_interaction_authorization(
    *, issuer: str, subject: str, evidence_kind: str, surface: str, purpose: str, request_id: str,
    uses: int = 1,
) -> str:
    """The registry's raw mint. Deliberately NOT the security boundary by itself — it mints whatever
    it is asked to mint. A3 ('nothing else mints, ever') is enforced by WHO calls this: stage 1362a has
    exactly one call site, `client_api._mint_chat_interaction_authorization`, which refuses to call
    this at all unless the CURRENT REQUEST carries a bearer token that verifies, in the funnel
    database, as a non-revoked, T1-ceiling device (Amendment v2.1 #2). A background caller — a tick, a
    retry, a scheduler, the synthetic-loopback bypass — presents no such token and never reaches here.
    `uses` > 1 is only for the tool allowance (`mint_tool_allowance`); every other mint stays single-use."""
    return _REGISTRY.mint(
        issuer=issuer, subject=subject, evidence_kind=evidence_kind, surface=surface,
        purpose=purpose, request_id=request_id, uses=uses,
    )


def mint_tool_allowance(
    *, issuer: str, subject: str, evidence_kind: str, surface: str, request_id: str,
) -> str:
    """Mint the context tool's per-reply T1 allowance. Called ONLY by the two ingress sites,
    immediately after their own successful `PURPOSE_MATERIALIZE_T1` mint for the same request,
    and only when the owner switched `context.tool_reads_t1` on. The proof is bound to the tool's
    own surface (`<surface>:tool`) and purpose `tool_read_t1`, so the briefing cannot spend it and
    the tool cannot spend the briefing's proof."""
    return _REGISTRY.mint(
        issuer=issuer, subject=subject, evidence_kind=evidence_kind,
        surface=surface + TOOL_SURFACE_SUFFIX, purpose=PURPOSE_TOOL_READ_T1,
        request_id=request_id, uses=MAX_PROOF_USES,
    )


def consume_interaction_authorization(
    raw_nonce: str, *, issuer: str, subject: str, surface: str, purpose: str, request_id: str,
) -> ConsumeResult:
    """`tiered_read`'s ONLY way to turn a presented nonce into an :class:`InteractionAuthorization`.
    Every field is checked against what was actually minted; ANY mismatch (or an unknown/already-
    consumed/expired nonce) fails closed with a distinguishable reason and grants nothing (Part B2)."""
    return _REGISTRY.consume(
        raw_nonce, issuer=issuer, subject=subject, surface=surface, purpose=purpose, request_id=request_id,
    )


def evict_interaction_authorization(raw_nonce: str) -> None:
    """See `_Registry.evict` — response-completion cleanup, called by the minting request's own
    handler, never by anything else."""
    _REGISTRY.evict(raw_nonce)


def record_telegram_owner_evidence(*, task_id: int, update_id: int) -> None:
    """Record that an owner-gated chat update (identified by `update_id`) created `task_id`.
    Called on the poll thread immediately after the `is_owner` gate, before enqueueing. The evidence
    is taken exactly once by `take_telegram_owner_evidence` on the executor thread at claim time.
    Raises `ValueError` if `task_id` already has evidence (never overwritten)."""
    _CHAT_EVIDENCE_REGISTRY.record(task_id=task_id, update_id=update_id)


def take_telegram_owner_evidence(task_id: int) -> int | None:
    """Take the owner-message evidence for `task_id` (one atomic pop). Returns the `update_id` or
    `None` if absent, already taken, or expired (older than `TELEGRAM_EVIDENCE_BACKSTOP_SECONDS`).
    Called on the executor thread immediately before minting the proof for a `converse` run."""
    return _CHAT_EVIDENCE_REGISTRY.take(task_id)


def bind_current_proof(handle: ProofHandle):
    """Bind `handle` as THIS request's current proof. Returns the `ContextVar` reset token — the
    caller MUST pass it to `clear_current_proof` once the request is done (mirrors `contextvars`' own
    `set`/`reset` contract; a bare `.set()` with no matching reset would leak the previous request's
    context var value across... nothing, in this single-assignment-per-thread usage, but the explicit
    pairing is the same discipline `db.connect`'s `with`/`finally` uses throughout this codebase)."""
    return _current_proof.set(handle)


def clear_current_proof(reset_token) -> None:
    """Undo `bind_current_proof`. Safe to call from a `finally` block even if minting never happened
    (the caller simply never calls this in that case — see `client_api`)."""
    _current_proof.reset(reset_token)


def peek_current_proof() -> ProofHandle | None:
    """Read (without clearing) whatever proof is bound for the calling request, or None when no proof
    was minted for it (every caller before 1362b/1362c land, and any request whose own credential did
    not qualify under Amendment v2.1 #2). Reading twice is harmless — only `tiered_read`'s own atomic
    `consume_interaction_authorization` call can ever actually spend it."""
    return _current_proof.get()


def bind_tool_allowance(handle: ProofHandle):
    """Bind `handle` as THIS request's current tool allowance. Returns the `ContextVar` reset token —
    the caller MUST pass it to `clear_tool_allowance` once the request is done."""
    return _current_tool_allowance.set(handle)


def clear_tool_allowance(reset_token) -> None:
    """Undo `bind_tool_allowance`. Safe to call from a `finally` block."""
    _current_tool_allowance.reset(reset_token)


def peek_tool_allowance() -> ProofHandle | None:
    """Read (without clearing) whatever tool allowance is bound for the calling request, or None when
    no tool allowance was minted for it. Reading twice is harmless — only `tiered_read`'s own atomic
    `consume_interaction_authorization` call can ever actually spend it."""
    return _current_tool_allowance.get()


__all__ = [
    "ABANDONED_PROOF_BACKSTOP_SECONDS",
    "EVIDENCE_KIND_DEVICE_REQUEST",
    "EVIDENCE_KIND_TELEGRAM_OWNER_MESSAGE",
    "EVIDENCE_KIND_TELEGRAM_OWNER_VOICE",
    "OWNER_SUBJECT",
    "PURPOSE_MATERIALIZE_T1",
    "REASON_EXPIRED_BACKSTOP",
    "REASON_ISSUER_MISMATCH",
    "REASON_PURPOSE_MISMATCH",
    "REASON_REQUEST_ID_MISMATCH",
    "REASON_SUBJECT_MISMATCH",
    "REASON_SURFACE_MISMATCH",
    "REASON_UNKNOWN_OR_ALREADY_CONSUMED",
    "TELEGRAM_EVIDENCE_BACKSTOP_SECONDS",
    "ConsumeResult",
    "InteractionAuthorization",
    "ProofHandle",
    "bind_current_proof",
    "clear_current_proof",
    "consume_interaction_authorization",
    "evict_interaction_authorization",
    "mint_interaction_authorization",
    "peek_current_proof",
    "record_telegram_owner_evidence",
    "take_telegram_owner_evidence",
    "MAX_PROOF_USES",
    "PURPOSE_TOOL_READ_T1",
    "TOOL_SURFACE_SUFFIX",
    "bind_tool_allowance",
    "clear_tool_allowance",
    "mint_tool_allowance",
    "peek_tool_allowance",
]
