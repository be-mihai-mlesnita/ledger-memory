"""The MCP server, exercised exactly as an agent's client launches it: a subprocess over stdio."""
from __future__ import annotations

import io
import json
import os
import sys

import anyio
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

TOOLS = {"remember", "recall", "revise", "dispute", "history", "beliefs_as_of", "current_beliefs",
         "record_decision", "link_records", "create_handover", "ingest_document", "search_documents",
         "memory_status",
         "brief", "recall_more"}
READ_ONLY = {"recall", "history", "beliefs_as_of", "current_beliefs", "search_documents", "memory_status",
             "brief", "recall_more"}


def _session(home, cwd, fn):
    env = {k: v for k, v in os.environ.items() if not k.startswith("LEDGER_")}
    env.update(LEDGER_HOME=str(home), LEDGER_AGENT_NAME="test-agent")
    params = StdioServerParameters(command=sys.executable, args=["-m", "ledger_memory.mcp_server"], env=env, cwd=str(cwd))
    errlog = open(cwd.parent / "server-stderr.log", "w")

    async def go():
        async with stdio_client(params, errlog=errlog) as (r, w):
            async with ClientSession(r, w) as s:
                init = await s.initialize()
                return await fn(s, init)
    try:
        return anyio.run(go)
    finally:
        errlog.close()


def _payload(res):
    if res.structured_content is not None:
        sc = res.structured_content
        return sc.get("result", sc) if isinstance(sc, dict) and set(sc) == {"result"} else sc
    return json.loads(res.content[0].text)


def test_handshake_tools_and_safety_annotations(tmp_path):
    cwd = tmp_path / "work"; cwd.mkdir()

    async def fn(s, init):
        return init, (await s.list_tools()).tools
    init, tools = _session(tmp_path / "home", cwd, fn)
    assert init.server_info.name == "ledger-memory"
    assert "recall" in (init.instructions or "")
    names = {t.name for t in tools}
    assert names == TOOLS
    assert not any(w in n for n in names for w in ("delete", "remove", "update", "edit", "purge", "drop"))
    assert {t.name for t in tools if t.annotations and t.annotations.read_only_hint} == READ_ONLY
    assert not any(t.annotations and t.annotations.destructive_hint for t in tools)


def test_round_trip_refusal_and_no_cwd_leak(tmp_path):
    cwd = tmp_path / "work"; cwd.mkdir()

    async def fn(s, init):
        f = _payload(await s.call_tool("remember", {"statement": "The staging DB lives on host db-2", "source": "observed", "kind": "system"}))
        rec = _payload(await s.call_tool("recall", {"query": "staging DB host"}))
        bad = await s.call_tool("revise", {"fact_id": f["id"], "statement": "db-3", "reason": ""})
        rev = _payload(await s.call_tool("revise", {"fact_id": f["id"], "statement": "The staging DB lives on host db-3", "reason": "migrated"}))
        st = _payload(await s.call_tool("memory_status", {}))
        dangling = await s.call_tool("link_records", {"source_type": "fact", "source_id": f["id"], "target_type": "fact",
                                                      "target_id": 999, "relationship": "depends_on"})
        return f, rec, bad, rev, st, dangling
    f, rec, bad, rev, st, dangling = _session(tmp_path / "home", cwd, fn)
    assert dangling.is_error and "no fact 999" in dangling.content[0].text   # the agent sees WHY, so it can fix it
    assert f["source"] == "observed" and f["current"]
    assert rec["results"] and rec["results"][0]["id"] == f["id"] and rec["index"]["fulltext_fresh"] is True
    assert bad.is_error and "a revision must say why (reason is required)" in bad.content[0].text
    assert rev["now"]["current"] and rev["revised"] == f["id"]
    assert st["facts_current"] == 1 and st["facts_superseded"] == 1
    assert list(cwd.iterdir()) == []                          # the server wrote nothing into its working directory
    assert (tmp_path / "home" / "memory.db").exists()
    assert "Traceback" not in (tmp_path / "server-stderr.log").read_text()


def test_brief_and_recall_more_over_stdio_and_the_reader_token_stays_private(tmp_path):
    cwd = tmp_path / "work"; cwd.mkdir()

    async def fn(s, init):
        await s.call_tool("remember", {"statement": "Changed the NAS admin password", "source": "user_said"})
        br = _payload(await s.call_tool("brief", {"question": "NAS password"}))
        more = [_payload(await s.call_tool("recall_more", {"query": "NAS password"})) for _ in range(4)]
        bad = _payload(await s.call_tool("recall_more", {"query": "  "}))
        return br, more, bad
    br, more, bad = _session(tmp_path / "home", cwd, fn)
    assert br["facts"] and br["facts"][0]["statement"].endswith("Changed the NAS admin password") or "NAS admin password" in br["facts"][0]["statement"]
    assert "read limit reached" in more[3]["result"] and "[r0]" in more[0]["result"]
    assert "malformed" in bad["result"]
    token = (tmp_path / "home" / "reader.token").read_text().strip()
    assert token and token not in json.dumps([br, more, bad]) and token not in (tmp_path / "server-stderr.log").read_text()
