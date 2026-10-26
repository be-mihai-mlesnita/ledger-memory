"""MCP server for ledger-memory (stdio). Launched by the agent's MCP client, e.g. opencode.

There is deliberately NO delete tool and no edit-in-place tool. Beliefs change by `revise` (with a
reason) or `dispute`; the history of every belief stays queryable forever.

Environment:
  LEDGER_HOME                where the memory lives (default ~/.ledger-memory)
  LEDGER_AGENT_NAME          the actor name writes are attributed to (default "agent")
  LEDGER_EMBEDDING_BACKEND   "bge-m3" for explicitly configured local semantic search
"""
from __future__ import annotations

import contextlib

from pathlib import Path
import logging
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from ledger_memory import locking
from ledger_memory.api import Ledger, LedgerError

INSTRUCTIONS = """\
This is your long-term memory. It is a ledger: nothing is deleted or overwritten, every change is
attributed to you, and old beliefs stay recoverable.

For each question the user asks: call `brief` ONCE with the question. It returns what memory knows that bears on
it: facts, the decisions linked to them, any contradiction between facts it shows, interpretations, the date of the
newest record shown, and the gaps. Answer from it. Only when the briefing lacks something, call `recall_more` with a
short, specific query (a few calls per question at most; the next `brief` starts a new allowance).
For exploring, `recall` searches by topic and `current_beliefs` gives an overview.
Before you assume something about the user, their preferences, their systems or past decisions: `brief` or `recall` first.

When you learn something durable, `remember` it — one fact per call, self-contained, specific.
Set `source` honestly: "user_said" only if the user actually said it; "inferred" if you concluded it;
"observed" if you measured or saw it; "document" if an ingested document backs it.
Never re-label an inference as "user_said".

When a remembered fact is wrong or outdated: `revise` it with the correction and the reason. Do not
remember a contradicting fact alongside it. If you are unsure which is right: `dispute` it.

Record significant choices with `record_decision`. Before ending a long task, `create_handover`.
Results tell you which search lanes answered and whether anything was degraded — read the warnings.
Do not store secrets, passwords, tokens or credentials.
"""

READ = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False)
WRITE = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=False)

START_CWD = Path.cwd().resolve()     # the project the agent works in, fixed when the server starts

server = MCPServer(name="ledger-memory", instructions=INSTRUCTIONS, version="0.3.0rc3")
_ledger: Ledger | None = None


def L() -> Ledger:
    global _ledger
    if _ledger is None:
        _ledger = Ledger().ensure()
    return _ledger


def _call(fn, *args, **kwargs) -> Any:
    try:
        return fn(*args, **kwargs)
    except LedgerError as exc:
        # ToolError = an anticipated refusal: its message reaches the agent so it can correct itself.
        # Any other exception is a crash, and the SDK deliberately keeps its text on the server.
        raise ToolError(str(exc)) from None


@server.tool(annotations=WRITE)
def remember(statement: str, source: str, kind: str = "note", confidence: float | None = None,
             document_id: int | None = None) -> dict[str, Any]:
    """Store one durable fact. source: "user_said" | "inferred" | "observed" | "document".
    kind: a short category such as "preference", "person", "system", "process", "note".
    confidence: 0-1, optional. document_id: required when source is "document"."""
    return _call(L().remember, statement, source=source, kind=kind, confidence=confidence, document_id=document_id)


@server.tool(annotations=READ)
def recall(query: str, limit: int = 8) -> dict[str, Any]:
    """Search memory (facts, decisions, handovers, links) by keywords and meaning. Superseded facts are
    excluded; use `history` to see them. Each result lists the search lanes that matched."""
    return _call(L().recall, query, limit=limit)


@server.tool(annotations=READ)
def brief(question: str, budget_tokens: int = 1500) -> dict[str, Any]:
    """Call once per user question. Returns what memory knows about it as structured fields: facts, decisions
    (title and summary, no rationale), contradictions between shown facts, interpretations, newest_known_at (the date
    of the newest record shown) and gaps (what was left out and why). The whole returned payload stays within
    budget_tokens (budgets below 200 are raised to 200). Starts a new allowance of recall_more calls."""
    return _call(L().brief, question, budget_tokens=budget_tokens)


@server.tool(annotations=READ)
def recall_more(query: str) -> dict[str, Any]:
    """Look up more when the briefing lacks something. Use a short, specific query. At most a few reads per
    question (default 3, never more than 5); after that it answers that the read limit is reached until the next
    `brief`. Cite results by their [rN] marks."""
    return _call(L().recall_more, query)


@server.tool(annotations=WRITE)
def revise(fact_id: int, statement: str, reason: str, confidence: float | None = None,
           source: str | None = None, document_id: int | None = None) -> dict[str, Any]:
    """Replace a fact with a corrected version. The old version is kept and linked. reason is required.
    source: where the CORRECTED statement comes from — 'user_said' only if the user stated the correction,
    'document' with document_id, otherwise omit (an agent's correction of something the user said or a document
    backs is recorded as inferred)."""
    return _call(L().revise, fact_id, statement, reason=reason, confidence=confidence,
                 source=source, document_id=document_id)


