"""Run the real JS plugin against an in-memory fetch, never a model endpoint."""
import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest

from test_gateway_controller import gateway, layout

pytestmark = pytest.mark.skipif(os.name != "posix" or shutil.which("node") is None, reason="Requires local POSIX Node, not Windows validation")

HARNESS = r'''
import fs from 'node:fs';
import path from 'node:path';
import {pathToFileURL} from 'node:url';
const cfg = JSON.parse(fs.readFileSync(process.env.LEDGER_GATEWAY_SETTINGS, 'utf8'));
const root = process.env.TEST_GATEWAY_ROOT;
const state = cfg.state_dir;
const scenario = process.env.TEST_SCENARIO;
const mode = process.env.TEST_MODE ?? 'facts';
const names = {facts:['ledger_brief'], history:['ledger_search_documents','ledger_history','ledger_beliefs_as_of'],
  both:['ledger_brief','ledger_search_documents','ledger_history','ledger_beliefs_as_of'],write:['ledger_apply_approved_write']};
const write = (name, value) => fs.writeFileSync(path.join(state,name),JSON.stringify(value),{mode:0o600});
const scope = {schema:1,owner:cfg.owner,nonce:'a'.repeat(64),session_id:'ses_test',request_id:'request-test',
  kind:mode==='write'?'write':'read',purpose:mode==='write'?null:mode,agent:'memory-luna-'+mode,
  query:'global: exact',project:mode==='write'?null:'global',expires_at:Date.now()/1000+60,max_calls:['facts','write'].includes(mode)?1:2,
  budget_tokens:800,document_limit:2};
if(scenario==='expired-scope')scope.expires_at=0;
if(scenario==='invalid-nonce')scope.nonce='PRIVATE_NOT_A_REQUEST_NONCE';
write('active-request.json',scope);
const tool = process.env.TEST_TOOL_NAME ?? names[mode][0];
const args = process.env.TEST_TOOL_ARGS === undefined
  ? (mode==='write'?{request_id:scope.request_id}:tool==='ledger_search_documents'?{query:'exact'}:{})
  : JSON.parse(process.env.TEST_TOOL_ARGS);
let output = [{type:'function_call',id:'fc_1',call_id:'call_1',name:tool,arguments:JSON.stringify(args)}];
let actual=0;
globalThis.fetch=async () => {
  actual++;
  if(scenario==='provider-exception')throw new Error('PRIVATE_PROVIDER_BODY_AND_CREDENTIAL');
  if(scenario==='provider-code-spoof')throw new Error('reported_model_mismatch');
  if(scenario==='response-invalid-json')return new Response('{"PRIVATE_MODEL_TEXT":broken');
  const result={model:['response-model','diagnostic-write-failure'].includes(scenario)?'gpt-other':'gpt-6-luna',status:'completed',output,usage:{input_tokens:12,output_tokens:3,total_tokens:15}};
  if(scenario==='response-tools') result.output[0].name='shell';
  if(scenario==='response-too-many') result.output=[...output,...output,...output];
  if(scenario==='response-arguments') result.output[0].arguments=JSON.stringify({question:'DIFFERENT',budget_tokens:800});
  if(scenario==='stale-during-fetch') write('active-request.json',{...scope,nonce:'b'.repeat(64)});
  if(scenario==='expired-during-fetch') write('active-request.json',{...scope,expires_at:0});
  if(scenario==='stream-mismatch') {
    const stream=[{type:'response.output_item.added',item:{...result.output[0],name:'shell',arguments:''}},
      {type:'response.completed',response:result}].map(e=>'data: '+JSON.stringify(e)+'\n\n').join('');
    return new Response(stream,{headers:{'content-type':'text/event-stream'}});
  }
  if(scenario==='stream-model' || scenario==='valid-stream') {
    const stream=[{type:'response.created',response:{model:scenario==='stream-model'?'other':'gpt-6-luna'}},
      {type:'response.output_item.added',item:{...result.output[0],arguments:''}},
      {type:'response.function_call_arguments.delta',item_id:'fc_1',delta:result.output[0].arguments},
      {type:'response.function_call_arguments.done',item_id:'fc_1',arguments:result.output[0].arguments},
      {type:'response.output_item.done',item:result.output[0]},
      {type:'response.completed',response:result}].map(e=>'data: '+JSON.stringify(e)+'\n\n').join('');
    return new Response(stream,{headers:{'content-type':'text/event-stream'}});
  }
  return new Response(JSON.stringify(result),{headers:{'content-type':'application/json'}});
};
const {MemoryGatewayGuard}=await import(pathToFileURL(path.join(root,'worker_guard.mjs')));
let error=null, released=false, dispatched=false;
try {
  const plugin=await MemoryGatewayGuard();
  if(scenario==='diagnostic-write-failure')fs.mkdirSync(path.join(state,'guard-last-error.json'),{mode:0o700});
  await plugin.config({model:'bemind-proxy/gpt-6-luna',small_model:'bemind-proxy/gpt-6-luna',instructions:[],share:'disabled',
    mcp:{ledger:{enabled:true},seeded:{enabled:true}},
    agent:Object.fromEntries(Object.keys(names).map(mode=>['memory-luna-'+mode,{prompt:'PRIVATE ONLY',model:'bemind-proxy/gpt-6-luna'}]))});
  const input={sessionID:scope.session_id,agent:scope.agent,model:{providerID:'bemind-proxy',id:'gpt-6-luna',api:{id:'gpt-6-luna'}}};
  if(scenario==='provider')input.model.providerID='other';
  if(scenario==='params-model')input.model.id='other';
  if(scenario==='api-model')input.model.api.id='other';
  if(scenario==='agent')input.agent='general';
  if(scenario==='session')input.sessionID='main';
  await plugin['chat.params'](input,{});
  const system={system:['NORMAL GLOBAL INSTRUCTIONS','OTHER PROJECT']};
  await plugin['experimental.chat.system.transform']({sessionID:scope.session_id},system);
  if(JSON.stringify(system.system)!=='["PRIVATE ONLY"]')throw Error('system not isolated');
  const body={model:'gpt-6-luna',max_output_tokens:1024,parallel_tool_calls:false,tools:names[mode].map(name=>({type:'function',name}))};
  if(scenario==='wire-model')body.model='other';
  if(scenario==='wire-cap')body.max_output_tokens=1025;
  if(scenario==='wire-missing-cap')delete body.max_output_tokens;
  if(scenario==='wire-tools')body.tools.push({type:'function',name:'task'});
  if(scenario==='wire-missing-tools')body.tools=[];
  if(scenario==='wire-parallel')body.parallel_tool_calls=true;
  const request=()=>fetch('https://mock.invalid/codex/v1/responses',{method:'POST',headers:{'x-session-affinity':scope.session_id},body:JSON.stringify(body)});
  await request(); released=true;
  if(scenario==='stale-dispatch')write('active-request.json',{...scope,nonce:'b'.repeat(64)});
  const dispatchArgs=process.env.TEST_DISPATCH_ARGS !== undefined ? JSON.parse(process.env.TEST_DISPATCH_ARGS)
    : scenario==='tool-arguments'?{...args,question:'PRIVATE_PROMPT'}:args;
  await plugin['tool.execute.before']({sessionID:scope.session_id,tool},{args:dispatchArgs}); dispatched=true;
  if(scenario==='duplicate-dispatch')await plugin['tool.execute.before']({sessionID:scope.session_id,tool},{args});
  if(scenario==='turn-limit') {
    output=[];
    for(let i=0;i<4;i++){await plugin['chat.params'](input,{});await request();}
  }
  if(scenario==='second-search') {
    const secondArgs={query:'exact'};
    output=[{...output[0],id:'fc_2',call_id:'call_2',arguments:JSON.stringify(secondArgs)}];
    await plugin['chat.params'](input,{});
    await request();
    await plugin['tool.execute.before']({sessionID:scope.session_id,tool},{args:secondArgs});
  }
  if(scenario==='new-scope') {
    Object.assign(scope,{nonce:'b'.repeat(64),session_id:'ses_second',request_id:'request-second'});
    write('active-request.json',scope);input.sessionID=scope.session_id;
    await plugin['chat.params'](input,{});
    if(fs.existsSync(path.join(state,'verified-model.json')))throw Error('stale marker remained');
    await request();
  }
} catch(e) {error=e.message;}
console.log(JSON.stringify({error,actual,released,dispatched,marker:fs.existsSync(path.join(state,'verified-model.json'))}));
'''


