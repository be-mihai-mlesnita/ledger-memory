from __future__ import annotations

"""the bitemporal belief resolver. the requirement made executable.

One primitive answers both of an earlier fix's questions — "what did ledger believe at time T?"
(as-known: transaction time) and "what was true of the world at time T?" (as-valid: valid time)
— over columns that have existed since the baseline schema's. The rules that must not soften:

- **The past is reconstructed, never rewritten.** An as-known query excludes a fact only when
  its SUCCESSOR was already known at the asked time; the superseded row itself is part of
  history and stays reachable through the chain links every timepoint carries.
- **Ambiguity is transparent.** A fact without `valid_from` cannot answer a valid-time question;
  it lands in the view's `ambiguous` bucket with the named reason — never silently included,
  never silently dropped.
- **Conflicts surface; nothing suppresses them.** Conflict objects are computed, deterministic,
  and carry each side's `authority_class` plus the deterministic authority ORDER as context for
  the CALLER — the resolver never picks a winner (§8.2: authority-based auto-resolution is the
  suppression the mandate's "zero suppressed conflicts" forbids).
- **Content-free.** Timepoints carry ids, statuses, tiers and windows — content stays behind
  `tiered_read`; this module opens no new read door.
"""

import json
from dataclasses import dataclass, field
from pathlib import Path

from ledger_memory.db import connect_readonly


def _norm(ts: str | None) -> str | None:
    """Normalize the timestamp separator before ANY lexical comparison (review blocker 1,
    reproduced against the real write path: SQLite's CURRENT_TIMESTAMP renders space-separated
    ("2026-08-28 09:42:51") while ISO input uses "T" — and ' ' (0x20) sorts below 'T' (0x54),
    so a same-day space-format known_at compared lexically against a T-format --as-known
    resolves on the SEPARATOR, not the clock: the correction appeared already-believed hours
    early — the exact past-rewriting the requirement forbids. One replace makes both forms identical;
    date-only strings stay prefix-comparable."""
    return ts.replace(" ", "T") if ts is not None else None

# Deterministic authority order, MOST authoritative first — attached to conflict objects as
# context, never used to auto-resolve (§8.2).
AUTHORITY_ORDER = ("evidence_attested", "actor_asserted", "system_observed",
                   "model_inferred", "legacy_unclassified")


class TemporalViewError(ValueError):
    """A refused view operation — the message names the exact reason."""


@dataclass(frozen=True, kw_only=True)
class FactTimepoint:
    """One fact as seen from the asked timepoint. Content-free by design."""

    fact_id: int
    status: str
    sensitivity_tier: int
    authority_class: str
    known_at: str
    valid_from: str | None
    valid_to: str | None
    supersedes: tuple[int, ...]  # the fact(s) this one superseded — N>2 fan-in is real (chain walk, backward)
    superseded_by: int | None   # the fact that superseded this one (chain walk, forward)


@dataclass(frozen=True, kw_only=True)
class TemporalView:
    as_known: str | None
    as_valid: str | None
    current: tuple[FactTimepoint, ...]      # the beliefs standing at the asked time
    superseded: tuple[FactTimepoint, ...]   # history visible from the asked time (chain-walkable)
    ambiguous: tuple[dict, ...]             # {fact_id, reason} — transparent, never silent


@dataclass(frozen=True, kw_only=True)
class ConflictObject:
    kind: str                   # 'dispute' | 'supersession_anomaly' | 'valid_overlap_dispute'
    fact_a: int
    fact_b: int
    authority_a: str
    authority_b: str
    detail: str
    authority_order: tuple[str, ...] = field(default=AUTHORITY_ORDER)


def _rows(connection, fact_id: int | None):
    return connection.execute(
        "SELECT id, status, sensitivity_tier, "
        "COALESCE(authority_class, 'legacy_unclassified') AS authority_class, known_at, "
        "valid_from, valid_to, superseded_by_fact_id FROM facts"
        + (" WHERE id = ?" if fact_id is not None else "") + " ORDER BY known_at, id",
        (fact_id,) if fact_id is not None else (),
    ).fetchall()


