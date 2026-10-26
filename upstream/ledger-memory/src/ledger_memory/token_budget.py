from __future__ import annotations

import math

"""token budgeting + the resident-fits-context guard inputs.

A pure leaf module (no `lms`, no DB) so it can be imported anywhere. It provides:
- the small/medium/large **budget-tier vocabulary** callers can target when assembling context (a
  guideline — the HARD ceiling is whatever context the resident model is actually LOADED with);
- a conservative **token estimate** (no tokenizer ships with ledger): ASCII / European-Latin text ≈ chars/4
  (a slight over-estimate, so the fit-guard errs toward refusing-early), while CJK / emoji codepoints are
  counted ≈ 1 token each so multibyte text is not grossly UNDER-counted (which would let a real LM Studio
  HTTP 400 through). Emoji that tokenize to >1 token are still approximate, but the response reserve
  absorbs small slack;
- `fits_context`, the boundary check the `invoke_model` guard uses.

Why a floor of 8192: the registry `models.context_length` is the model CAPABILITY (e.g. gemma-4-e2b =
262144, the 26b front desk = None), NOT the loaded context. The safe budget is the LOADED context, which
for the arbiter path is `model_arbiter.DEFAULT_ARBITER_CONTEXT_LENGTH`. We mirror it here as
`DEFAULT_RESIDENT_CONTEXT` (a test asserts equality so they cannot drift) and keep this module
dependency-free.
"""

# Budget tiers — the context-budget vocabulary from the progressive-response analysis. A guideline for
# callers assembling context; the resident model's loaded context is the hard ceiling the guard enforces.
BUDGET_SMALL = 8192      # converse / routing / identity — sub-second prefill
BUDGET_MEDIUM = 30720    # worker tasks needing recent traces / ADRs
BUDGET_LARGE = 131072    # deep-read / RAG only (forces a high-context residency, heavier under the 36GB floor)

# The conservative loaded-context floor used when the caller does not state the load context. Mirrors
# model_arbiter.DEFAULT_ARBITER_CONTEXT_LENGTH (kept in sync by test_default_resident_context_matches_arbiter).
DEFAULT_RESIDENT_CONTEXT = 8192

# Headroom reserved below the context ceiling so the model has room to answer.
DEFAULT_RESPONSE_RESERVE = 1024

_CHARS_PER_TOKEN = 4   # ASCII + European-Latin ~4 chars/token; code is denser, so the estimate runs high (safe).
_PER_MESSAGE_OVERHEAD = 4  # role + chat delimiters per message.
# measured weights — see _estimate_text_tokens. Each digit and each non-ASCII letter count as one token.
_PUNCTUATION_WEIGHT = 1.5
_SPACE_WEIGHT = 0.5
# Codepoints at/above this boundary (CJK ideographs ~U+4E00, kana, emoji ~U+1F300, CJK symbols ~U+3000)
# tokenize to roughly >=1 token EACH — counting them as chars/4 grossly under-counts (a real 400 risk).
# Latin diacritics (Romanian ă/î/â/ș/ț, French, etc.) sit below U+3000, so they stay on the chars/4 path
# and do NOT inflate the estimate into a false-positive.
_DENSE_CODEPOINT_FLOOR = 0x3000


def _estimate_text_tokens(text: str) -> int:
    """Estimate tokens for a string, never below the real count of the local gemma tokenizer on the measured corpus.

    the previous chars/4 rule UNDER-counted structured text for gemma by ~36%:
    gemma spends one token per digit (a date "[2026-04-01]" is 12 tokens, chars/4 said 3), dense punctuation runs above
    chars/4, and Latin diacritics (Romanian ș ț ă î â) cost more than ASCII letters. Fitted on 397 samples (the repo's
    English docs, briefing lines, dates, tags, code, JSON, Romanian, emoji) against the real tokenizer, these weights
    never under-count, with a median over-count of ~1.5x on prose (the safe side for the fits-the-context guard; Jon's
    system prompt plus a full briefing still sits near half the guard's ceiling). Empty -> 0.
    """
    if not text:
        return 0
    letters = digits = spaces = dense = other_non_ascii = 0
    for ch in text:
        if ch.isascii():
            if ch.isalpha():
                letters += 1
            elif ch.isdigit():
                digits += 1
            elif ch.isspace():
                spaces += 1
        elif ord(ch) >= _DENSE_CODEPOINT_FLOOR:
            dense += 1
        else:
            other_non_ascii += 1
    punctuation = len(text) - letters - digits - spaces - dense - other_non_ascii
    return math.ceil(letters / _CHARS_PER_TOKEN + digits + punctuation * _PUNCTUATION_WEIGHT
                     + spaces * _SPACE_WEIGHT + other_non_ascii + dense)


def estimate_tokens(messages: list[dict] | str) -> int:
    """Estimate the prompt token count for a chat `messages` list (or a raw string).

    Heuristic and deliberately a slight OVER-estimate (chars/4 + per-message overhead) so the fit-guard
    refuses a borderline prompt slightly early (safe) rather than letting LM Studio 400.
    """
    if isinstance(messages, str):
        return _estimate_text_tokens(messages)
    total = 0
    for message in messages:
        content = message.get("content", "") if isinstance(message, dict) else str(message)
        total += _PER_MESSAGE_OVERHEAD + _estimate_text_tokens(str(content))
    return total


def fits_context(
    messages: list[dict] | str,
    *,
    context_length: int,
    reserve: int = DEFAULT_RESPONSE_RESERVE,
) -> tuple[bool, int]:
    """Return ``(fits, estimated_tokens)``. Fits iff ``estimate + reserve <= context_length``."""
    estimated = estimate_tokens(messages)
    return (estimated + reserve <= context_length), estimated
