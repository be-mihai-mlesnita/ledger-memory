from __future__ import annotations

"""evidence-bound entity proposals and reversible resolution.

The discipline, compressed:
- **Entities are proposals.** `propose_entities` reads LIVE speaker/entity extraction candidates
 and groups them by CODE-computed alias equality — no model
  anywhere in v1. Every entity-to-record link is a receipt: confidence + method + method version.
- **A merge is a pointer, not a rewrite.** `merge_entity` sets `canonical_entity_id` on the
  absorbed row; `split_entity` clears it. Flat by construction (one hop): merging INTO an
  absorbed entity refuses, as does absorbing a row that others already point at — chains are a
  reversibility hazard, refused rather than flattened silently (§8.3, disclosed).
- **Protected identities cannot be merged by machinery.** `merge_entity`/`split_entity` refuse
  any protected participant ON EITHER SIDE of the pointer unless the caller passes the owner-act
  acknowledgment (`protected_override=True`, surfaced in the CLI as `--protected-override`) —
  and proposal NEVER creates protected rows or merges anything at all. "Zero protected-identity
  false merges" is structural twice over: the mutations' write is a conditional UPDATE that
  re-asserts every invariant atomically (rowcount-checked — the C2 pattern), and
  `mutation_caller_inventory` scans src/ledger so the only sanctioned caller of the mutation
  functions is the owner-act CLI — a property a test enforces, not an absence of callers.
- Confirmation (`proposed → confirmed`) is an owner act; scale confirmation waits on the gate-1
  instrument (spec §4.1, staged).
"""

import json
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from ledger_memory.db import connect, connect_readonly
from ledger_memory.emission import emit_audit_and_event

RESOLVER_VERSION = "entity-resolution/1"
RESOLVER_VERSION_V2 = "entity-resolution/2"

_FOLD_CONFIDENCE = 0.8
_NAME_LINK_CONFIDENCE = 0.6


class EntityResolutionError(ValueError):
    """A refused entity operation — the message names the exact reason."""


def normalize_alias(alias: str) -> str:
    """Casefold + whitespace collapse, nothing else (v1; §8.4 — diacritic folding is a measured
    recall lever for the gate-1 instrument, not a default)."""
    return " ".join(alias.split()).casefold()


def fold_alias(alias: str) -> str:
    """v2 recall lever, layered ON TOP of normalize_alias (unchanged; §8.4,
    tests/test_entity_resolution_workflow.py:40 pins normalize_alias's own no-fold behavior).
    Diacritic-strips (Unicode NFD, drop combining marks — 'Ștefania' -> 'stefania') AND sorts
    the whitespace-collapsed tokens, so a name spelled 'Last First' folds identically to 'First
    Last' ('Bărbulescu Ștefania' == 'Ștefania Bărbulescu' post-fold): both are the SAME textual
    identity expressed differently, not a distinguishing signal, and Romanian sources mix both
    conventions. A WEAKER signal than v1's exact match — folding away a diacritic can
    occasionally erase the one thing that told two different people apart (the spec's culled
    Cârstea/Carstea case) — hence link_entity_aliases_v2 receipts it below 1.0, never at v1's
    certainty."""
    base = normalize_alias(alias)
    stripped = "".join(
        ch for ch in unicodedata.normalize("NFD", base) if not unicodedata.combining(ch)
    )
    return " ".join(sorted(stripped.split()))


@dataclass(frozen=True, kw_only=True)
class ProposalResult:
    entities_created: int
    links_created: int
    aliases_created: int
    candidates_seen: int


