"""Search result preview resolver (NEM-1290).

MEASURED on the live store, 2026-08-01: `store search "first heart problem started"`
returned 20 hits and every single one rendered as the word `observation`, because for a fact the
indexed *title* column holds `fact_type` while the actual STATEMENT lives in the body. The owner
could not tell what any result contained without running `show-fact` once per row.
"""
from __future__ import annotations

from pathlib import Path

from ledger_memory.db import connect_readonly


def previews(database_path, results, *, max_chars: int = 100) -> dict[tuple[str, int], str]:
    """Return preview text for search results. DISPLAY ONLY — changes what the user reads, never
    what ranks where.
    """
    results_list = list(results)
    fact_ids = [r.record_id for r in results_list if r.record_type == "fact"]

    fact_statements: dict[int, str] = {}
    if fact_ids:
        placeholders = ",".join("?" for _ in fact_ids)
        with connect_readonly(database_path) as connection:
            rows = connection.execute(
                f"SELECT id, statement FROM facts WHERE id IN ({placeholders})",
                tuple(fact_ids),
            ).fetchall()
        for row in rows:
            stmt = row["statement"]
            if stmt is None:
                continue
            fact_statements[row["id"]] = _normalise(stmt, max_chars)

    out: dict[tuple[str, int], str] = {}
    for r in results_list:
        key = (r.record_type, r.record_id)
        if r.record_type == "fact":
            if r.record_id in fact_statements:
                out[key] = fact_statements[r.record_id]
        else:
            # normalise EVERY record type, not only facts. The first version returned a
            # non-fact title verbatim, so a 300-character context_package title was printed twice —
            # once as `title:` and again on a line advertised as a <=100-char preview. A preview that
            # is not previewed is a broken promise, and one that repeats the line above it is noise.
            out[key] = _normalise(r.title, max_chars)
    return out


def _normalise(text: str | None, max_chars: int) -> str:
    """One line, whitespace-collapsed, truncated at *max_chars* with a single `…`.
    The returned string is never longer than *max_chars* (invariant: len(result) <= max_chars).
    Previous versions returned max_chars + 1 because the ellipsis was appended outside the cap.
    """
    if max_chars <= 0:
        return ""
    collapsed = " ".join((text or "").replace("\r", " ").replace("\n", " ").replace("\t", " ").split())
    if len(collapsed) > max_chars:
        return collapsed[:max_chars - 1] + "…"
    return collapsed
