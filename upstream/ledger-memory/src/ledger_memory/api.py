"""The single front door to ledger-memory. Both the MCP server and the CLI call this.

Design rules, enforced here rather than hoped for:

* **Nothing is ever deleted or edited in place.** A belief is *revised* (superseded, with a reason) or
  *disputed*. The old version stays queryable forever.
* **Every write is attributed** to an actor (the agent, or the human owner via the CLI) and lands in
  the audit trail.
* **Authority is explicit.** Every remembered fact states whether the user said it, the agent inferred
  it, the agent observed it, or a document attests it. A revision inherits its parent's authority, so
  an inference can never be re-labelled as something the user said.
* **Search never lies about its own health.** Results report which search lanes contributed, and any
  degradation is returned to the caller instead of being printed somewhere the agent cannot see.
* **No file is ever placed relative to the current directory.** Everything lives under LEDGER_HOME.
"""
from __future__ import annotations

import contextlib
import datetime as _dt
import io
import lzma
import os
import sqlite3
from dataclasses import asdict
from pathlib import Path
from typing import Any, Callable, Iterator

from ledger_memory import (
    actors,
    audit,
    chunk_embeddings,
    chunkers,
    chunks,
    config,
    context_packages,
    db,
    db_packaging,
    decisions,
    documents,
    facts,
    integrity,
    rag_search,
    relationships,
    search_index,
    semantic_rebuild,
    temporal_view,
    unified_search,
)
from ledger_memory import brain_identity, locking, reader
from ledger_memory.context_packages import ContextPackageItem
from ledger_memory.about_me_recall import _fused_fact_candidates, tool_read_authorizer, tool_read_settings
from ledger_memory.candidates import Candidate, decision_candidates_for_query
from ledger_memory.context_tool import (
    HARD_MAX_READS, LIMIT_REACHED, REFUSED_MALFORMED, ReadBudget, ToolReadRequest, _clean_arguments, render_result,
)
from ledger_memory.embeddings import resolve_embedding_backend_name
from ledger_memory.paths import default_vectors_path
from ledger_memory.tiered_read import MAX_CANDIDATES, TieredReadRefused, record_read_audit, resolve_read_authority, tiered_read
from ledger_memory.tiers import may_see

# The audit label every read through the memory tools carries (one read-log row per read).
READ_SURFACE = "mcp"

# What recall searches. Events and audit entries are history, reachable through `history`; indexing them
# would let bookkeeping crowd out knowledge, and a rebuild's own bookkeeping would make the index stale.
KNOWLEDGE_TYPES = "fact,decision,context_package,relationship"
# The LIKE floor reads tables directly, so the index setting alone does not keep events out of recall.
_KNOWLEDGE = frozenset(KNOWLEDGE_TYPES.split(","))

SOURCES = {
    "user_said": "actor_asserted",   # the human stated it; the agent is only the recorder
    "inferred": "model_inferred",    # the agent concluded it
    "observed": "system_observed",   # the agent measured or saw it happen (ran a command, read a value)
    "document": "evidence_attested", # a stored document backs it (document_id required)
}
AUTHORITY_LABELS = {v: k for k, v in SOURCES.items()} | {"legacy_unclassified": "unclassified"}
LINKABLE = {"fact": "facts", "decision": "decisions", "context_package": "context_packages", "document": "documents"}
MAX_FACT_CHARS = 4000   # a fact is one self-contained statement; long text belongs in a document


class LedgerError(ValueError):
    """A request the memory refuses, with a message written for the agent to act on."""


def _now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def _required(value: str | None, what: str) -> str:
    value = (value or "").strip()
    if not value:
        raise LedgerError(f"{what} is required")
    return value


def _clamp(n: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, int(n)))


def _fact_text(statement: str | None, what: str = "statement") -> str:
    s = _required(statement, what)
    if len(s) > MAX_FACT_CHARS:
        raise LedgerError(f"{what} is too long for one fact ({len(s)} characters, max {MAX_FACT_CHARS}); "
                          "keep facts short and store long text with ingest_document")
    return s


def _utc_moment(when: str) -> str:
    """ISO-8601 date or date-time -> 'YYYY-MM-DDTHH:MM:SS' in UTC. Naive times are taken as UTC."""
    raw = _required(when, "a moment (ISO-8601, e.g. 2026-09-30T14:00:00)")
    try:
        moment = _dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        raise LedgerError(f"{raw!r} is not an ISO-8601 date or time (e.g. 2026-09-30 or 2026-09-30T14:00:00Z)") from None
    if moment.tzinfo is not None:
        moment = moment.astimezone(_dt.timezone.utc).replace(tzinfo=None)
    return moment.strftime("%Y-%m-%dT%H:%M:%S")


def default_home() -> Path:
    return Path(os.environ.get("LEDGER_HOME", "~/.ledger-memory")).expanduser()