def propose_entities(database_path: Path, *, actor_id: int | None = None) -> ProposalResult:
    """Deterministic pass over LIVE speaker/entity extraction candidates: group by normalized
    alias; link each candidate's RUN SOURCE to the alias's entity (creating a `proposed` entity
    for a never-seen alias). Idempotent: the UNIQUE keys make a re-run create nothing new.

    Tier: a new entity snapshots the MOST RESTRICTIVE tier among its evidence (min — the
    an earlier fixc lattice: lower = more sensitive); an existing entity's tier is never widened
    here (tightening on new low-tier evidence IS applied — fail-safe direction only)."""
    with connect_readonly(database_path) as connection:
        candidates = connection.execute(
            "SELECT c.id, c.kind, c.value, c.sensitivity_tier, r.source_id "
            "FROM extraction_candidates c JOIN extraction_runs r ON r.id = c.run_id "
            "WHERE c.kind IN ('speaker', 'entity') AND c.status IN ('proposed', 'promoted') "
            "ORDER BY c.id",
        ).fetchall()

    groups: dict[str, dict] = {}
    for row in candidates:
        key = normalize_alias(str(row["value"]))
        if not key:
            continue
        group = groups.setdefault(key, {"display": str(row["value"]), "tier": 3,
                                        "candidate_ids": [], "kinds": set()})
        group["tier"] = min(group["tier"], int(row["sensitivity_tier"]))
        group["candidate_ids"].append(int(row["id"]))
        group["kinds"].add(str(row["kind"]))

    entities_created = links_created = aliases_created = 0
    with connect(database_path) as connection:
        for key, group in sorted(groups.items()):
            existing = connection.execute(
                "SELECT entity_id FROM entity_aliases WHERE normalized_alias = ? LIMIT 1", (key,)
            ).fetchone()
            if existing is not None:
                entity_id = int(existing["entity_id"])
                connection.execute(
                    "UPDATE entities SET sensitivity_tier = MIN(sensitivity_tier, ?) WHERE id = ?",
                    (group["tier"], entity_id))
            else:
                # A speaker-evidenced alias reads as a person; proposal NEVER sets protected.
                kind = "person" if "speaker" in group["kinds"] else "thing"
                cursor = connection.execute(
                    "INSERT INTO entities (display_name, entity_kind, status, protected, "
                    "sensitivity_tier, created_by_actor_id) VALUES (?, ?, 'proposed', 0, ?, ?)",
                    (group["display"], kind, group["tier"], actor_id))
                entity_id = int(cursor.lastrowid)
                entities_created += 1
                connection.execute(
                    "INSERT INTO entity_aliases (entity_id, alias, normalized_alias, "
                    "sensitivity_tier) VALUES (?, ?, ?, ?)",
                    (entity_id, group["display"], key, group["tier"]))
                aliases_created += 1
            for candidate_id in group["candidate_ids"]:
                inserted = connection.execute(
                    "INSERT OR IGNORE INTO entity_record_links (entity_id, record_type, "
                    "record_id, confidence, method, method_version, sensitivity_tier) "
                    "VALUES (?, 'extraction_candidate', ?, 1.0, 'alias_exact', ?, ?)",
                    (entity_id, candidate_id, RESOLVER_VERSION, group["tier"])).rowcount
                links_created += int(inserted)
        if entities_created or links_created:
            emit_audit_and_event(
                connection,
                action_type="entity.resolution_proposed",
                target_type="entity", target_id=None, actor_id=actor_id,
                reason="deterministic alias-equality proposal pass",
                event_type="entity.resolution_proposed",
                summary=(f"Entity proposal pass: {entities_created} entities, "
                         f"{aliases_created} aliases, {links_created} links "
                         f"from {len(candidates)} candidates"),
                metadata=json.dumps({
                    "entities_created": entities_created, "links_created": links_created,
                    "aliases_created": aliases_created, "method": "alias_exact",
                    "method_version": RESOLVER_VERSION}, sort_keys=True),
            )
    return ProposalResult(entities_created=entities_created, links_created=links_created,
                          aliases_created=aliases_created, candidates_seen=len(candidates))


@dataclass(frozen=True, kw_only=True)
class AliasLinkV2Result:
    fold_links_created: int
    name_links_created: int
    candidates_seen: int
    links_retracted: int = 0