def fact_view(database_path: Path, *, as_known: str | None = None,
              as_valid: str | None = None, fact_id: int | None = None) -> TemporalView:
    """The one bitemporal read. Timestamps are ISO strings compared lexically — the same
    convention every temporal column in this schema already relies on."""
    as_known = _norm(as_known)
    as_valid = _norm(as_valid)
    with connect_readonly(database_path) as connection:
        rows = _rows(connection, fact_id)
        # Both maps come from the FULL table (review blocker 2: building the predecessor map
        # from --id-filtered rows silently lost the backward chain link on a scoped query).
        known_at_by_id: dict[int, str] = {}
        supersedes_map: dict[int, list[int]] = {}
        for r in connection.execute("SELECT id, known_at, superseded_by_fact_id FROM facts"):
            known_at_by_id[int(r["id"])] = str(r["known_at"])
            if r["superseded_by_fact_id"] is not None:
                # A LIST per successor (review blocker 3: genuine N>2 fan-in — several facts
                # superseded by one successor — is a documented schema capability
                # (facts.py's get_superseded_attestations); a one-to-one dict silently
                # dropped all but the last predecessor).
                supersedes_map.setdefault(int(r["superseded_by_fact_id"]), []).append(int(r["id"]))

    current: list[FactTimepoint] = []
    superseded: list[FactTimepoint] = []
    ambiguous: list[dict] = []

    for r in rows:
        fid = int(r["id"])
        known = str(r["known_at"])
        if as_known is not None and _norm(known) > as_known:
            continue  # not yet known at the asked time — genuinely outside the view
        successor = r["superseded_by_fact_id"]
        superseded_at_asked_time = False
        if successor is not None:
            successor_known = _norm(known_at_by_id.get(int(successor)))
            # The successor hides this fact only once IT was known (as-known semantics: the
            # past is reconstructed — at a time before the correction, the old belief stood).
            if as_known is None or (successor_known is not None
                                    and successor_known <= as_known):
                superseded_at_asked_time = True

        if as_valid is not None:
            if r["valid_from"] is None:
                ambiguous.append({"fact_id": fid,
                                  "reason": "valid_from is NULL — this fact cannot answer a "
                                            "valid-time question (transparent ambiguity)"})
                continue
            if not (_norm(str(r["valid_from"])) <= as_valid
                    and (r["valid_to"] is None or as_valid < _norm(str(r["valid_to"])))):
                continue  # outside the valid window — unambiguously not applicable

        point = FactTimepoint(
            fact_id=fid, status=str(r["status"]), sensitivity_tier=int(r["sensitivity_tier"]),
            authority_class=str(r["authority_class"]), known_at=known,
            valid_from=r["valid_from"], valid_to=r["valid_to"],
            supersedes=tuple(sorted(supersedes_map.get(fid, ()))),
            superseded_by=int(successor) if successor is not None
            and superseded_at_asked_time else None,
        )
        (superseded if superseded_at_asked_time else current).append(point)

    return TemporalView(as_known=as_known, as_valid=as_valid,
                        current=tuple(current), superseded=tuple(superseded),
                        ambiguous=tuple(ambiguous))


