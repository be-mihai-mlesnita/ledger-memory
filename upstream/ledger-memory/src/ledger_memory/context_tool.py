"""A bounded, read-only, model-callable context tool. Provider-agnostic core: the tool
schema, OpenAI and Anthropic payload converters, the closed mode set, the hard read limits, the per-reply read budget,
the outward-tool check and the tool loop. It never reads storage: the host supplies a ToolReadAuthorizer that owns
identity, sensitivity and audit, and a `send` transport. See docs/architecture/MODEL-CALLABLE-CONTEXT-TOOL.md."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Protocol, Any



# Constants
TOOL_NAME = "recall_more"
OFF = "off"
BOUNDED = "bounded"
MODES = (OFF, BOUNDED)
HARD_MAX_READS = 5
DEFAULT_MAX_READS = 3
MAX_RECORDS_PER_READ = 6
MAX_QUERY_CHARS = 200
OUTWARD_RISK_CLASSES = frozenset({
    "external_message", "external_booking", "payment_or_purchase",
    "destructive_external", "external_search", "security_admin"
})
TOOL_DESCRIPTION = (
    "Look up more of the owner's recorded memory when the briefing you were given does not cover "
    "the question. Read-only. Returns at most a few dated, indexed lines; cite them as [rN]. "
    "Use a short, specific query."
)
REFUSED_MALFORMED = "RECALL: refused (the call was malformed; pass one string argument named query)."
LIMIT_REACHED = "RECALL: read limit reached for this reply."
NOTHING_FOUND = "RECALL: nothing found for that query."
PRIVATE_NOTE = "(some matches may be private and not available to this tool)"
RESULT_HEADER = "RECALL (dated; cite [rN] when you use one):"


# Dataclasses
@dataclass(frozen=True)
class ToolCall:
    call_id: str
    name: str
    arguments: dict | None


@dataclass(frozen=True)
class ToolReadRequest:
    query: str
    surface: str
    reply_id: str
    read_number: int


@dataclass(frozen=True)
class ManifestEntry:
    """One citable line a tool read shipped. ``payload`` is the host's own citation row (ledger: a
    ``claim_envelope.ManifestRow``), carried through untouched; the core never reads it."""

    index: str
    record_type: str
    record_id: int
    payload: Any = None


@dataclass(frozen=True)
class ToolReadResult:
    lines: tuple[str, ...]
    entries: tuple[ManifestEntry, ...]
    withheld_private: bool = False
    refused_reason: str | None = None


@dataclass(frozen=True)
class LoopOutcome:
    content: str
    entries: tuple[ManifestEntry, ...]
    reads_used: int
    rounds: int


# Protocol
class ToolReadAuthorizer(Protocol):
    def authorize(self, request: ToolReadRequest) -> ToolReadResult: ...


# Functions
def tool_schema() -> dict:
    """Return a NEW dict each call."""
    return {
        "type": "function",
        "function": {
            "name": TOOL_NAME,
            "description": TOOL_DESCRIPTION,
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "maxLength": MAX_QUERY_CHARS,
                        "description": "What to look up, in a few words."
                    }
                },
                "required": ["query"],
                "additionalProperties": False
            }
        }
    }


def to_anthropic_tool(schema: dict) -> dict:
    """Convert OpenAI function schema to Anthropic tool format."""
    # Deep copy of parameters
    params = json.loads(json.dumps(schema["function"]["parameters"]))
    return {
        "name": schema["function"]["name"],
        "description": schema["function"]["description"],
        "input_schema": params
    }


def _clean_arguments(name: str, args: Any) -> dict | None:
    """Return cleaned arguments dict or None if malformed."""
    if name != TOOL_NAME:
        return None
    if not isinstance(args, dict):
        return None
    if "query" not in args:
        return None
    query = args["query"]
    if not isinstance(query, str):
        return None
    stripped = query.strip()
    if not stripped:
        return None
    if len(args) != 1:
        return None
    return {"query": stripped[:MAX_QUERY_CHARS]}


def parse_openai_tool_calls(message: dict) -> list[ToolCall]:
    """Parse OpenAI tool_calls from a chat message."""
    if not isinstance(message, dict):
        return []
    tool_calls = message.get("tool_calls")
    if not isinstance(tool_calls, list):
        return []
    result = []
    for item in tool_calls:
        if not isinstance(item, dict):
            continue
        call_id = str(item.get("id") or "")
        fn = item.get("function")
        if not isinstance(fn, dict):
            fn = {}
        name = str(fn.get("name") or "")
        raw = fn.get("arguments")
        args: dict | None = None
        if isinstance(raw, str):
            try:
                args = json.loads(raw)
            except Exception:
                args = None
        elif isinstance(raw, dict):
            args = raw
        else:
            args = None
        cleaned = _clean_arguments(name, args)
        result.append(ToolCall(call_id, name, cleaned))
    return result


def parse_anthropic_tool_uses(content_blocks: list) -> list[ToolCall]:
    """Parse Anthropic tool_use blocks."""
    if not isinstance(content_blocks, list):
        return []
    result = []
    for block in content_blocks:
        if not isinstance(block, dict):
            continue
        if block.get("type") != "tool_use":
            continue
        call_id = str(block.get("id") or "")
        name = str(block.get("name") or "")
        raw_input = block.get("input")
        cleaned = _clean_arguments(name, raw_input)
        result.append(ToolCall(call_id, name, cleaned))
    return result


def openai_tool_result_message(call_id: str, text: str) -> dict:
    return {"role": "tool", "tool_call_id": call_id, "content": text}


def anthropic_tool_result_block(call_id: str, text: str) -> dict:
    return {"type": "tool_result", "tool_use_id": call_id, "content": text}


def parse_mode(raw: Any) -> str:
    if isinstance(raw, str) and raw in MODES:
        return raw
    return OFF


def clamp_max_reads(raw: Any) -> int:
    if isinstance(raw, bool):
        return DEFAULT_MAX_READS
    try:
        if isinstance(raw, str):
            s = raw.strip()
            if not s.isdigit():
                return DEFAULT_MAX_READS
            val = int(s)
        elif isinstance(raw, int):
            val = raw
        else:
            return DEFAULT_MAX_READS
    except Exception:
        return DEFAULT_MAX_READS
    if val < 1:
        return DEFAULT_MAX_READS
    if val > HARD_MAX_READS:
        return HARD_MAX_READS
    return val


class ReadBudget:
    def __init__(self, max_reads: Any):
        self.max_reads = clamp_max_reads(max_reads)
        self.used = 0

    def take(self) -> bool:
        if self.used >= self.max_reads:
            return False
        self.used += 1
        return True

    @property
    def remaining(self) -> int:
        return self.max_reads - self.used


def outward_tools_present(tool_names: list[str], policy_lookup: Any) -> bool:
    for name in tool_names:
        if name == TOOL_NAME:
            continue
        try:
            risk = policy_lookup(name)
        except Exception:
            return True
        if risk is None:
            return True
        if risk in OUTWARD_RISK_CLASSES:
            return True
    return False


def render_result(result: ToolReadResult, start_index: int) -> tuple[str, tuple[ManifestEntry, ...]]:
    if result.refused_reason:
        return (f"RECALL: refused ({result.refused_reason}).", ())

    # Pair lines and entries up to min length, then take at most MAX_RECORDS_PER_READ
    pairs = list(zip(result.lines, result.entries))
    pairs = pairs[:MAX_RECORDS_PER_READ]

    if not pairs:
        text = NOTHING_FOUND
        if result.withheld_private:
            text += "\n" + PRIVATE_NOTE
        return (text, ())

    lines_out = [RESULT_HEADER]
    new_entries = []
    for i, (line, entry) in enumerate(pairs):
        clean_line = line.replace("\n", " ").replace("\r", " ").strip()
        idx = f"r{start_index + i}"
        lines_out.append(f"- [{idx}] {clean_line}")
        new_entries.append(ManifestEntry(idx, entry.record_type, entry.record_id, entry.payload))

    if result.withheld_private:
        lines_out.append(PRIVATE_NOTE)

    return ("\n".join(lines_out), tuple(new_entries))


def run_tool_loop(
    send: Any,
    messages: list[dict],
    *,
    authorizer: ToolReadAuthorizer,
    budget: ReadBudget,
    surface: str,
    reply_id: str,
    entries_so_far: tuple[ManifestEntry, ...] = ()
) -> LoopOutcome:
    msgs = list(messages)  # copy
    max_rounds = HARD_MAX_READS + 1
    entries = list(entries_so_far)

    # Count existing r-entries in entries_so_far
    next_index = sum(1 for e in entries if e.index.startswith("r"))

    rounds = 0
    while rounds < max_rounds:
        # Once the read budget is spent, stop offering the tool: the model answers instead of burning rounds (review N4).
        if budget.remaining <= 0:
            break
        rounds += 1
        content, tool_calls, assistant_msg = send(msgs, [tool_schema()])
        if not tool_calls:
            return LoopOutcome(content or "", tuple(entries), budget.used, rounds)

        # Append assistant message
        if assistant_msg is not None:
            msgs.append(assistant_msg)
        else:
            # Build a generic assistant message with tool_calls
            built_tool_calls = []
            for c in tool_calls:
                built_tool_calls.append({
                    "id": c.call_id,
                    "type": "function",
                    "function": {
                        "name": c.name,
                        "arguments": json.dumps(c.arguments or {})
                    }
                })
            msgs.append({
                "role": "assistant",
                "content": content or "",
                "tool_calls": built_tool_calls
            })

        # Process each tool call in order
        for call in tool_calls:
            if call.arguments is None:
                text = REFUSED_MALFORMED
            elif not budget.take():
                text = LIMIT_REACHED
            else:
                try:
                    req = ToolReadRequest(
                        query=call.arguments["query"],
                        surface=surface,
                        reply_id=reply_id,
                        read_number=budget.used
                    )
                    result = authorizer.authorize(req)
                except Exception:
                    text = "RECALL: refused (the read failed)."
                else:
                    text, new_entries = render_result(result, next_index)
                    entries.extend(new_entries)
                    next_index += len(new_entries)
            msgs.append(openai_tool_result_message(call.call_id, text))

    # One final send without tools (the round cap was hit, or the budget is spent)
    final_content, _, _ = send(msgs, None)
    return LoopOutcome(final_content or "", tuple(entries), budget.used, rounds + 1)


__all__ = [
    "TOOL_NAME",
    "OFF",
    "BOUNDED",
    "MODES",
    "HARD_MAX_READS",
    "DEFAULT_MAX_READS",
    "MAX_RECORDS_PER_READ",
    "MAX_QUERY_CHARS",
    "OUTWARD_RISK_CLASSES",
    "TOOL_DESCRIPTION",
    "REFUSED_MALFORMED",
    "LIMIT_REACHED",
    "NOTHING_FOUND",
    "PRIVATE_NOTE",
    "RESULT_HEADER",
    "ToolCall",
    "ToolReadRequest",
    "ManifestEntry",
    "ToolReadResult",
    "LoopOutcome",
    "ToolReadAuthorizer",
    "tool_schema",
    "to_anthropic_tool",
    "parse_openai_tool_calls",
    "parse_anthropic_tool_uses",
    "openai_tool_result_message",
    "anthropic_tool_result_block",
    "parse_mode",
    "clamp_max_reads",
    "ReadBudget",
    "outward_tools_present",
    "render_result",
    "run_tool_loop",
]