def link_entity_aliases_v2(database_path: Path, *,
                           actor_id: int | None = None) -> AliasLinkV2Result:
    """a SECOND, receipted linking pass over entities `propose_entities` (v1)
    already created. Additive only: every new row is an ADDITIONAL entity_record_links receipt
    (method='alias_folded' or 'alias_name_link' — UNIQUE(entity_id, record_type, record_id,
    method) means it can never collide with v1's own 'alias_exact' row for the same candidate);
    v1's own rows are never touched, entity_aliases is never written (a folded/partial key is
    NOT a safe GLOBAL alias — the unscoped lookup a few lines up would let it silently claim an
    unrelated future candidate), and no entity/merge/protection state changes — this module is
    its own MUTATION_ALLOWLIST entry, and this function calls none of the three anyway.

    Two methods, run in order:

    1. `alias_folded` (confidence 0.8): entities whose alias (a v1 group is always exactly one
       alias) folds to the SAME `fold_alias` key are spelling/order variants of one identity —
       fold groups are FLAT (fold_alias is a proper equivalence key: identical strings, no
       threshold, no chaining needed) — link every non-winner's candidates to the winner (lowest
       entity id, deterministic). Catches diacritic and Last/First-order variance.

    2. `alias_name_link` (confidence 0.6): a BARE single-token alias (a lone first name or lone
       surname) is linked to the ONE fold-consolidated multi-token alias that carries that exact
       token — ONLY when the token belongs to EXACTLY ONE such full name across the WHOLE
       current candidate population. Two or more full names sharing the token means the bare
       mention could belong to either — the precision invariant this slice is bound to (gold
       must_never_merge: shared-first-name / shared-surname pairs) — so it is left a singleton,
       never a guess. The rule reads token identity only, never position: the same check applies
       whether the shared token conventionally reads as a first name or a surname.

    Deliberately NOT internally chained: a method-2 link always targets the fold-winner the
    token index names, never a value re-resolved through a running union-find. Two bare tokens
    that both point at the same full name (e.g. 'Zamfira' and 'Vasilescu' both -> 'Zamfira
    Vasilescu') therefore both carry an EXPLICIT receipt to that SAME entity id — which is what
    lets an independent reader of entity_record_links (the benchmark drive fix, same slice)
    reconstruct the whole cluster by unioning entities that co-occur on a shared record;
    chaining here would leave the full name's OWN candidates with no receipt in common with the
    bare ones at all — an island no downstream union-find could recover.

    Groups at PROPOSAL time only; never creates a protected row, never merges (carried from v1).
    """
    with connect_readonly(database_path) as connection:
        alias_rows = connection.execute(
            "SELECT entity_id, normalized_alias FROM entity_aliases ORDER BY entity_id"
        ).fetchall()
        receipt_rows = connection.execute(
            "SELECT entity_id, record_id, sensitivity_tier FROM entity_record_links "
            "WHERE method = 'alias_exact' AND record_type = 'extraction_candidate' "
            "ORDER BY entity_id, record_id"
        ).fetchall()

    alias_of: dict[int, str] = {int(r["entity_id"]): str(r["normalized_alias"])
                                for r in alias_rows}
    candidates_of: dict[int, list[tuple[int, int]]] = {}
    for r in receipt_rows:
        candidates_of.setdefault(int(r["entity_id"]), []).append(
            (int(r["record_id"]), int(r["sensitivity_tier"])))

    # Method 1 groundwork: fold_key equality is a flat equivalence — direct dict grouping,
    # winner = lowest entity id (deterministic; no dependency on dict/set iteration order).
    fold_groups: dict[str, list[int]] = {}
    for entity_id, alias in alias_of.items():
        fold_groups.setdefault(fold_alias(alias), []).append(entity_id)
    fold_winner: dict[int, int] = {}
    for members in fold_groups.values():
        winner = min(members)
        for member in members:
            fold_winner[member] = winner

    to_insert: list[tuple[int, int, float, str, int]] = []  # entity_id, record_id, conf, method, tier

    for members in fold_groups.values():
        if len(members) < 2:
            continue
        winner = min(members)
        for loser in sorted(members):
            if loser == winner:
                continue
            for record_id, tier in candidates_of.get(loser, []):
                to_insert.append((winner, record_id, _FOLD_CONFIDENCE, "alias_folded", tier))

    # Method 2 groundwork: token -> the set of fold-winners whose (folded) alias is a full name
    # (>=2 tokens) carrying that token. Built from WINNERS only, so a diacritic/order variant of
    # a full name counts once, at its consolidated identity.
    token_owner: dict[str, set[int]] = {}
    for winner in sorted(set(fold_winner.values())):
        tokens = fold_alias(alias_of[winner]).split()
        if len(tokens) >= 2:
            for token in tokens:
                token_owner.setdefault(token, set()).add(winner)

    for entity_id in sorted(alias_of):
        if fold_winner[entity_id] != entity_id:
            continue  # a fold LOSER already has its method-1 receipt; nothing further to add
        tokens = fold_alias(alias_of[entity_id]).split()
        if len(tokens) != 1:
            continue  # method 2 only links BARE (single-token) aliases
        owners = {w for w in token_owner.get(tokens[0], set()) if w != entity_id}
        if len(owners) != 1:
            continue  # zero or ambiguous carriers — the precision-safe refusal; stays a singleton
        target = next(iter(owners))
        for record_id, tier in candidates_of.get(entity_id, []):
            to_insert.append((target, record_id, _NAME_LINK_CONFIDENCE, "alias_name_link", tier))

    # review finding 1: the unambiguity invariant is a statement about the CURRENT
    # population, so every v2 link is derived state that must be RE-DERIVED, never trusted from
    # a prior run — a surname unique last month gains a second bearer and the old bare-name link
    # becomes a coin flip with a clean receipt. Reconcile: any existing v2-method row the
    # current population no longer justifies is retracted (deleted, counted, and announced in
    # the same event) — covering both the newly-ambiguous name-link and the symmetric case of a
    # fold group changing shape. v1's alias_exact rows are never touched.
    justified = {(e, r, m) for e, r, _c, m, _t in to_insert}
    fold_links = name_links = retracted = 0
    with connect(database_path) as connection:
        existing_v2 = connection.execute(
            "SELECT id, entity_id, record_id, method FROM entity_record_links "
            "WHERE method IN ('alias_folded', 'alias_name_link') "
            "AND record_type = 'extraction_candidate' ORDER BY id"
        ).fetchall()
        for row in existing_v2:
            if (int(row["entity_id"]), int(row["record_id"]), str(row["method"])) not in justified:
                connection.execute("DELETE FROM entity_record_links WHERE id = ?", (int(row["id"]),))
                retracted += 1
        for entity_id, record_id, confidence, method, tier in to_insert:
            inserted = connection.execute(
                "INSERT OR IGNORE INTO entity_record_links (entity_id, record_type, record_id, "
                "confidence, method, method_version, sensitivity_tier) "
                "VALUES (?, 'extraction_candidate', ?, ?, ?, ?, ?)",
                (entity_id, record_id, confidence, method, RESOLVER_VERSION_V2, tier),
            ).rowcount
            if method == "alias_folded":
                fold_links += int(inserted)
            else:
                name_links += int(inserted)
        if fold_links or name_links or retracted:
            emit_audit_and_event(
                connection,
                action_type="entity.alias_link_v2",
                target_type="entity", target_id=None, actor_id=actor_id,
                reason="deterministic fold/name-link second-method pass (re-derived)",
                event_type="entity.alias_link_v2",
                summary=(f"Entity alias-link v2 pass: {fold_links} fold-links, "
                         f"{name_links} name-links, {retracted} retracted from "
                         f"{len(receipt_rows)} v1 receipts"),
                metadata=json.dumps({
                    "fold_links_created": fold_links, "name_links_created": name_links,
                    "links_retracted": retracted,
                    "methods": ["alias_folded", "alias_name_link"],
                    "method_version": RESOLVER_VERSION_V2}, sort_keys=True),
            )
    return AliasLinkV2Result(fold_links_created=fold_links, name_links_created=name_links,
                             candidates_seen=len(receipt_rows), links_retracted=retracted)


