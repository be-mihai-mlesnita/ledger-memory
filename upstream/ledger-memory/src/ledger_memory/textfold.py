"""one deterministic Unicode folding primitive.

The LIKE lanes (`candidates._like_lane_fact_ids`, `search.search_records`) are byte-exact:
measured `'%inimă%'` misses `inima` and `'%MĂ%'` misses `Mă` (ASCII-only case folding, no
diacritic normalization). `fold_for_match` is the ONE place that folding logic lives — NFD
decompose, strip combining marks, casefold, collapse whitespace — so every lane that needs
diacritic/case-insensitive literal matching shares the identical rule rather than growing its
own slightly-different regex.

Pure, dependency-free (stdlib `re` + `unicodedata` only), and registered verbatim as the
`ledger_fold` deterministic SQLite scalar function on the LIKE lanes' own connections — never
imported by, and never touching, `unified_search.py` (PROTECTED) or the FTS schema/tokenizer
(measured innocent: `search_index`'s default `unicode61 remove_diacritics=1` already folds
both sides of an FTS MATCH).
"""
from __future__ import annotations

import re
import unicodedata

_WHITESPACE_RUN = re.compile(r"\s+")


def fold_for_match(text: str) -> str:
    """NFD-decompose *text*, drop Unicode combining marks (category Mn), casefold, and collapse
    any run of whitespace to a single space (leading/trailing stripped).

    Idempotent by construction: after the first pass no combining marks remain, casefolded text
    re-casefolds to itself, and collapsed whitespace re-collapses to itself — so
    ``fold_for_match(x) == fold_for_match(fold_for_match(x))`` for every *x*.

    Explicit Romanian pairs this exists to close: ``ă``/``â`` -> ``a``, ``î`` -> ``i``,
    ``ș`` -> ``s``, ``ț`` -> ``t`` (each has a canonical NFD decomposition into its base Latin
    letter + one combining mark, per Unicode).
    """
    decomposed = unicodedata.normalize("NFD", text)
    without_marks = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    folded = without_marks.casefold()
    return _WHITESPACE_RUN.sub(" ", folded).strip()


__all__ = ["fold_for_match"]


_ROMANIAN_DIACRITICS = frozenset("ăâîșțĂÂÎȘȚşţŞŢ")  # incl. legacy cedilla forms (ş/ţ)


def looks_romanian(text: str) -> bool:
    """Dumb, deterministic Romanian signal: the text contains at least one Romanian diacritic.

    Review fix (2026-08-07): the Romanian stopword filter must NEVER fire on English queries
    (collision tokens: 'mai'≈May, 'care', 'am', 'la'…), and language detection would be a new
    failure mode — a diacritic is a signal that costs nothing and never occurs in English text.
    An all-ASCII Romanian query simply skips the RO filter (its content words still carry it).
    """
    return any(ch in _ROMANIAN_DIACRITICS for ch in text)