class Ledger:
    """One memory store, rooted at a home directory (default ``~/.ledger-memory``)."""

    def __init__(self, home: Path | str | None = None, *, agent_name: str | None = None) -> None:
        self.home = Path(home).expanduser() if home else default_home()
        self.db_path = self.home / "memory.db"
        self.vectors_path = default_vectors_path(self.db_path)
        self.backups_dir = self.home / "backups"
        self.agent_name = agent_name or os.environ.get("LEDGER_AGENT_NAME", "agent")
        self._agent_id: int | None = None
        self._owner_id: int | None = None
        self.last_upgrade: dict[str, Any] | None = None
        self._window = ReadBudget(None)   # the recall_more reply window; reset by `brief` and at start
        self._window_entries = 0

    # ------------------------------------------------------------------ setup

    def ensure(self) -> "Ledger":
        """Create the store on first use; idempotent. Sets the knowledge-only index and the agent actor."""
        self.recovery_note = None
        self.home.mkdir(parents=True, exist_ok=True, mode=0o700)          # an existing directory is never loosened
        for sub in (self.backups_dir, self.home / "inbox"):
            sub.mkdir(exist_ok=True, mode=0o700)
        with locking.exclusive(reader.lock_path(self.db_path)):             # first start / upgrade: one process at a time
            self.recovery_note = self._recover_interrupted_restore()           # finish or report a crashed restore first
            self._upgrade_if_behind()
            if not db.database_is_initialized(self.db_path):
                db.initialize_database(self.db_path)
            if not (config.get_config(self.db_path, "search.index_types") or "").strip():
                config.set_config(self.db_path, key="search.index_types", value=KNOWLEDGE_TYPES, actor_id=self.owner_id)
            if config.get_config(self.db_path, "context.tool_reads") is None:
                config.set_config(self.db_path, key="context.tool_reads", value="bounded", actor_id=self.owner_id)
            brain_identity.ensure_brain_lineage_id(self.db_path, actor_id=self.owner_id)   # the issuer proofs bind to
            reader.remove_stale_temp_files(self.db_path)
            reader.ensure_reader(self.db_path, owner_actor_id=self.owner_id)
        self.reset_window()
        backend = os.environ.get("LEDGER_EMBEDDING_BACKEND")
        if backend and config.get_config(self.db_path, "embedding.backend") != backend:
            config.set_config(self.db_path, key="embedding.backend", value=backend, actor_id=self.owner_id)
        return self

    def _upgrade_if_behind(self) -> None:
        """A memory made by an older version is upgraded in place, and ONLY after a verified backup of it exists.
        The migrations are additive (new tables only); the backup is the rollback path."""
        if not self.db_path.exists():
            return
        state, pending = db.migration_status(self.db_path)
        if state != "behind":
            return
        made = self.backup(label="pre-upgrade")
        self._verify_backup(made)                       # a backup that does not verify is no rollback path: abort
        try:
            db.initialize_database(self.db_path)
        except Exception as exc:
            raise LedgerError(f"upgrade failed ({type(exc).__name__}); the memory was left as it was and the backup is "
                              f"at {made.get('archive_path')}") from None
        self.last_upgrade = {"applied": pending, "backup": made.get("archive_path")}

    def _verify_backup(self, made: dict[str, Any]) -> None:
        """Re-read the archive, check its SHA-256 against the sidecar, then open the restored copy read-only and compare
        the row count of one table with the live memory. Any failure aborts the upgrade before anything is migrated."""
        import hashlib
        import tempfile
        try:
            archive = Path(made["archive_path"])
            expected = Path(made["sha256_path"]).read_text().split()[0].strip()
            actual = hashlib.sha256(archive.read_bytes()).hexdigest()
            if actual != expected:
                raise ValueError("the archive does not match its recorded SHA-256")
            with tempfile.TemporaryDirectory(prefix="ledger-verify-") as tmp:
                copy = Path(tmp) / "check.db"
                copy.write_bytes(lzma.decompress(archive.read_bytes()))
                src = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
                dst = sqlite3.connect(f"file:{copy}?mode=ro", uri=True)
                try:
                    live = src.execute("select count(*) from facts").fetchone()[0]
                    saved = dst.execute("select count(*) from facts").fetchone()[0]
                finally:
                    src.close(); dst.close()
                if live != saved:
                    raise ValueError(f"the backup holds {saved} facts but the memory holds {live}")
        except LedgerError:
            raise
        except Exception as exc:
            raise LedgerError(f"upgrade aborted, nothing was changed: the pre-upgrade backup did not verify ({exc})") from None

    def _actor(self, name: str) -> int | None:
        for a in actors.list_actors(self.db_path):
            if a.name == name:
                return a.id
        return None

    @property
    def owner_id(self) -> int:
        if self._owner_id is None:
            found = self._actor("owner")
            if found is None:
                raise LedgerError("store is not initialized; run `ledger init`")
            self._owner_id = found
        return self._owner_id

    @property
    def agent_id(self) -> int:
        if self._agent_id is None:
            found = self._actor(self.agent_name)
            if found is None:
                found = actors.add_actor(self.db_path, name=self.agent_name, actor_type="agent",
                                         notes="AI agent writing through the memory tools").id
            self._agent_id = found
        return self._agent_id

    def _writer(self, as_owner: bool) -> int:
        return self.owner_id if as_owner else self.agent_id

    @property
    def semantic_enabled(self) -> bool:
        from ledger_memory.embeddings import is_legacy_embedding_backend
        name = resolve_embedding_backend_name(self.db_path)
        return name in ("fake", "bge-m3") or is_legacy_embedding_backend(name)

    @property
    def _semantic_deferred(self) -> bool:
        return resolve_embedding_backend_name(self.db_path) == "bge-m3"

    def _request_semantic_maintenance(self) -> list[str]:
        from ledger_memory.semantic_pending import request_drain
        try:
            request_drain(self.db_path, self.vectors_path)
        except Exception:
            return ["semantic maintenance unavailable; saved content remains available for keyword retrieval"]
        return []

    # ------------------------------------------------------------------ index upkeep

    def _index_status(self) -> dict[str, Any]:
        from ledger_memory.search_index import get_search_index_status
        from ledger_memory.semantic_index import get_semantic_index_status
        st = {"fulltext_fresh": get_search_index_status(self.db_path).fresh,
              "semantic": "on" if self.semantic_enabled else "off (not configured)"}
        if self.semantic_enabled:
            if self._semantic_deferred:
                from ledger_memory.semantic_readiness import readiness
                report = readiness(self.db_path, self.vectors_path)
                st.update(semantic_fresh=report.lane_open, semantic_state=report.state,
                          pending_rows=report.pending_rows, failed_rows=report.failed_rows,
                          semantic_reason=report.reason)
            else:
                st["semantic_fresh"] = get_semantic_index_status(self.db_path).fresh
        return st

    def _after_commit(self, label: str, fn: Callable[..., Any], *args: Any) -> list[str]:
        """Review fix (0.3.0-rc2): a hook that runs AFTER the record committed must never turn a saved write
        into an apparent refusal; a failure becomes a warning."""
        warnings: list[str] = []
        try:
            fn(*args)
        except Exception as exc:
            warnings.append(f"{label} index not updated ({type(exc).__name__}); the record is saved; run `ledger reindex`")
        return warnings

    def _refresh(self, record_type: str, record_id: int) -> list[str]:
        """Keep both indexes fresh after a write: a scoped rebuild of just the touched record."""
        warnings: list[str] = []
        warnings.extend(self._after_commit(
            "search",
            lambda: search_index.rebuild_search_index(self.db_path, record_type=record_type, record_id=record_id)
        ))
        if self._semantic_deferred:
            warnings.extend(self._request_semantic_maintenance())
        elif self.semantic_enabled:
            try:
                semantic_rebuild.rebuild_semantic_index(
                    self.db_path, record_type=record_type, record_id=record_id, sidecar_path=self.vectors_path)
            except Exception as exc:
                warnings.append(f"semantic index not updated ({type(exc).__name__}: {exc}); "
                                "the fact is saved and full-text searchable")
        return warnings

    def refresh(self, *, repair: bool = False) -> dict[str, Any]:
        """Bring any stale index up to date (e.g. after writes made from another process).

        The synchronous test backend can explicitly replace a blocked generation,
        retaining its rollback artifact. BGE queues revision maintenance only;
        repair=True reports that generation repair was not performed. Ordinary
        recall never certifies or takes over a foreign generation.
        """
        warnings: list[str] = []
        repaired: dict[str, Any] | None = None
        deferred_repair: dict[str, Any] | None = None
        st = self._index_status()
        if not st["fulltext_fresh"]:
            search_index.rebuild_search_index(self.db_path)
        if self._semantic_deferred:
            warnings.extend(self._request_semantic_maintenance())
            if repair:
                reason = ("deferred maintenance does not perform generation repair; "
                          "an explicit generation rebuild is required for a blocked binding")
                deferred_repair = {"requested": True, "performed": False, "reason": reason}
                warnings.append(reason)
        elif self.semantic_enabled:
            try:
                if not st.get("semantic_fresh", True):
                    semantic_rebuild.rebuild_semantic_index(self.db_path, sidecar_path=self.vectors_path)
                if repair:
                    from ledger_memory.semantic_search import semantic_lane_blocked
                    blocked = semantic_lane_blocked(self.db_path, self.vectors_path)
                    if blocked:
                        r = semantic_rebuild.replace_active_model_rebuild(self.db_path, sidecar_path=self.vectors_path)
                        repaired = {"was": blocked, "rebuilt_rows": r.embedded_rows, "search_ready": bool(r.gate_open),
                                    "previous_vectors_kept_at": r.rollback_artifact}
                        if not r.gate_open:
                            warnings.append(f"meaning-based search is still blocked after a full rebuild ({blocked})")
            except Exception as exc:
                warnings.append(f"semantic index could not be refreshed: {type(exc).__name__}: {exc}")
        out: dict[str, Any] = {"index": self._index_status(), "warnings": warnings}
        if repaired:
            out["repaired"] = repaired
        if deferred_repair is not None:
            out["repair"] = deferred_repair
        return out

    # ------------------------------------------------------------------ facts

    def _echo(self, f: facts.Fact) -> dict[str, Any]:
        """A fact echoed back after a write or by id: the record goes through the door like any other read."""
        try:
            got = {r.record_id: r.content for r in self._door(self._cands("fact", [f.id])) if r.record_type == "fact"}
            if f.id in got:
                return self._fact_dict(f, got[f.id])
            d = self._fact_dict(f, "")
            d["statement"], d["withheld"] = None, True
            return d
        except Exception:
            # The record is saved; only reading it back failed. Say so, distinct from a deliberate withhold.
            d = self._fact_dict(f, "")
            d["statement"], d["echo_unavailable"] = None, True
            return d

    def _fact_dict(self, f: facts.Fact, content: str | None = None) -> dict[str, Any]:
        d = {"id": f.id, "statement": f.statement if content is None else content, "kind": f.fact_type, "status": f.status,
             "confidence": f.confidence, "known_at": f.known_at,
             "source": AUTHORITY_LABELS.get(getattr(f, "authority_class", "") or "", "unclassified"),
             "current": f.superseded_by is None}
        if f.superseded_by is not None:
            d["superseded_by"] = f.superseded_by
        # A dispute is an append-only event; "disputed" is DERIVED from those events, never a stored status.
        disputes = facts.get_fact_dispute_events(self.db_path, f.id)
        if disputes:
            d["disputed"] = True
            d["disputes"] = [{"reason": (x.get("metadata") or {}).get("reason") if isinstance(x.get("metadata"), dict)
                              else x.get("reason"), "at": x.get("occurred_at") or x.get("created_at")} for x in disputes]
        return d

    def remember(self, statement: str, *, source: str, kind: str = "note", confidence: float | None = None,
                 document_id: int | None = None, as_owner: bool = False) -> dict[str, Any]:
        statement = _fact_text(statement)
        kind = (kind or "note").strip()[:40] or "note"
        if source not in SOURCES:
            raise LedgerError(f"source must be one of {sorted(SOURCES)}")
        if confidence is not None and not 0.0 <= confidence <= 1.0:
            raise LedgerError("confidence must be between 0 and 1")
        authority = SOURCES[source]
        kwargs: dict[str, Any] = {}
        if source == "user_said":
            kwargs["asserted_by_actor_id"] = self.owner_id
        if source == "document":
            if document_id is None or documents_exists(self.db_path, document_id) is False:
                raise LedgerError("source='document' needs the document_id of an ingested document")
            kwargs.update(evidence_type="document", evidence_id=document_id)
        f = facts.add_fact(self.db_path, statement=statement, fact_type=kind or "note", confidence=confidence,
                           actor_id=self._writer(as_owner), authority_class=authority, **kwargs)
        out = self._echo(f)
        out["warnings"] = self._refresh("fact", f.id)
        return out

    def revise(self, fact_id: int, statement: str, *, reason: str, confidence: float | None = None,
               as_owner: bool = False, source: str | None = None, document_id: int | None = None) -> dict[str, Any]:
        if not (reason or "").strip():
            raise LedgerError("a revision must say why (reason is required)")
        old = facts.get_fact(self.db_path, fact_id)
        if old is None:
            raise LedgerError(f"no fact {fact_id}")
        if old.superseded_by is not None:
            raise LedgerError(f"fact {fact_id} was already revised by fact {old.superseded_by}; revise that one")
        statement = _fact_text(statement, "the revised statement")
        if confidence is not None and not 0.0 <= confidence <= 1.0:
            raise LedgerError("confidence must be between 0 and 1")

        # Determine authority_class and related kwargs for the new fact
        authority_class = None
        kwargs: dict[str, Any] = {}

        if document_id is not None and source != "document":
            raise LedgerError("document_id is only used with source='document'")
        # Review fix (0.3.0-rc2): the corrected statement's provenance. An agent's correction of something the user
        # said or a document backs is the agent's inference unless it says the user stated it or cites a new document.
        if source is not None:
            if source not in SOURCES:
                raise LedgerError(f"source must be one of {sorted(SOURCES)}")
            authority_class = SOURCES[source]
            if source == "user_said":
                kwargs["asserted_by_actor_id"] = self.owner_id
            elif source == "document":
                if document_id is None or documents_exists(self.db_path, document_id) is False:
                    raise LedgerError("source='document' needs the document_id of an ingested document")
                kwargs.update(evidence_type="document", evidence_id=document_id)
            elif source in ("inferred", "observed"):
                # authority_class already set; no assertor, no evidence
                pass
        else:
            if as_owner:
                # The owner's own correction keeps its source, except that a document label cannot survive a change of
                # content without its document: the owner then stated the new text.
                if old.authority_class == "evidence_attested":
                    authority_class, kwargs["asserted_by_actor_id"] = "actor_asserted", self.owner_id
                else:
                    authority_class = None
            else:
                # The agent's unsourced correction is its inference: it may not keep "the user said", "a document
                # backs it" or "observed" on text it changed.
                if old.authority_class in ("actor_asserted", "evidence_attested", "system_observed"):
                    authority_class = "model_inferred"
                else:
                    authority_class = None

        try:
            new = facts.supersede_fact(
                self.db_path,
                fact_id=fact_id,
                statement=statement,
                confidence=confidence,
                actor_id=self._writer(as_owner),
                reason=reason.strip(),
                authority_class=authority_class,
                **kwargs,
            )
        except facts.FactAlreadySupersededError as e:
            raise LedgerError(f"fact {fact_id} was already revised by fact {e.superseded_by}; revise that one")
        except facts.AuthorityAssignmentError as e:
            raise LedgerError(str(e))

        warnings = self._refresh("fact", new.id) + self._refresh("fact", fact_id)
        return {"revised": fact_id, "now": self._echo(new), "warnings": warnings}

    def dispute(self, fact_id: int, *, reason: str, as_owner: bool = False,
                conflicts_with: int | None = None) -> dict[str, Any]:
        if not (reason or "").strip():
            raise LedgerError("a dispute must say why (reason is required)")
        if facts.get_fact(self.db_path, fact_id) is None:
            raise LedgerError(f"no fact {fact_id}")
        if conflicts_with is not None and (conflicts_with == fact_id or facts.get_fact(self.db_path, conflicts_with) is None):
            raise LedgerError(f"conflicts_with must be another existing fact, not {conflicts_with}")
        f = facts.dispute_fact(self.db_path, fact_id=fact_id, reason=reason.strip(), actor_id=self._writer(as_owner),
                               disputed_by_fact_id=conflicts_with)
        return {"fact": self._echo(f), "warnings": self._refresh("fact", fact_id)}

    def _exists(self, record_type: str, record_id: int) -> bool:
        con = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
        try:
            return con.execute(f"select 1 from {LINKABLE[record_type]} where id = ?", (record_id,)).fetchone() is not None
        finally:
            con.close()

    def get_fact(self, fact_id: int) -> dict[str, Any]:
        f = facts.get_fact(self.db_path, fact_id)
        if f is None:
            raise LedgerError(f"no fact {fact_id}")
        return self._echo(f)

    # ------------------------------------------------------------------ the one door

    def _ctx(self):
        """The local reader's context, with its presence refreshed (so tier-1 records are visible)."""
        try:
            return reader.begin_read(self.db_path, owner_actor_id=self.owner_id)
        except reader.ReaderError as exc:
            raise LedgerError(str(exc)) from None

    def _door(self, candidates: list[Candidate], *, surface: str = READ_SURFACE, ctx=None) -> list:
        """Every read of record CONTENT goes through `tiered_read`: authorised, audited once, then materialised."""
        ctx = ctx or self._ctx()
        out: list = []
        try:
            for i in range(0, max(len(candidates), 1), MAX_CANDIDATES):
                out.extend(tiered_read(ctx, candidates[i:i + MAX_CANDIDATES], database_path=self.db_path, surface=surface))
        except TieredReadRefused as exc:
            raise LedgerError(f"read refused: {exc}") from None
        return out

    @staticmethod
    def _cands(record_type: str, ids: list[int], lane: str = "list") -> list[Candidate]:
        return [Candidate(record_type=record_type, record_id=int(i), score=1.0, lane=lane) for i in ids]

    def current_beliefs(self, *, kind: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        limit = _clamp(limit, 1, 500)
        con = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
        try:
            sql = f"select id from facts where {facts.CURRENT_FACT_SQL}" + (" and fact_type = ?" if kind is not None else "")
            ids = [r[0] for r in con.execute(sql + " order by id desc limit ?", ([kind] if kind is not None else []) + [limit])]
        finally:
            con.close()
        content = {r.record_id: r.content for r in self._door(self._cands("fact", ids)) if r.record_type == "fact"}
        return [self._fact_dict(f, content[i]) for i in ids if i in content and (f := facts.get_fact(self.db_path, i))]

    def history(self, fact_id: int) -> dict[str, Any]:
        """Every version of a belief, who changed it, when, and why."""
        if facts.get_fact(self.db_path, fact_id) is None:
            raise LedgerError(f"no fact {fact_id}")
        before, after = facts.get_fact_supersession_chain(self.db_path, fact_id)
        chain = [*before, fact_id, *after]
        content = {r.record_id: r.content for r in self._door(self._cands("fact", chain)) if r.record_type == "fact"}
        if fact_id not in content:
            raise LedgerError(f"fact {fact_id} is not readable by this reader")
        versions = []
        names = {a.id: a.name for a in actors.list_actors(self.db_path)}
        for fid in chain:
            if fid not in content:
                continue
            f = facts.get_fact(self.db_path, fid)
            entries = audit.list_audit_entries(self.db_path, target_type="fact", target_id=fid)
            versions.append({**self._fact_dict(f, content[fid]), "changes": [
                {"action": e.action_type, "by": names.get(e.actor_id, e.actor_id), "reason": e.reason,
                 "at": getattr(e, "occurred_at", None) or getattr(e, "created_at", None)} for e in entries]})
        return {"fact_id": fact_id, "versions": versions}

    def as_of(self, when: str) -> dict[str, Any]:
        """What the memory believed at a past moment. A date alone means the start of that day; UTC unless an
        offset is given. Timestamps are compared as text, so the input is parsed and normalised first."""
        when = _utc_moment(when)
        try:
            view = temporal_view.fact_view(self.db_path, as_known=when)
        except Exception as exc:
            raise LedgerError(f"could not read beliefs as of {when!r}: {exc}") from exc
        ids = [tp.fact_id for tp in view.current]
        content = {r.record_id: r.content for r in self._door(self._cands("fact", ids)) if r.record_type == "fact"}
        out = []
        for fid in ids:
            if fid not in content:
                continue
            f = facts.get_fact(self.db_path, fid)
            out.append({"id": fid, "statement": content[fid], "kind": f.fact_type if f else None})
        return {"as_of": when, "beliefs": out}

    # ------------------------------------------------------------------ recall

    def recall(self, query: str, *, limit: int = 8) -> dict[str, Any]:
        query = (query or "").strip()
        if not query:
            raise LedgerError("query is empty")
        limit = _clamp(limit, 1, 50)
        refreshed = self.refresh()
        err = io.StringIO()
        with contextlib.redirect_stderr(err):  # the search prints lane warnings; return them instead
            fact_cands, _, _ = _fused_fact_candidates(self.db_path, query, limit=limit, sidecar_path=self.vectors_path)
            decision_cands = decision_candidates_for_query(self.db_path, query, limit=limit)
        # Decisions get a quarter of the slots (never fewer than they can fill); facts take the rest.
        d_slots = min(len(decision_cands), limit // 4)
        chosen = list(fact_cands[:limit - d_slots])
        chosen += decision_cands[:limit - len(chosen)]
        lanes = {("fact", c.record_id): ("fused" if c.lane == "funnel" else c.lane) for c in chosen if c.record_type == "fact"}
        lanes.update({("decision", c.record_id): c.lane for c in chosen if c.record_type == "decision"})
        scores = {(c.record_type, c.record_id): c.score for c in chosen}
        ctx = self._ctx()
        records = {(r.record_type, r.record_id): r for r in self._door(chosen, ctx=ctx)}
        results = []
        for c in chosen:
            rec = records.get((c.record_type, c.record_id))
            if rec is None:
                continue                                  # the door withheld it
            item: dict[str, Any] = {"type": c.record_type, "id": c.record_id, "lanes": [lanes[(c.record_type, c.record_id)]],
                                    "score": round(float(scores[(c.record_type, c.record_id)]), 4)}
            item.update(self._describe(c.record_type, c.record_id, rec.content, content=rec.content))
            results.append(item)
        if len(results) < limit:                          # handovers and links: not readable through the door
            results += self._other_hits(query, limit - len(results), ctx)
        warnings = list(refreshed["warnings"])
        if self.semantic_enabled:
            from ledger_memory.semantic_search import semantic_lane_blocked
            blocked = semantic_lane_blocked(self.db_path, self.vectors_path)
            if blocked:
                from ledger_memory.embeddings import is_legacy_embedding_backend
                backend_name = resolve_embedding_backend_name(self.db_path)
                if is_legacy_embedding_backend(backend_name):
                    fix = "the user runs `ledger semantic enable`"
                elif backend_name == "bge-m3":
                    fix = "indexing may still be running; `ledger semantic status` shows the state and reason"
                else:
                    fix = "the user runs `ledger reindex`"
                warnings.append(f"semantic lane disabled ({blocked}) (fix: {fix}; "
                                "results above came from keyword search only)")
        for line in err.getvalue().splitlines():
            w = line.strip().removeprefix("warning:").strip()
            if w and not (w.startswith("semantic") and not self.semantic_enabled) and w not in warnings:
                warnings.append(w)
        return {"query": query, "results": results, "index": self._index_status(), "warnings": warnings}

    def _other_hits(self, query: str, room: int, ctx) -> list[dict[str, Any]]:
        """Handover and link hits. The door serves facts and decisions only, so these are filtered against the
        reader's tier ceiling here and, when any are returned, counted as one more audited read."""
        try:
            device_id, ceiling = resolve_read_authority(ctx, database_path=self.db_path, surface=READ_SURFACE)
        except TieredReadRefused as exc:
            raise LedgerError(f"read refused: {exc}") from None
        wanted = {"context_package", "relationship"}
        with contextlib.redirect_stderr(io.StringIO()):
            hits = unified_search.unified_search(self.db_path, query, limit=room, sidecar_path=self.vectors_path,
                                                 permission_filter=lambda record_type, _id: record_type in wanted)
        table = {"context_package": "context_packages", "relationship": "relationships"}
        con = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
        out: list[dict[str, Any]] = []
        try:
            for h in hits:
                row = con.execute(f"select sensitivity_tier from {table[h.record_type]} where id = ?", (h.record_id,)).fetchone()
                if row is None or not may_see(row[0], ceiling):
                    continue
                item = {"type": h.record_type, "id": h.record_id, "lanes": sorted(h.lanes), "score": round(float(h.score), 4)}
                item.update(self._describe(h.record_type, h.record_id, h.title))
                out.append(item)
        finally:
            con.close()
        if out:
            record_read_audit(self.db_path, device_id=device_id, record_count=len(out), ceiling=ceiling, surface=READ_SURFACE)
        return out

    def _describe(self, record_type: str, record_id: int, title: str, content: str | None = None) -> dict[str, Any]:
        if record_type == "fact":
            f = facts.get_fact(self.db_path, record_id)
            return self._fact_dict(f, content) if f else {"text": title}
        if record_type == "decision":
            d = decisions.get_decision(self.db_path, record_id)
            return {"title": d.title, "status": d.status, "summary": d.summary, "rationale": d.rationale} if d else {"text": title}
        return {"text": title}

    # ------------------------------------------------------------------ decisions, links, handovers

    def record_decision(self, title: str, *, summary: str, rationale: str, status: str = "accepted",
                        fact_id: int | None = None, as_owner: bool = False) -> dict[str, Any]:
        title, summary, rationale = (_required(title, "title"), _required(summary, "summary"),
                                     _required(rationale, "rationale (why it was decided)"))
        if status not in decisions.DECISION_STATES:
            raise LedgerError(f"status must be one of {list(decisions.DECISION_STATES)}")
        if fact_id is not None and facts.get_fact(self.db_path, fact_id) is None:
            raise LedgerError(f"no fact {fact_id}")
        d = decisions.add_decision(self.db_path, title=title, summary=summary, rationale=rationale, status=status,
                                   fact_id=fact_id, actor_id=self._writer(as_owner))
        return {"decision": {"id": d.id, "title": d.title, "status": d.status}, "warnings": self._refresh("decision", d.id)}

    def link(self, source_type: str, source_id: int, target_type: str, target_id: int, relationship: str, *,
             summary: str | None = None, as_owner: bool = False) -> dict[str, Any]:
        relationship = _required(relationship, "relationship (e.g. depends_on, supports, contradicts)")
        for kind, rid in ((source_type, source_id), (target_type, target_id)):
            if kind not in LINKABLE:
                raise LedgerError(f"record type must be one of {sorted(LINKABLE)}, not {kind!r}")
            if not self._exists(kind, rid):
                raise LedgerError(f"no {kind} {rid}")
        if (source_type, source_id) == (target_type, target_id):
            raise LedgerError("a record cannot be linked to itself")
        r = relationships.add_relationship(self.db_path, source_type=source_type, source_id=source_id,
                                           target_type=target_type, target_id=target_id, relationship=relationship,
                                           summary=summary, actor_id=self._writer(as_owner))
        return {"link": {"id": r.id, "from": f"{source_type}:{source_id}", "to": f"{target_type}:{target_id}",
                         "relationship": relationship}, "warnings": self._refresh("relationship", r.id)}

    def handover(self, objective: str, *, status_summary: str, fact_ids: list[int] | None = None,
                 decision_ids: list[int] | None = None, open_questions: str | None = None, risks: str | None = None,
                 next_actions: str | None = None, as_owner: bool = False) -> dict[str, Any]:
        """Bundle context for another session or agent to pick up. Everything is validated BEFORE the first write:
        the store is append-only, so a half-built handover could never be removed."""
        objective, status_summary = _required(objective, "objective"), _required(status_summary, "status_summary")

        # Review fix (0.3.0-rc2): duplicates used to fail mid-way and leave a half-built handover; now they are
        # dropped (first-seen order kept) and the package, its items and its event are written in ONE transaction.
        def _dedup(seq: list[int] | None) -> list[int]:
            seen = set()
            out = []
            for x in seq or []:
                if x not in seen:
                    seen.add(x)
                    out.append(x)
            return out

        fact_ids = _dedup(fact_ids)
        decision_ids = _dedup(decision_ids)

        for kind, ids in (("fact", fact_ids), ("decision", decision_ids)):
            missing = [i for i in ids if not self._exists(kind, i)]
            if missing:
                raise LedgerError(f"no {kind} {', '.join(map(str, missing))}")

        writer = self._writer(as_owner)
        items = [
            ContextPackageItem(item_type="fact", item_id=fid, relationship="supports")
            for fid in fact_ids
        ] + [
            ContextPackageItem(item_type="decision", item_id=did, relationship="supports")
            for did in decision_ids
        ]

        pkg = context_packages.create_context_package(
            self.db_path, objective=objective, status_summary=status_summary,
            open_questions=open_questions, risks=risks,
            recommended_next_actions=next_actions, actor_id=writer, items=items
        )
        items = context_packages.list_context_package_items(self.db_path, pkg.id)
        md = context_packages.render_context_package_markdown(pkg, items)
        return {"handover_id": pkg.id, "markdown": md, "warnings": self._refresh("context_package", pkg.id)}

    # ------------------------------------------------------------------ documents

    def ingest(self, *, text: str | None = None, path: str | None = None, uri: str | None = None,
               media_type: str = "text/plain", as_owner: bool = False,
               confine_to: list[Path] | None = None) -> dict[str, Any]:
        """confine_to: when given (the agent-facing server), a path must resolve (symlinks followed) to inside one of
        these folders and contain no hidden component. The CLI passes nothing: a person typed that path."""
        if (text is None) == (path is None):
            raise LedgerError("give exactly one of text or path")
        if path is not None:
            p = Path(path).expanduser()
            if confine_to is not None:
                self._check_confined(p, confine_to)
            if not p.is_file():
                raise LedgerError(f"not a readable file: {p}")
            text = p.read_text(encoding="utf-8", errors="replace")
            uri = uri or p.resolve().as_uri()
        if not (text or "").strip():
            raise LedgerError("document is empty")
        writer = self._writer(as_owner)
        doc = documents.ingest_document(self.db_path, content=text, origin_type="file" if path else "text",
                                        media_type=media_type, uri=uri, retrieved_at=_now_iso(), actor_id=writer)
        made = chunks.chunk_document(self.db_path, document_id=doc.id, chunker=chunkers.BoundaryChunker(), actor_id=writer)
        warnings: list[str] = []
        embedded = 0
        if self._semantic_deferred:
            warnings.extend(self._request_semantic_maintenance())
        elif self.semantic_enabled:
            try:
                embedded = chunk_embeddings.embed_chunks(self.db_path, document_id=doc.id, actor_id=writer,
                                                         sidecar_path=self.vectors_path).embedded_rows
            except Exception as exc:
                warnings.append(f"chunks stored but not embedded ({type(exc).__name__}: {exc})")
        return {"document_id": doc.id, "uri": uri, "chunks": len(made), "embedded": embedded, "warnings": warnings}

    def _check_confined(self, p: Path, roots: list[Path]) -> None:
        resolved = p.resolve()
        for root in roots:
            try:
                rel = resolved.relative_to(root.resolve())
            except ValueError:
                continue
            if any(part.startswith(".") for part in rel.parts):
                break
            return
        raise LedgerError(
            "path not allowed: the memory server reads files only inside the folder it was started in or "
            f"{self.home / 'inbox'}, and never hidden files or folders. Paste the text instead (text=...), or ask the "
            "user to run `ledger ingest FILE`.")

    def search_documents(self, query: str, *, limit: int = 5) -> dict[str, Any]:
        limit = _clamp(limit, 1, 50)
        query = (query or "").strip()
        if not query:
            raise LedgerError("query is empty")
        ctx = self._ctx()
        try:
            device_id, ceiling = resolve_read_authority(ctx, database_path=self.db_path, surface=READ_SURFACE)
        except TieredReadRefused as exc:
            raise LedgerError(f"read refused: {exc}") from None
        hits: list[dict[str, Any]] = []
        mode = "semantic"
        semantic_reason = None
        if self.semantic_enabled:
            semantic_rows, semantic_reason = rag_search.rag_search_outcome(
                self.db_path, query, limit=limit, sidecar_path=self.vectors_path)
            for r in semantic_rows:
                hits.append({"document_id": r.document_id, "uri": r.document_uri, "chunk_id": r.chunk_id,
                             "score": round(float(r.score), 4), "text": r.content})
        if not hits:
            mode = "keyword (semantic search off)" if not self.semantic_enabled else "keyword (no semantic hits)"
            if semantic_reason is not None:
                mode = "keyword (semantic unavailable)"
            hits = _keyword_chunks(self.db_path, query, limit)
        # Documents are not records the door serves (it reads facts and decisions), so each hit is checked against
        # the document's own tier here, and the whole read is logged once, like every other read.
        con = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
        try:
            tiers = {r[0]: r[1] for r in con.execute(
                f"select id, sensitivity_tier from documents where id in ({','.join('?' * len(hits))})",
                [h["document_id"] for h in hits])} if hits else {}
        finally:
            con.close()
        hits = [h for h in hits if may_see(tiers.get(h["document_id"], 3), ceiling)]
        record_read_audit(self.db_path, device_id=device_id, record_count=len(hits), ceiling=ceiling, surface=READ_SURFACE)
        response = {"query": query, "mode": mode, "results": hits}
        if semantic_reason is not None:
            response["semantic_unavailable_reason"] = semantic_reason
        return response

    # ------------------------------------------------------------------ the briefing and the bounded look-up

    def reset_window(self) -> None:
        """Open a new reply window for `recall_more`: at most `context.tool_reads_max` reads (default 3, never above 5)."""
        self._window = ReadBudget(tool_read_settings(self.db_path).max_reads)
        self._window_entries = 0

    def brief(self, question: str, *, budget_tokens: int = 1500) -> dict[str, Any]:
        """The budgeted briefing for one user question; also opens a new reply window for `recall_more`.
        Budgets below 200 tokens are raised to 200; `tokens_estimated` is the estimate of the whole returned payload."""
        from ledger_memory import briefing
        question = (question or "").strip()
        if not question:
            raise LedgerError("question is empty")
        budget_tokens = _clamp(budget_tokens, briefing.MIN_BUDGET, briefing.MAX_BUDGET)
        self.refresh()
        ctx = self._ctx()
        self.reset_window()
        try:
            out = briefing.build_brief(self.db_path, self.vectors_path, ctx, question, budget_tokens, AUTHORITY_LABELS,
                                       reply_window={"recall_more_reads": self._window.max_reads})
        except TieredReadRefused as exc:
            raise LedgerError(f"read refused: {exc}") from None
        return out

    def recall_more(self, query: str) -> dict[str, Any]:
        """A bounded extra look-up when the briefing lacks something. Refuses a malformed query without using a read;
        refuses once the reply window's reads are spent."""
        if _clean_arguments("recall_more", {"query": query}) is None:
            return self._window_result(REFUSED_MALFORMED)
        q = _clean_arguments("recall_more", {"query": query})["query"]
        if not self._window.take():
            return self._window_result(LIMIT_REACHED)
        self.refresh()
        self._ctx()                                       # presence first; the authorizer verifies the credential itself
        result = tool_read_authorizer(self.db_path, surface=READ_SURFACE).authorize(
            ToolReadRequest(query=q, surface=READ_SURFACE, reply_id="mcp", read_number=self._window.used))
        text, entries = render_result(result, self._window_entries)
        self._window_entries += len(entries)
        out = self._window_result(text)
        out["entries"] = [{"index": e.index, "type": e.record_type, "id": e.record_id} for e in entries]
        return out

    def _window_result(self, text: str) -> dict[str, Any]:
        return {"result": text, "reads_used": self._window.used, "reads_remaining": self._window.remaining}

    # ------------------------------------------------------------------ operations

    def status(self) -> dict[str, Any]:
        con = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
        q = lambda sql: con.execute(sql).fetchone()[0]
        out = {
            "home": str(self.home),
            "database_mb": round(self.db_path.stat().st_size / 1048576, 2),
            "facts_current": q("select count(*) from facts where superseded_by_fact_id is null"),
            "facts_superseded": q("select count(*) from facts where superseded_by_fact_id is not null"),
            "facts_disputed": q("select count(distinct target_id) from audit_entries where action_type = 'fact.disputed'"),
            "decisions": q("select count(*) from decisions"),
            "documents": q("select count(*) from documents"),
            "handovers": q("select count(*) from context_packages"),
            "audit_entries": q("select count(*) from audit_entries"),
            "embedding_backend": resolve_embedding_backend_name(self.db_path),
            "index": self._index_status(),
        }
        out["recovery"] = getattr(self, "recovery_note", None)
        con.close()
        return out

    def backup(self, label: str = "ledger") -> dict[str, Any]:
        stamp = _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%d-%H%M%S")
        try:
            r = db_packaging.package_database(self.db_path, output_dir=self.backups_dir, label=label, timestamp=stamp)
        except _PACKAGING_REFUSALS as exc:
            raise LedgerError(_refusal_text(exc)) from exc
        return {k: (str(v) if isinstance(v, Path) else v) for k, v in asdict(r).items()}

    @contextlib.contextmanager
    def session(self) -> Iterator["Ledger"]:
        """Wrap use of the memory in a session so a restore can see active users.

        Library users should wrap their use of the memory in this context manager.
        While a session is held, an in-place restore will be refused.
        """
        with locking.shared(locking.session_lock_path(self.db_path), timeout=120):
            # A restore may have happened since this object last held a session: ids cached from the old memory would
            # point at actors the restored one does not have. Re-resolve them, and start a fresh reply window.
            self._owner_id = self._agent_id = None
            self.reset_window()
            yield self

    def _marker_path(self) -> Path:
        return self.home / "restore-in-progress.json"

    @staticmethod
    def _file_identity(path: Path) -> list[int] | None:
        """(device, inode) of the database file. Replacing the file (`os.replace`) gives it a new identity; ordinary
        writes never do — so, unlike a content hash, this proves whether a swap happened."""
        try:
            info = os.stat(path)
        except FileNotFoundError:
            return None
        return [info.st_dev, info.st_ino]

    def _write_marker(self, stage: str, target: Path, pre_image_identity: list[int] | None) -> None:
        import json
        marker = self._marker_path()
        data = {"stage": stage, "target": str(target), "pre_image_identity": pre_image_identity,
                "started_at": _dt.datetime.now(_dt.timezone.utc).isoformat()}
        tmp = marker.with_name(marker.name + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, marker)

    def _vector_step(self) -> str | dict:
        """The vector step of an in-place restore (also run by crash recovery); returns what goes in result['vectors']."""
        if self.semantic_enabled:
            return self.refresh(repair=True)
        stamp = _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%d%H%M%S")
        for suffix in ("", "-wal", "-shm"):
            src = Path(f"{self.vectors_path}{suffix}")
            if src.exists():
                os.replace(src, Path(f"{self.vectors_path}.before-restore-{stamp}{suffix}"))
        return (f"vectors from before the restore moved aside ({self.vectors_path.name}"
                f".before-restore-{stamp}); a fresh set is built when semantic search is on")

    def _recover_interrupted_restore(self) -> str | None:
        """Run by `ensure()` under the start-up lock: finish or report a restore that a crash interrupted."""
        import json
        marker = self._marker_path()
        if not marker.exists():
            return None
        unreadable = LedgerError(f"an interrupted restore left an unreadable marker at {marker}; inspect it, then "
                                 f"remove it to start")
        try:
            data = json.loads(marker.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            raise unreadable from None
        if (not isinstance(data, dict) or data.get("stage") not in ("restoring", "replaced")
                or not isinstance(data.get("target"), str)
                or not (data.get("pre_image_identity") is None or (isinstance(data.get("pre_image_identity"), list)
                        and len(data["pre_image_identity"]) == 2))):
            raise unreadable
        if Path(data["target"]).resolve() != self.db_path.resolve():
            marker.unlink(missing_ok=True)          # a restore into another file never affects this memory
            return None
        staging = self.db_path.with_name(self.db_path.name + ".restoring")
        if staging.exists() and not staging.is_symlink():
            staging.unlink()                        # a crashed restore's staging copy; it would block the next restore
        swapped = (data["stage"] == "replaced"
                   or self._file_identity(self.db_path) != data["pre_image_identity"])
        if not swapped:
            marker.unlink(missing_ok=True)          # the database file is the same one: its -wal is live, keep it
            return "an interrupted restore changed nothing"
        if data["stage"] == "restoring":
            # The swap happened but its clean-up may not have: side files left beside the NEW file belong to the old one.
            for suffix in ("-wal", "-shm"):
                side = Path(f"{self.db_path}{suffix}")
                if side.exists() and not side.is_symlink():
                    side.unlink()
        self._owner_id = self._agent_id = None
        note = "an interrupted restore was completed"
        try:
            self._vector_step()
        except Exception as exc:  # noqa: BLE001 — a derived index must never keep the memory from starting
            note += f"; the search index could not be rebuilt yet ({type(exc).__name__}): run `ledger reindex`"
        marker.unlink(missing_ok=True)
        return note

    def restore(self, archive: str, out: str, *, force: bool = False) -> dict[str, Any]:
        """Restore a backup into another file, or over the live memory with force. Verifies the sha256 sidecar first.
        Over the live memory it refuses while any session uses the memory, records its progress so a crash part-way is
        finished or reported on the next start, and re-syncs the vectors file (which otherwise still describes the
        replaced state, including records the restore removed)."""
        # Review fix (0.3.0-rc3): the start-up lock keeps new sessions out; an in-place restore ALSO takes the session
        # lock exclusively, so it refuses while any session (agent client, ledger command, background indexing) is open.
        target = Path(out).expanduser()
        in_place = target.resolve() == self.db_path.resolve()
        with contextlib.ExitStack() as stack:
            try:
                stack.enter_context(locking.exclusive(reader.lock_path(self.db_path), timeout=30))
            except TimeoutError:
                raise LedgerError("restore refused: another process is starting or upgrading this memory; "
                                  "close every session and retry") from None
            if not in_place:
                try:
                    r = db_packaging.restore_database(Path(archive).expanduser(), target, force=force)
                except _PACKAGING_REFUSALS as exc:
                    raise LedgerError(_refusal_text(exc)) from exc
                return {k: (str(v) if isinstance(v, Path) else v) for k, v in asdict(r).items()}
            try:
                stack.enter_context(locking.exclusive(locking.session_lock_path(self.db_path), timeout=0))
            except TimeoutError:
                raise LedgerError(
                    "restore refused: this memory is in use by another session "
                    "(an agent client, a ledger command or background indexing); close them and retry") from None
            before = self._file_identity(target)
            self._write_marker("restoring", target, before)
            try:
                r = db_packaging.restore_database(Path(archive).expanduser(), target, force=force)
            except BaseException as exc:
                if self._file_identity(target) == before:
                    # Nothing was replaced: the memory is exactly as it was.
                    self._marker_path().unlink(missing_ok=True)
                    if isinstance(exc, _PACKAGING_REFUSALS):
                        raise LedgerError(_refusal_text(exc)) from exc
                    raise
                # The database WAS replaced but a later step failed: keep the marker; the next start finishes it.
                raise LedgerError("restore was interrupted after the database was replaced; the next start of the "
                                  "memory finishes it") from exc
            self._write_marker("replaced", target, before)
            self._owner_id = self._agent_id = None
            result = {k: (str(v) if isinstance(v, Path) else v) for k, v in asdict(r).items()}
            result["vectors"] = self._vector_step()
            self._marker_path().unlink(missing_ok=True)
        # The locks are released: background indexing that stepped aside during the restore can run now.
        if self._semantic_deferred:
            result.setdefault("warnings", []).extend(self._request_semantic_maintenance())
        return result

    def check(self) -> dict[str, Any]:
        r = integrity.check_integrity(self.db_path)
        return {"ok": r.ok, "report": {k: (v if isinstance(v, (int, float, bool, str, type(None))) else str(v))
                                       for k, v in asdict(r).items()}}


# Every anticipated way a backup or restore says "no": surfaced as a refusal, never as a crash.
_PACKAGING_REFUSALS = (
    db_packaging.ChecksumMismatchError, db_packaging.SourceIntegrityError, db_packaging.RestoreTargetBusyError,
    db_packaging.UnverifiedArchiveError, db_packaging.UnsafeLabelError, db_packaging.BackupCollisionError,
    FileExistsError, FileNotFoundError, IsADirectoryError, PermissionError, lzma.LZMAError, ValueError, RuntimeError,
)


def _refusal_text(exc: BaseException) -> str:
    if isinstance(exc, db_packaging.UnverifiedArchiveError):
        return "the archive has no .sha256 file next to it; only verified backups are restored"
    return str(exc).replace("(use force)", "(pass --force to overwrite it)")


def documents_exists(database_path: Path, document_id: int) -> bool:
    try:
        documents.get_document(database_path, document_id=document_id)
        return True
    except Exception:
        return False


def _keyword_chunks(database_path: Path, query: str, limit: int) -> list[dict[str, Any]]:
    """Fallback when semantic search is off: chunks containing every query word (case-insensitive)."""
    words = [w for w in query.lower().split() if len(w) > 2] or [query.lower()]
    where = " AND ".join("lower(c.content) LIKE ?" for _ in words)
    con = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True)
    rows = con.execute(
        f"SELECT c.id, c.document_id, d.uri, c.content FROM chunks c JOIN documents d ON d.id = c.document_id "
        f"WHERE {where} ORDER BY c.id LIMIT ?", [f"%{w}%" for w in words] + [limit]).fetchall()
    con.close()
    return [{"document_id": r[1], "uri": r[2], "chunk_id": r[0], "text": r[3]} for r in rows]