def _entity(connection, entity_id: int):
    row = connection.execute("SELECT * FROM entities WHERE id = ?", (entity_id,)).fetchone()
    if row is None:
        raise EntityResolutionError(f"entity not found: {entity_id}")
    return row


def merge_entity(database_path: Path, *, absorbed_id: int, canonical_id: int, actor_id: int,
                 reason: str, protected_override: bool = False) -> None:
    """Set the reversible pointer. Owner-act only (v1, §8.2 — proposal machinery never calls
    this); a protected participant additionally requires the explicit acknowledgment flag."""
    if absorbed_id == canonical_id:
        raise EntityResolutionError("an entity cannot absorb itself")
    if not reason or not reason.strip():
        raise EntityResolutionError("a non-empty reason is required for a merge")
    if actor_id is None:
        raise EntityResolutionError("merge requires a named owner actor — never anonymous")
    with connect(database_path) as connection:
        absorbed = _entity(connection, absorbed_id)
        canonical = _entity(connection, canonical_id)
        if absorbed["canonical_entity_id"] is not None:
            raise EntityResolutionError(
                f"entity {absorbed_id} is already merged (into "
                f"{absorbed['canonical_entity_id']}) — split it first")
        if canonical["canonical_entity_id"] is not None:
            raise EntityResolutionError(
                f"entity {canonical_id} is itself merged — pointers stay flat (one hop); "
                "merge into its canonical target instead")
        pointing = connection.execute(
            "SELECT COUNT(*) FROM entities WHERE canonical_entity_id = ?", (absorbed_id,)
        ).fetchone()[0]
        if pointing:
            raise EntityResolutionError(
                f"entity {absorbed_id} is the canonical target of {pointing} other(s) — "
                "absorbing it would chain pointers; split those first")
        if (absorbed["protected"] or canonical["protected"]) and not protected_override:
            raise EntityResolutionError(
                "a PROTECTED identity is involved — machinery never merges these; the owner-act "
                "path requires the explicit --protected-override acknowledgment")
        # The C2 conditional-write pattern (review blocker 1: the reads above are courtesy
        # diagnostics; a plain UPDATE after them is a TOCTOU — the reviewer raced two merges into
        # a live 3→2→1 chain past every guard. The WHERE re-asserts EVERY flatness invariant
        # atomically; rowcount != 1 means the world moved and the merge refuses).
        updated = connection.execute(
            "UPDATE entities SET canonical_entity_id = ? WHERE id = ? "
            "AND canonical_entity_id IS NULL "
            "AND (SELECT canonical_entity_id FROM entities WHERE id = ?) IS NULL "
            "AND NOT EXISTS (SELECT 1 FROM entities WHERE canonical_entity_id = ?)",
            (canonical_id, absorbed_id, canonical_id, absorbed_id)).rowcount
        if updated != 1:
            raise EntityResolutionError(
                f"merge lost the race: the flatness invariants no longer hold for "
                f"{absorbed_id} → {canonical_id} — re-inspect and retry")
        # Summary and metadata are deliberately REASON-FREE (review blocker 2: the owner's merge
        # reason narrates an identity linkage — the paradigm place a real name appears — and
        # events ship in the scrubbed dogfood export. The reason lives in audit_entries.reason,
        # which the export scrub placeholders for entity.* actions).
        emit_audit_and_event(
            connection,
            action_type="entity.merged",
            target_type="entity", target_id=absorbed_id, actor_id=actor_id, reason=reason,
            event_type="entity.merged",
            summary=f"Entity {absorbed_id} merged into {canonical_id} (reason in audit)",
            metadata=json.dumps({"absorbed_id": absorbed_id, "canonical_id": canonical_id,
                                 "protected_override": bool(protected_override)},
                                sort_keys=True),
        )


