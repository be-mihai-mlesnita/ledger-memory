"""the typed claim envelope (RECALL-INTEGRITY v3 §R3.1 core + §R3.2 structural).

A model can cite a REAL id for a WRONG claim; before this slice it could also cite nothing at all,
and the runtime could not tell. This module is the validation half of the fix: the prompt asks the
model to emit, after its prose answer, one fenced JSON "claim envelope" describing which of its own
sentences are grounded in which numbered memory-block entries (C3). The runtime NEVER trusts the
model's self-report at face value — every ref is resolved through the :class:`EvidenceManifest`, a
deterministic table built by the context builder from what ACTUALLY shipped in the prompt (C1/C2),
never from the model's word.

No new wire machinery: prompt-and-parse, per the dispatcher's `_parse_job_class` precedent
(fence-strip, ``json.loads``, closed-set/schema check, fail toward the caller's own bounded retry).

Non-goals (spec-pinned): no semantic entailment / NLI pass (no ``support_checked`` state ships
tonight); no R2.1 relation-edge grounding (v0 grounds on "refs valid + kind allowed" only).
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass

# --- claim kinds --------------------------------------------------------------

KIND_BIOGRAPHICAL = "biographical"
KIND_TEMPORAL = "temporal"
KIND_CAUSAL = "causal"
KIND_TREATMENT = "treatment"
KIND_IDENTITY = "identity"
KIND_GENERAL = "general"

VALID_KINDS = frozenset({
    KIND_BIOGRAPHICAL, KIND_TEMPORAL, KIND_CAUSAL, KIND_TREATMENT, KIND_IDENTITY, KIND_GENERAL,
})

# Owner-life claim kinds (C3 "coverage"): everything but small talk / general chit-chat.
OWNER_LIFE_KINDS = frozenset({
    KIND_BIOGRAPHICAL, KIND_TEMPORAL, KIND_CAUSAL, KIND_TREATMENT, KIND_IDENTITY,
})

# R3.2 structural core — the high-risk RELATION kinds that fail closed on ungrounded refs.
HIGH_RISK_KINDS = frozenset({KIND_CAUSAL, KIND_TREATMENT, KIND_IDENTITY})

# The fixed withheld-claim line (C3 — verbatim, never paraphrased, so a test/UI can match it exactly).
WITHHELD_LINE = "…a claim was withheld: it could not be substantiated from your records…"

# `_parse_job_class` shape (dispatch_strategies.py): a bounded, small retry count — TOTAL attempts,
# not "retries on top of one" — matching `LLMDispatchStrategy.max_parse_attempts`'s own default.
MAX_ENVELOPE_ATTEMPTS = 2

_FENCE_RE = re.compile(r"```(?:json)?\s*\n?(.*?)```", re.DOTALL)


# --- the evidence manifest (C1/C2) --------------------------------------------


@dataclass(frozen=True)
class ManifestRow:
    """One numbered memory-block entry, as it ACTUALLY shipped in the prompt (emitted AFTER all
    truncation — the v2 rule). ``content_sha256`` is over the shipped (already-capped/one-lined)
    statement text, never the raw database row — a receipt of what the model could literally see."""

    index: str                      # the menu index the model must cite ("0", "1", "2", …)
    record_id: int
    sensitivity_tier: int | None
    known_at: str | None
    source_label: str | None
    superseded: bool
    content_sha256: str
    # a briefing cites facts ("0"), decisions ("d0") and interpretations ("i0"); defaulted so every
    # existing constructor call keeps meaning a fact.
    record_type: str = "fact"


@dataclass(frozen=True)
class EvidenceManifest:
    """A deterministic table the validator actually READS (the mem0 lesson: mem0 built a map and
    never read it back; we read it — C2)."""

    rows: tuple[ManifestRow, ...] = ()

    def resolve(self, ref: str) -> ManifestRow | None:
        """Index-only lookup (C2). A ref that is anything but one of THIS manifest's own menu
        indices — a raw record id, a made-up id-shaped literal, an out-of-range index, a non-index
        token — resolves to None, exactly like a genuinely unknown index would."""
        for row in self.rows:
            if row.index == ref:
                return row
        return None

    def __len__(self) -> int:
        return len(self.rows)


# --- claims --------------------------------------------------------------------


@dataclass(frozen=True)
class Claim:
    text: str
    kind: str
    refs: tuple[str, ...] = ()


@dataclass(frozen=True)
class ValidatedClaim:
    text: str
    kind: str
    refs: tuple[str, ...]                     # the raw ref tokens the model cited
    resolved: tuple[ManifestRow, ...]         # the subset that actually resolved through the manifest
    coverage: bool                             # C3 "coverage": the claim carries >=1 ref (any ref)
    membership_valid: bool                     # C3 "membership_valid": EVERY ref resolves, and >=1 exists
    suppressed: bool                            # R3.2 fail-closed: a high-risk kind with !membership_valid


# --- envelope instructions (injected as their OWN system message; C1/C3) -------

ENVELOPE_INSTRUCTIONS = (
    "After you finish your prose answer, on new lines, emit exactly ONE fenced JSON code block — the "
    "claim envelope. Format:\n"
    '```json\n{"claims": [{"text": "<the exact sentence from your answer>", '
    '"kind": "biographical|temporal|causal|treatment|identity|general", "refs": ["0", "2"]}]}\n```\n'
    "Rules for the envelope:\n"
    "- One entry per factual claim in your answer. A claim not grounded in any RECALLED MEMORY "
    "line still gets an entry, with \"refs\": [].\n"
    "- \"text\" MUST be copied VERBATIM from your own prose answer above (an exact substring) — never "
    "paraphrased, never summarized.\n"
    "- \"refs\" cites the bracketed MENU INDEX of each memory line that supports the claim — e.g. the "
    "line \"[0] [2026-01-01] ...\" is cited as \"0\". NEVER cite a record id, a date, or any other "
    "identifier — indices ONLY, and only indices that actually appear in the memory block above.\n"
    "- \"kind\" is exactly one of: biographical, temporal, causal, treatment, identity, general. Use "
    "causal/treatment/identity ONLY for a claim that asserts a cause, a treatment/effect relationship, "
    "or an identity fact — and only when a memory line actually supports it.\n"
    "- Small talk / opinions / claims about yourself (the memory system) needing no stored-memory grounding are "
    '"kind": "general" with "refs": [].'
)


# --- parsing (C3 — prompt-and-parse, the `_parse_job_class` shape) -------------


def parse_envelope(raw_text: str) -> tuple[str, list[Claim] | None]:
    """Locate the LAST fenced code block, parse it as the claim envelope, and return
    ``(visible_text, claims)``. ``claims`` is ``None`` when no envelope could be parsed — a whole-
    parse failure (mirrors `_parse_job_class`'s closed-set-or-None shape), never a partial one: a
    single malformed claim invalidates this WHOLE attempt, so the caller's bounded retry re-asks the
    model rather than silently dropping one claim.

    ``visible_text`` is the prose with the envelope fence removed when a fence was found (whether or
    not it parsed) — a stray/garbled fence must never leak into what the owner reads; when NO fence
    exists at all, ``visible_text`` is the raw text, unchanged.
    """
    matches = list(_FENCE_RE.finditer(raw_text))
    if not matches:
        return raw_text, None

    last = matches[-1]
    visible_text = (raw_text[: last.start()] + raw_text[last.end():]).strip()
    fenced = last.group(1).strip()

    try:
        obj = json.loads(fenced)
    except (json.JSONDecodeError, ValueError):
        return visible_text, None
    if not isinstance(obj, dict):
        return visible_text, None
    raw_claims = obj.get("claims")
    if not isinstance(raw_claims, list) or not raw_claims:
        return visible_text, None

    claims: list[Claim] = []
    for item in raw_claims:
        if not isinstance(item, dict):
            return visible_text, None
        text = item.get("text")
        kind = item.get("kind")
        refs = item.get("refs")
        if not isinstance(text, str) or not text.strip():
            return visible_text, None
        if not isinstance(kind, str) or kind not in VALID_KINDS:
            return visible_text, None
        if not isinstance(refs, list) or not all(isinstance(r, (str, int)) for r in refs):
            return visible_text, None
        claims.append(Claim(text=text.strip(), kind=kind, refs=tuple(str(r) for r in refs)))

    return visible_text, claims


# Conservative content-based detector, used ONLY when the envelope is missing/unparseable after every
# retry (C3). It leans toward FALSE POSITIVES (flagging plain prose as owner-life content) on purpose
# — fail-toward-escalate: a missed owner-life claim shipping unmarked is the bad outcome, an
# unnecessary escalation is merely annoying.
_DATE_HINT_RE = re.compile(r"\b(19|20)\d{2}\b|\b\d{1,2}[/-]\d{1,2}([/-]\d{2,4})?\b")
_OWNER_LIFE_HINTS = (
    "your name", "you are", "you were", "your family", "your wife", "your husband", "your partner",
    "your child", "your daughter", "your son", "your mother", "your father", "your birthday",
    "your address", "your job", "your work", "your employer", "your diagnosis", "your condition",
    "your symptom", "your medication", "your treatment", "your therapy", "your prescri",
    " because ", " caused by", " due to", " treats ", " helps with", " is why ",
    "diagnosis", "medication", "treatment", "condition", "symptom", "therapy", "prescribed",
)


def looks_like_owner_life(text: str) -> bool:
    """The validator's OWN conservative detector (C3) — used only to decide, when the model gave no
    parseable envelope at all, whether it is safe to ship the raw prose unmarked (`support_unverified`)
    or whether the missing grounding must escalate instead."""
    lowered = text.lower()
    if _DATE_HINT_RE.search(lowered):
        return True
    return any(hint in lowered for hint in _OWNER_LIFE_HINTS)


# --- validation (C2 map-back + R3.2 fail-closed) -------------------------------


def validate_claims(claims: list[Claim], manifest: EvidenceManifest) -> list[ValidatedClaim]:
    """Map-back through the manifest — a table the validator actually READS. Any ref that is not
    exactly one of the manifest's own indices (a non-index token, an out-of-range index, or an
    id-shaped literal that merely LOOKS like a citation) fails to resolve (C2)."""
    validated: list[ValidatedClaim] = []
    for claim in claims:
        resolved = tuple(row for row in (manifest.resolve(r) for r in claim.refs) if row is not None)
        coverage = len(claim.refs) > 0
        membership_valid = coverage and len(resolved) == len(claim.refs)
        suppressed = claim.kind in HIGH_RISK_KINDS and not membership_valid
        validated.append(ValidatedClaim(
            text=claim.text, kind=claim.kind, refs=claim.refs, resolved=resolved,
            coverage=coverage, membership_valid=membership_valid, suppressed=suppressed,
        ))
    return validated


@dataclass(frozen=True)
class EnvelopeResolution:
    """One parse+validate attempt's outcome (used inside the caller's bounded retry loop)."""

    parsed: bool
    visible_text: str
    claims: tuple[ValidatedClaim, ...] = ()


def resolve_envelope(raw_text: str, manifest: EvidenceManifest) -> EnvelopeResolution:
    visible_text, claims = parse_envelope(raw_text)
    if claims is None:
        return EnvelopeResolution(parsed=False, visible_text=visible_text)
    validated = tuple(validate_claims(claims, manifest))
    return EnvelopeResolution(parsed=True, visible_text=visible_text, claims=validated)


def render_reply(visible_text: str, claims: tuple[ValidatedClaim, ...] | list[ValidatedClaim]) -> str:
    """Apply R3.2's fail-closed suppression: each suppressed high-risk claim's OWN text is replaced
    by the fixed withheld line — never silently stripped to plain (unmarked) prose. The prompt asks
    the model to copy claim text VERBATIM from its own answer (C3) so this is normally an exact
    substring replace; when a claim's text cannot be located verbatim (the model did not comply), the
    safe fail-closed fallback is to withhold the WHOLE reply rather than leave an unlocatable,
    ungrounded high-risk claim visible."""
    text = visible_text
    for claim in claims:
        if not claim.suppressed:
            continue
        if claim.text in text:
            text = text.replace(claim.text, WITHHELD_LINE, 1)
        else:
            lowered = text.lower()
            idx = lowered.find(claim.text.lower())
            if idx != -1:
                text = text[:idx] + WITHHELD_LINE + text[idx + len(claim.text):]
            else:
                # Cannot locate the claim's own text to redact it — fail closed on the WHOLE reply
                # rather than risk an ungrounded high-risk claim remaining visible verbatim.
                return WITHHELD_LINE
    return text


def citations_payload(claims: tuple[ValidatedClaim, ...] | list[ValidatedClaim]) -> list[dict]:
    """C5 — the additive `citations` SSE frame body: per-claim refs resolved to
    ``{record_id, known_at, source_label, superseded}``. Only claims carrying at least one VALID
    resolved ref are represented (nothing to cite otherwise)."""
    payload = []
    for claim in claims:
        if not claim.resolved:
            continue
        # Review BLOCK fix (2026-08-07): a SUPPRESSED claim's text was withheld from the reply —
        # emitting its partially-valid refs in the citations frame would hand it back the very
        # authority the suppression removed. Suppressed claims cite NOTHING.
        if claim.suppressed:
            continue
        payload.append({
            "kind": claim.kind,
            "refs": [
                {
                    "record_id": row.record_id,
                    "known_at": row.known_at,
                    "source_label": row.source_label,
                    "superseded": row.superseded,
                }
                for row in claim.resolved
            ],
        })
    return payload


def content_sha256(statement: str) -> str:
    return hashlib.sha256(statement.encode("utf-8")).hexdigest()