def conflicts(database_path: Path) -> tuple[ConflictObject, ...]:
    """Deterministic conflict objects; no suppression path exists. Sorted (kind, fact_a, fact_b)."""
    out: list[ConflictObject] = []
    with connect_readonly(database_path) as connection:
        facts = {int(r["id"]): dict(r) for r in _rows(connection, None)}
        disputes = connection.execute(
            "SELECT json_extract(metadata, '$.fact_id') AS fact_id, "
            "json_extract(metadata, '$.disputed_by_fact_id') AS by_id, metadata "
            "FROM events WHERE event_type = 'fact.disputed' ORDER BY id",
        ).fetchall()

    seen_pairs: set[tuple[int, int]] = set()
    seen_single: set[int] = set()
    for d in disputes:
        if d["fact_id"] is None:
            continue
        a = int(d["fact_id"])
        b = int(d["by_id"]) if d["by_id"] is not None else None
        fa = facts.get(a)
        if fa is None:
            continue
        if b is not None and b in facts:
            pair = (min(a, b), max(a, b))
            if pair in seen_pairs:
                continue
            seen_pairs.add(pair)
            fb = facts[b]
            if fa["status"] == "active" and fb["status"] == "active":
                out.append(ConflictObject(
                    kind="dispute", fact_a=pair[0], fact_b=pair[1],
                    authority_a=str(facts[pair[0]]["authority_class"]),
                    authority_b=str(facts[pair[1]]["authority_class"]),
                    detail="both sides of a recorded dispute are ACTIVE"))
                overlap = _windows_overlap(fa, fb)
                if overlap:
                    out.append(ConflictObject(
                        kind="valid_overlap_dispute", fact_a=pair[0], fact_b=pair[1],
                        authority_a=str(facts[pair[0]]["authority_class"]),
                        authority_b=str(facts[pair[1]]["authority_class"]),
                        detail=f"disputed AND valid windows overlap ({overlap})"))
        elif fa["status"] == "active":
            if a in seen_single:
                continue  # review major 1: a re-disputed fact is ONE open conflict, not N
            seen_single.add(a)
            out.append(ConflictObject(
                kind="dispute", fact_a=a, fact_b=a,
                authority_a=str(fa["authority_class"]), authority_b=str(fa["authority_class"]),
                detail="a recorded dispute names no counter-fact and the disputed fact is "
                       "still ACTIVE — an unresolved open dispute"))

    for fid, r in facts.items():
        if r["superseded_by_fact_id"] is not None and r["status"] == "active":
            out.append(ConflictObject(
                kind="supersession_anomaly", fact_a=fid,
                fact_b=int(r["superseded_by_fact_id"]),
                authority_a=str(r["authority_class"]),
                authority_b=str(facts.get(int(r["superseded_by_fact_id"]),
                                          {"authority_class": "legacy_unclassified"}
                                          )["authority_class"]),
                detail="superseded yet still ACTIVE — the double-active chain defect"))

    return tuple(sorted(out, key=lambda c: (c.kind, c.fact_a, c.fact_b)))


def _windows_overlap(fa: dict, fb: dict) -> str | None:
    if fa["valid_from"] is None or fb["valid_from"] is None:
        return None  # ambiguity is not an overlap claim — transparent elsewhere, not inflated here
    start = max(str(fa["valid_from"]), str(fb["valid_from"]))
    ends = [e for e in (fa["valid_to"], fb["valid_to"]) if e is not None]
    end = min(map(str, ends)) if len(ends) == 2 else (str(ends[0]) if ends else None)
    if end is None or start < end:
        return f"{start} .. {end or 'open'}"
    return None


def render_view(view: TemporalView) -> str:
    """Content-free rendering for the CLI (ids/windows/chains only — content stays behind
    tiered_read)."""
    lines = [f"as_known: {view.as_known or 'now'}  as_valid: {view.as_valid or '(not asked)'}"]
    lines.append(f"current beliefs: {len(view.current)}")
    for p in view.current:
        window = f"[{p.valid_from or '?'} .. {p.valid_to or 'open'}]"
        chain = (f" supersedes {','.join(map(str, p.supersedes))}" if p.supersedes else "")
        lines.append(f"  fact {p.fact_id} [{p.status} T{p.sensitivity_tier} "
                     f"{p.authority_class}] known {p.known_at} valid {window}{chain}")
    if view.superseded:
        lines.append(f"superseded at this time: {len(view.superseded)}")
        for p in view.superseded:
            lines.append(f"  fact {p.fact_id} → superseded by {p.superseded_by}")
    if view.ambiguous:
        lines.append(f"ambiguous: {len(view.ambiguous)}")
        for a in view.ambiguous:
            lines.append(f"  fact {a['fact_id']}: {a['reason']}")
    return "\n".join(lines)


__all__ = [
    "AUTHORITY_ORDER",
    "ConflictObject",
    "FactTimepoint",
    "TemporalView",
    "TemporalViewError",
    "conflicts",
    "fact_view",
    "render_view",
]