def split_entity(database_path: Path, *, entity_id: int, actor_id: int, reason: str,
                 protected_override: bool = False) -> None:
    """Clear the pointer — the un-merge. The protection guard covers BOTH sides (review major 4:
    the first build checked only the row being split, so an unprotected node could silently
    detach from a PROTECTED canonical's lineage without the acknowledgment the merge required
    to attach it — the lineage is as protected leaving as it was arriving)."""
    if not reason or not reason.strip():
        raise EntityResolutionError("a non-empty reason is required for a split")
    if actor_id is None:
        raise EntityResolutionError("split requires a named owner actor — never anonymous")
    with connect(database_path) as connection:
        row = _entity(connection, entity_id)
        if row["canonical_entity_id"] is None:
            raise EntityResolutionError(f"entity {entity_id} is not merged — nothing to split")
        previous = int(row["canonical_entity_id"])
        canonical = _entity(connection, previous)
        if (row["protected"] or canonical["protected"]) and not protected_override:
            raise EntityResolutionError(
                "a PROTECTED identity is involved (on either side of the pointer) — the "
                "owner-act path requires the explicit --protected-override acknowledgment")
        # Conditional write + rowcount, the same C2 shape as the merge (the pointer must still
        # be the one the guards inspected).
        updated = connection.execute(
            "UPDATE entities SET canonical_entity_id = NULL "
            "WHERE id = ? AND canonical_entity_id = ?",
            (entity_id, previous)).rowcount
        if updated != 1:
            raise EntityResolutionError(
                f"split lost the race: entity {entity_id}'s pointer moved — re-inspect and retry")
        emit_audit_and_event(
            connection,
            action_type="entity.split",
            target_type="entity", target_id=entity_id, actor_id=actor_id, reason=reason,
            event_type="entity.split",
            summary=f"Entity {entity_id} split from {previous} (reason in audit)",
            metadata=json.dumps({"entity_id": entity_id, "was_canonical_id": previous},
                                sort_keys=True),
        )


def set_entity_protected(database_path: Path, *, entity_id: int, protected: bool,
                         actor_id: int, reason: str) -> None:
    """Owner act: flip protection. Proposal machinery never touches this column."""
    if not reason or not reason.strip():
        raise EntityResolutionError("a non-empty reason is required")
    if actor_id is None:
        raise EntityResolutionError("protection changes require a named owner actor")
    with connect(database_path) as connection:
        _entity(connection, entity_id)
        connection.execute("UPDATE entities SET protected = ? WHERE id = ?",
                           (1 if protected else 0, entity_id))
        emit_audit_and_event(
            connection,
            action_type="entity.protection_changed",
            target_type="entity", target_id=entity_id, actor_id=actor_id, reason=reason,
            event_type="entity.protection_changed",
            summary=f"Entity {entity_id} protection → {'ON' if protected else 'off'} "
                    "(reason in audit)",
            metadata=json.dumps({"entity_id": entity_id, "protected": bool(protected)},
                                sort_keys=True),
        )