@server.tool(annotations=WRITE)
def dispute(fact_id: int, reason: str, conflicts_with: int | None = None) -> dict[str, Any]:
    """Flag a fact as contested without replacing it, e.g. when two sources disagree. reason is required.
    conflicts_with: the id of the other fact it contradicts, so `brief` can show the two as a contradiction."""
    return _call(L().dispute, fact_id, reason=reason, conflicts_with=conflicts_with)


@server.tool(annotations=READ)
def history(fact_id: int) -> dict[str, Any]:
    """Every version of a fact: what it said, who changed it, when, and why."""
    return _call(L().history, fact_id)


@server.tool(annotations=READ)
def beliefs_as_of(when: str) -> dict[str, Any]:
    """What memory believed at a past moment. when: ISO-8601 UTC, e.g. "2026-09-30T14:00:00"."""
    return _call(L().as_of, when)


@server.tool(annotations=READ)
def current_beliefs(kind: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
    """The newest current (not superseded) facts, optionally of one kind."""
    return _call(L().current_beliefs, kind=kind, limit=limit)


@server.tool(annotations=WRITE)
def record_decision(title: str, summary: str, rationale: str, status: str = "accepted",
                    fact_id: int | None = None) -> dict[str, Any]:
    """Record a decision and why it was made. fact_id optionally links the fact it rests on."""
    return _call(L().record_decision, title, summary=summary, rationale=rationale, status=status, fact_id=fact_id)


@server.tool(annotations=WRITE)
def link_records(source_type: str, source_id: int, target_type: str, target_id: int, relationship: str,
                 summary: str | None = None) -> dict[str, Any]:
    """Link two records, e.g. fact 12 "depends_on" fact 4. Types: fact, decision, context_package, document."""
    return _call(L().link, source_type, source_id, target_type, target_id, relationship, summary=summary)


@server.tool(annotations=WRITE)
def create_handover(objective: str, status_summary: str, fact_ids: list[int] | None = None,
                    decision_ids: list[int] | None = None, open_questions: str | None = None,
                    risks: str | None = None, next_actions: str | None = None) -> dict[str, Any]:
    """Bundle the context another session or agent needs to continue this work. Returns Markdown."""
    return _call(L().handover, objective, status_summary=status_summary, fact_ids=fact_ids,
                 decision_ids=decision_ids, open_questions=open_questions, risks=risks, next_actions=next_actions)


@server.tool(annotations=WRITE)
def ingest_document(text: str | None = None, path: str | None = None, uri: str | None = None,
                    media_type: str = "text/plain") -> dict[str, Any]:
    """Store a text document so its contents become searchable. Give text, or a path to a text file inside the
    project folder this server was started in (or the memory's inbox folder); hidden files and folders and anything
    outside are refused: paste the text instead."""
    if path is not None:
        p = Path(path).expanduser()
        path = str(p if p.is_absolute() else START_CWD / p)
    return _call(L().ingest, text=text, path=path, uri=uri, media_type=media_type,
                 confine_to=[START_CWD, L().home / "inbox"])


@server.tool(annotations=READ)
def search_documents(query: str, limit: int = 5) -> dict[str, Any]:
    """Search inside ingested documents. Returns matching passages and their source."""
    return _call(L().search_documents, query, limit=limit)


@server.tool(annotations=READ)
def memory_status() -> dict[str, Any]:
    """Counts, where memory lives, whether meaning-based search is on, and index health."""
    return _call(L().status)


def _semantic_startup(ledger: Ledger) -> list[str]:
    """Warm an unwarmed opted-in fingerprint; resume derived work without downloading."""
    from ledger_memory.embeddings import resolve_embedding_backend_name

    try:
        if resolve_embedding_backend_name(ledger.db_path, strict=True) != "bge-m3":
            return []
    except Exception:
        return ["semantic startup configuration unavailable; keyword retrieval remains available"]
    warnings = []
    try:
        from ledger_memory.embedding_worker import ensure_worker, read_worker_state, worker_profile
        from ledger_memory.model_artifacts import _manifest

        profile = worker_profile()
        _, fingerprint = _manifest(None)
        # This receipt only suppresses redundant eager warmup, never proves current readiness.
        if read_worker_state(profile).get("warm_fingerprint") != fingerprint:
            ensure_worker(profile=profile)
    except Exception:
        warnings.append("semantic startup warmup unavailable; keyword retrieval remains available")
    try:
        from ledger_memory.semantic_pending import request_drain

        request_drain(ledger.db_path, ledger.vectors_path)
    except Exception:
        warnings.append("semantic startup maintenance unavailable; keyword retrieval remains available")
    return warnings


_SESSION: contextlib.ExitStack | None = None   # the shared session lock, held for the whole server process


def main() -> None:
    global _SESSION
    stack = contextlib.ExitStack()
    try:
        stack.enter_context(locking.shared(locking.session_lock_path(Ledger().db_path), timeout=120))
    except TimeoutError:
        logging.getLogger(__name__).error("memory is being restored; the server did not start")
        raise SystemExit(3) from None
    _SESSION = stack   # held, not closed: an in-place restore refuses while this server runs
    ledger = L()  # bad LEDGER_HOME still fails before accepting the first client call
    for warning in _semantic_startup(ledger):
        logging.getLogger(__name__).warning(warning)
    server.run(transport="stdio")


if __name__ == "__main__":
    main()