def run_guard(layout, scenario, mode="facts", *, tool_name=None, tool_args=None, dispatch_args=None):
    env = {**os.environ, "LEDGER_GATEWAY_SETTINGS": str(layout.path), "TEST_GATEWAY_ROOT": str(gateway.ROOT),
           "TEST_SCENARIO": scenario, "TEST_MODE": mode}
    for key, value in (("TEST_TOOL_NAME", tool_name), ("TEST_TOOL_ARGS", tool_args), ("TEST_DISPATCH_ARGS", dispatch_args)):
        if value is not None:
            env[key] = value
        else:
            env.pop(key, None)
    result = subprocess.run([shutil.which("node"), "--input-type=module", "-e", HARNESS],
        env=env, capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


@pytest.mark.parametrize("mode", ["facts", "history", "both", "write"])
def test_correct_scoped_model_and_tools(layout, mode):
    result = run_guard(layout, "valid", mode)
    assert result == {"error": None, "actual": 1, "released": True, "dispatched": True, "marker": True}
    saved = gateway.load(layout.state / "guard-calls.json")
    assert saved["turns"] == saved["tools"] == 1
    assert set(saved["usage"]) == {"input_tokens", "output_tokens", "total_tokens"}


@pytest.mark.parametrize("args", [None, [], "", {"question": "global: exact"}, {"query": "global: exact"},
                                 {"budget_tokens": 800}, {"limit": 2}, {"extra": False}])
def test_brief_still_rejects_all_supplied_arguments_before_sdk(layout, args):
    result = run_guard(layout, "valid", "facts", tool_args=json.dumps(args))
    assert result["error"] == "guard_inference_refused" and result["actual"] == 1
    assert not result["released"] and not result["dispatched"] and not result["marker"]
    assert gateway.load(layout.state / "guard-last-error.json")["code"] == "invalid_tool_arguments"


@pytest.mark.parametrize("args", [None, [], "exact", {}, {"query": None}, {"query": 7}, {"query": ""},
                                 {"query": " \t\n"}, {"query": "x" * 601}, {"query": "exact", "limit": 2},
                                 {"query": "exact", "budget_tokens": 800}, {"question": "exact"}])
def test_search_requires_only_a_nonblank_bounded_query(layout, args):
    result = run_guard(layout, "valid", "history", tool_args=json.dumps(args))
    assert result["error"] == "guard_inference_refused" and not result["released"] and not result["dispatched"]
    assert gateway.load(layout.state / "guard-last-error.json")["code"] == "invalid_tool_arguments"


@pytest.mark.parametrize("mode", ["history", "both"])
@pytest.mark.parametrize("query", ["exact", "x" * 600, "backend validates these project and topic terms"])
def test_search_shape_gate_leaves_term_and_project_validation_to_backend(layout, mode, query):
    result = run_guard(layout, "valid", mode, tool_name="ledger_search_documents", tool_args=json.dumps({"query": query}))
    assert result["error"] is None and result["released"] and result["dispatched"]


def test_history_allows_second_shorter_search_within_read_budget(layout):
    result = run_guard(layout, "second-search", "history", tool_args=json.dumps({"query": "global exact"}))
    assert result["error"] is None and result["actual"] == 2
    assert gateway.load(layout.state / "guard-calls.json")["tools"] == 2


def test_both_does_not_allow_second_history_search(layout):
    result = run_guard(layout, "second-search", "both", tool_name="ledger_search_documents", tool_args=json.dumps({"query": "exact"}))
    assert result["error"] == "guard_inference_refused"
    assert gateway.load(layout.state / "guard-last-error.json")["code"] == "history_call_limit"


@pytest.mark.parametrize("mode,args", [("facts", {"question": "global: exact", "budget_tokens": 800}),
                                      ("history", {"query": "global: exact", "limit": 2})])
def test_brief_and_search_also_reject_legacy_arguments_at_dispatch(layout, mode, args):
    result = run_guard(layout, "valid", mode, dispatch_args=json.dumps(args))
    assert result["released"] and not result["dispatched"]
    assert result["error"] == "invalid_tool_arguments"


@pytest.mark.parametrize("mode,tool,args,valid", [
    ("history", "ledger_history", {"fact_id": 7}, True),
    ("history", "ledger_history", {}, False),
    ("history", "ledger_history", {"fact_id": 7, "query": "global: exact"}, False),
    ("history", "ledger_beliefs_as_of", {"when": "2026-10-01"}, True),
    ("history", "ledger_beliefs_as_of", {}, False),
    ("history", "ledger_beliefs_as_of", {"when": "2026-10-01", "limit": 2}, False),
    ("write", "ledger_apply_approved_write", {"request_id": "request-test"}, True),
    ("write", "ledger_apply_approved_write", {}, False),
    ("write", "ledger_apply_approved_write", {"request_id": "other-request"}, False),
])
def test_history_asof_and_write_argument_contracts_unchanged(layout, mode, tool, args, valid):
    result = run_guard(layout, "valid", mode, tool_name=tool, tool_args=json.dumps(args))
    assert result["dispatched"] is valid and result["released"] is valid
    assert result["error"] == (None if valid else "guard_inference_refused")


@pytest.mark.parametrize("scenario", ["provider", "params-model", "api-model", "agent", "session", "wire-model",
    "wire-cap", "wire-missing-cap", "wire-tools", "wire-missing-tools", "wire-parallel"])
def test_outgoing_gate_rejects_before_fetch(layout, scenario):
    result = run_guard(layout, scenario)
    assert result["error"] and not result["released"] and not result["marker"] and result["actual"] == 0


@pytest.mark.parametrize("scenario", ["response-model", "response-tools", "response-too-many", "response-arguments",
    "stream-mismatch", "stream-model", "stale-during-fetch", "expired-during-fetch"])
def test_reported_model_and_tool_mismatch_never_released_to_sdk(layout, scenario):
    result = run_guard(layout, scenario)
    assert result["error"] == "guard_inference_refused" and result["actual"] == 1
    assert not result["released"] and not result["dispatched"] and not result["marker"]


def test_valid_buffered_stream(layout):
    result = run_guard(layout, "valid-stream")
    assert result["error"] is None and result["released"] and result["dispatched"]


@pytest.mark.parametrize("mode,turns", [("facts", 2), ("history", 3), ("both", 3), ("write", 2)])
def test_on_wire_turn_caps(layout, mode, turns):
    result = run_guard(layout, "turn-limit", mode)
    assert result["error"] == "wire_turn_limit" and result["actual"] == turns


def test_stale_scope_and_duplicate_tool_dispatch(layout):
    result = run_guard(layout, "stale-dispatch")
    assert result["error"] and not result["dispatched"] and not result["marker"]


def test_repeated_dispatch_is_rejected(layout):
    result = run_guard(layout, "duplicate-dispatch")
    assert result["dispatched"] and result["error"] == "unverified_tool_dispatch"


def test_new_scope_resets_caps_and_requires_new_marker(layout):
    result = run_guard(layout, "new-scope")
    assert result["error"] is None and result["actual"] == 2
    marker = gateway.load(layout.state / "verified-model.json")
    assert marker["session_id"] == "ses_second" and marker["nonce"] == "b" * 64


@pytest.mark.parametrize("scenario,code", [
    ("provider", "unapproved_inference_context"),
    ("expired-scope", "invalid_guard_scope"),
    ("wire-model", "wire_model_or_cap"),
    ("wire-tools", "wire_tool_set_mismatch"),
    ("turn-limit", "wire_turn_limit"),
    ("response-model", "reported_model_mismatch"),
    ("response-tools", "unapproved_response_tool"),
    ("response-too-many", "tool_call_limit"),
    ("stream-mismatch", "stream_tool_mismatch"),
    ("response-invalid-json", "invalid_guard_json"),
    ("tool-arguments", "invalid_tool_arguments"),
    ("duplicate-dispatch", "unverified_tool_dispatch"),
    ("provider-exception", "guard_inference_refused"),
    ("provider-code-spoof", "guard_inference_refused"),
])
def test_guard_diagnostic_contains_only_controlled_code_and_nonce(layout, scenario, code):
    result = run_guard(layout, scenario)
    assert result["error"]
    target = layout.state / "guard-last-error.json"
    assert gateway.load(target) == {"code": code, "nonce": "a" * 64}
    assert target.stat().st_mode & 0o777 == 0o600
    assert "PRIVATE_" not in target.read_text()
    assert code in gateway.GUARD_ERROR_CODES


def test_guard_diagnostic_keeps_original_inflight_nonce(layout):
    result = run_guard(layout, "stale-during-fetch")
    assert result["error"] == "guard_inference_refused"
    assert gateway.load(layout.state / "guard-last-error.json") == {"code": "concurrent_scope", "nonce": "a" * 64}


def test_guard_diagnostic_failure_does_not_recurse_or_change_refusal(layout):
    result = run_guard(layout, "diagnostic-write-failure")
    assert result["error"] == "guard_inference_refused"
    assert not result["released"] and not result["marker"]
    assert (layout.state / "guard-last-error.json").is_dir()


def test_guard_diagnostic_does_not_follow_symlinks(layout):
    outside = layout.path.parent / "diagnostic-sentinel"
    outside.write_text("UNCHANGED")
    outside.chmod(0o600)
    (layout.state / "guard-last-error.json").symlink_to(outside)
    result = run_guard(layout, "wire-model")
    assert result["error"] == "wire_model_or_cap" and result["actual"] == 0
    assert outside.read_text() == "UNCHANGED"


def test_guard_diagnostic_never_records_invalid_nonce(layout):
    result = run_guard(layout, "invalid-nonce")
    assert result["error"] == "invalid_guard_scope"
    assert not (layout.state / "guard-last-error.json").exists()


def test_successful_guard_does_not_create_error_diagnostic(layout):
    assert run_guard(layout, "valid")["error"] is None
    assert not (layout.state / "guard-last-error.json").exists()