def resolve_canonical(database_path: Path, entity_id: int) -> int:
    """One hop, flat by construction."""
    with connect_readonly(database_path) as connection:
        row = _entity(connection, entity_id)
    return int(row["canonical_entity_id"]) if row["canonical_entity_id"] is not None else entity_id


def list_entities(database_path: Path, *, status: str | None = None) -> list[dict]:
    with connect_readonly(database_path) as connection:
        rows = connection.execute(
            "SELECT e.*, "
            "(SELECT COUNT(*) FROM entity_aliases a WHERE a.entity_id = e.id) AS alias_count, "
            "(SELECT COUNT(*) FROM entity_record_links l WHERE l.entity_id = e.id) AS link_count "
            "FROM entities e" + (" WHERE e.status = ?" if status else "") + " ORDER BY e.id",
            (status,) if status else (),
        ).fetchall()
    return [dict(r) for r in rows]


def show_entity(database_path: Path, entity_id: int) -> dict:
    with connect_readonly(database_path) as connection:
        entity = dict(_entity(connection, entity_id))
        aliases = [dict(r) for r in connection.execute(
            "SELECT alias, normalized_alias, sensitivity_tier FROM entity_aliases "
            "WHERE entity_id = ? ORDER BY id", (entity_id,))]
        # Links are append-only receipts: when a 1077 re-extraction supersedes a cited
        # candidate, the old link REMAINS (history), and the inspect surface says so instead of
        # presenting a stale receipt as current (review NIT 5 — visible, never silent).
        links = [dict(r) for r in connection.execute(
            "SELECT l.record_type, l.record_id, l.confidence, l.method, l.method_version, "
            "l.sensitivity_tier, "
            "CASE WHEN l.record_type = 'extraction_candidate' THEN "
            "(SELECT c.status FROM extraction_candidates c WHERE c.id = l.record_id) END "
            "AS cited_status "
            "FROM entity_record_links l WHERE l.entity_id = ? ORDER BY l.id", (entity_id,))]
        absorbed = [int(r[0]) for r in connection.execute(
            "SELECT id FROM entities WHERE canonical_entity_id = ? ORDER BY id", (entity_id,))]
    return {"entity": entity, "aliases": aliases, "links": links, "absorbed_ids": absorbed}


# The functions that MUTATE identity state; "owner-act only" must be structure, not prose
# (review major 3 — 1076 set the standard with its call-tree scans).
MUTATION_ALLOWLIST = {
    "entity_resolution.py": "the mutations' own home",
    "cli.py": "the owner-act command surface — the ONLY sanctioned caller",
}


def mutation_caller_inventory(src_dir: Path | None = None) -> list[tuple[str, int, str]]:
    """Modules outside the allowlist that import or reference the mutation functions by name
    (`merge_entity` / `split_entity` / `set_entity_protected`) — fail-closed on the bare import
    (an unused import IS the seam). This is what makes "proposal machinery never merges, ticks
    never merge" a scanned property rather than an absence-of-callers observation."""
    import ast

    watched = {"merge_entity", "split_entity", "set_entity_protected"}
    src_dir = src_dir or Path(__file__).resolve().parent
    violations: list[tuple[str, int, str]] = []
    for path in sorted(src_dir.glob("*.py")):
        if path.name in MUTATION_ALLOWLIST:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "ledger_memory.entity_resolution":
                for alias in node.names:
                    if alias.name in watched:
                        violations.append((path.name, node.lineno, f"import {alias.name}"))
            elif isinstance(node, ast.Attribute) and node.attr in watched:
                violations.append((path.name, node.lineno, f"attribute .{node.attr}"))
    return violations


__all__ = [
    "EntityResolutionError",
    "ProposalResult",
    "AliasLinkV2Result",
    "RESOLVER_VERSION",
    "RESOLVER_VERSION_V2",
    "MUTATION_ALLOWLIST",
    "fold_alias",
    "link_entity_aliases_v2",
    "list_entities",
    "merge_entity",
    "mutation_caller_inventory",
    "normalize_alias",
    "propose_entities",
    "resolve_canonical",
    "set_entity_protected",
    "show_entity",
    "split_entity",
]
