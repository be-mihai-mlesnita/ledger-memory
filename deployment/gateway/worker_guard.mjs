/* Scope-gated Responses API transport. Never log headers, prompts or evidence. */
import fs from "node:fs";
import path from "node:path";
import crypto from "node:crypto";
import { fileURLToPath } from "node:url";

const ROOT = path.dirname(fileURLToPath(import.meta.url));
const MODEL = "gpt-6-luna";
const TOOLS = {
  facts: ["ledger_brief"],
  history: ["ledger_search_documents", "ledger_history", "ledger_beliefs_as_of"],
  both: ["ledger_brief", "ledger_search_documents", "ledger_history", "ledger_beliefs_as_of"],
  write: ["ledger_apply_approved_write"],
};
class GuardError extends Error {
  constructor(code) { super(code); this.code = code; }
}
const check = (condition, code) => { if (!condition) throw new GuardError(code); };
const same = (a, b) => JSON.stringify([...a].sort()) === JSON.stringify([...b].sort());
const sameArgs = (a, b) => same(Object.keys(a), Object.keys(b)) && Object.keys(a).every(key => a[key] === b[key]);

function secure(file, directory = false) {
  check(process.platform !== "win32", "unsupported_platform_acl");
  check(path.isAbsolute(file) && fs.realpathSync(file) === file, "unsafe_guard_path");
  const info = fs.lstatSync(file);
  check(!info.isSymbolicLink() && (directory ? info.isDirectory() : info.isFile())
    && info.uid === process.getuid() && !(info.mode & 0o077), "unsafe_guard_permissions");
}

function read(file) {
  secure(file);
  check(fs.statSync(file).size <= 2097152, "guard_state_limit");
  const fd = fs.openSync(file, fs.constants.O_RDONLY | fs.constants.O_NOFOLLOW);
  try { return parse(fs.readFileSync(fd, "utf8")); }
  finally { fs.closeSync(fd); }
}

function save(file, value) {
  secure(path.dirname(file), true);
  try { fs.lstatSync(file); secure(file); }
  catch (error) { if (error.code !== "ENOENT") throw error; }
  const temp = file + "." + crypto.randomBytes(12).toString("hex");
  const fd = fs.openSync(temp, fs.constants.O_WRONLY | fs.constants.O_CREAT | fs.constants.O_EXCL | fs.constants.O_NOFOLLOW, 0o600);
  try { fs.writeFileSync(fd, JSON.stringify(value)); fs.fsyncSync(fd); }
  finally { fs.closeSync(fd); }
  fs.renameSync(temp, file);
}

function parse(text) {
  try { return JSON.parse(text); }
  catch { throw new GuardError("invalid_guard_json"); }
}

function remove(file) {
  try { secure(file); fs.unlinkSync(file); }
  catch (error) { if (error.code !== "ENOENT") throw error; }
}

function argumentsAllowed(scope, name, args) {
  check(args && typeof args === "object" && !Array.isArray(args), "invalid_tool_arguments");
  const keys = Object.keys(args);
  if (name === "ledger_brief") {
    check(keys.length === 0, "invalid_tool_arguments");
  } else if (name === "ledger_search_documents") {
    check(same(keys, ["query"]) && typeof args.query === "string" && args.query.trim().length > 0
      && args.query.length <= 600, "invalid_tool_arguments");
  } else if (name === "ledger_history") {
    check(same(keys, ["fact_id"]) && Number.isInteger(args.fact_id) && args.fact_id > 0, "invalid_tool_arguments");
  } else if (name === "ledger_beliefs_as_of") {
    check(same(keys, ["when"]) && typeof args.when === "string" && args.when.length <= 80, "invalid_tool_arguments");
  } else if (name === "ledger_apply_approved_write") {
    check(same(keys, ["request_id"]) && args.request_id === scope.request_id, "invalid_tool_arguments");
  } else throw new GuardError("invalid_tool");
}

export const MemoryGatewayGuard = async () => {
  const settingsFile = process.env.LEDGER_GATEWAY_SETTINGS;
  check(typeof settingsFile === "string", "missing_guard_settings");
  const cfg = read(settingsFile);
  const state = cfg.state_dir;
  secure(state, true);
  const identity = read(path.join(state, "identity.json"));
  check(cfg.schema === 1 && cfg.model === "bemind-proxy/" + MODEL && identity.owner === cfg.owner
    && identity.store === cfg.store && identity.settings === settingsFile && identity.runtime === ROOT
    && identity.uid === process.getuid(), "guard_ownership_mismatch");
  const markerFile = path.join(state, "verified-model.json");
  const diagnostic = (error, nonce) => {
    if (!(error instanceof GuardError) || typeof nonce !== "string" || !/^[a-f0-9]{64}$/.test(nonce)) return;
    // Do not instrument save/check themselves: a failed diagnostic write must
    // neither recurse nor replace the original fail-closed validation error.
    try { save(path.join(state, "guard-last-error.json"), {code: error.code, nonce}); }
    catch {}
  };
  const observed = (operation, fallback) => async (...args) => {
    let nonce;
    try { nonce = read(path.join(state, "active-request.json"))?.nonce; }
    catch {}
    try { return await operation(...args); }
    catch (error) {
      diagnostic(error instanceof GuardError || !fallback ? error : new GuardError(fallback), nonce);
      throw error;
    }
  };
  let calls = null;
  let inFlight = false;
  let paramsNonce = null;
  let privatePrompts = {};
  const scopeNow = () => {
    const currentIdentity = read(path.join(state, "identity.json"));
    check(Object.keys(identity).every(key => currentIdentity[key] === identity[key]), "guard_ownership_mismatch");
    const scope = read(path.join(state, "active-request.json"));
    const mode = scope.kind === "write" ? "write" : scope.purpose;
    check(scope.schema === 1 && scope.owner === cfg.owner
      && (scope.kind === "write" && scope.purpose === null || scope.kind === "read" && ["facts", "history", "both"].includes(scope.purpose))
      && Object.hasOwn(TOOLS, mode) && scope.agent === "memory-luna-" + mode
      && typeof scope.nonce === "string" && /^[a-f0-9]{64}$/.test(scope.nonce)
      && typeof scope.session_id === "string" && /^[A-Za-z0-9_-]{1,160}$/.test(scope.session_id)
      && typeof scope.request_id === "string" && scope.request_id.length <= 128
      && Number.isFinite(scope.expires_at) && scope.expires_at > Date.now() / 1000
      && scope.max_calls === (["facts", "write"].includes(mode) ? 1 : 2)
      && scope.budget_tokens === 800 && scope.document_limit === 2, "invalid_guard_scope");
    if (!calls || calls.nonce !== scope.nonce) {
      check(!inFlight, "concurrent_scope");
      calls = {nonce: scope.nonce, turns: 0, tools: 0, brief: 0, history: 0, dispatched: 0, approved: []};
      paramsNonce = null;
      remove(markerFile);
    }
    return {scope, mode};
  };
  const original = globalThis.fetch;
  globalThis.fetch = observed(async (input, init) => {
    const url = new URL(typeof input === "string" ? input : input instanceof URL ? input.href : input.url);
    if (!url.pathname.endsWith("/responses")) {
      check(!/\/(chat\/completions|completions|messages|generateContent|streamGenerateContent)(?:$|\/)/.test(url.pathname), "alternate_inference_refused");
      return original(input, init);
    }
    check(url.pathname.endsWith("/codex/v1/responses"), "unexpected_responses_endpoint");
    const {scope, mode} = scopeNow();
    check(paramsNonce === scope.nonce && !inFlight, "unapproved_wire_context");
    const headers = new Headers(init?.headers ?? input?.headers);
    check(headers.get("x-session-affinity") === scope.session_id, "wire_session_mismatch");
    check(typeof init?.body === "string" && init.body.length <= 131072, "wire_body_limit");
    const body = parse(init.body);
    check(body.model === MODEL && Number.isInteger(body.max_output_tokens) && body.max_output_tokens > 0
      && body.max_output_tokens <= 1024 && body.parallel_tool_calls === false, "wire_model_or_cap");
    check(Array.isArray(body.tools) && body.tools.every(tool => tool.type === "function" && typeof tool.name === "string")
      && same(body.tools.map(tool => tool.name), TOOLS[mode]), "wire_tool_set_mismatch");
    check(calls.turns < (["facts", "write"].includes(mode) ? 2 : 3), "wire_turn_limit");
    calls.turns++;
    inFlight = true;
    remove(markerFile);
    try {
      const response = await original(input, {...init, redirect: "error"});
      check(response.ok, "model_http_error");
      const reader = response.body.getReader();
      const chunks = [];
      let size = 0;
      try {
        while (true) {
          const {value, done} = await reader.read();
          if (done) break;
          size += value.byteLength;
          check(size <= 2097152, "model_response_limit");
          chunks.push(Buffer.from(value));
        }
      } catch (error) { await reader.cancel(); throw error; }
      const bytes = Buffer.concat(chunks);
      const text = bytes.toString("utf8");
      let terminal;
      let events = [];
      if (response.headers.get("content-type")?.includes("text/event-stream")) {
        for (const block of text.split(/\r?\n\r?\n/)) {
          const data = block.split(/\r?\n/).filter(line => line.startsWith("data:")).map(line => line.slice(5).trimStart()).join("\n");
          if (!data || data === "[DONE]") continue;
          const event = parse(data);
          events.push(event);
          if (event.response?.model !== undefined) check(event.response.model === MODEL, "reported_model_mismatch");
          check(!["response.failed", "response.incomplete", "error"].includes(event.type), "model_terminal_error");
          if (event.type === "response.completed") {
            check(!terminal, "duplicate_terminal");
            terminal = event.response;
          }
        }
      } else terminal = parse(text);
      check(terminal && terminal.model === MODEL && terminal.status === "completed" && Array.isArray(terminal.output), "reported_model_mismatch");
      check(terminal.output.every(item => ["message", "reasoning", "function_call"].includes(item.type)), "unexpected_response_tool");
      const functions = terminal.output.filter(item => item.type === "function_call");
      check(calls.tools + functions.length <= scope.max_calls, "tool_call_limit");
      for (const item of functions) {
        check(TOOLS[mode].includes(item.name) && typeof item.arguments === "string", "unapproved_response_tool");
        const args = parse(item.arguments);
        argumentsAllowed(scope, item.name, args);
        if (item.name === "ledger_brief") check(++calls.brief <= 1, "brief_call_limit");
        if (["ledger_search_documents", "ledger_history", "ledger_beliefs_as_of"].includes(item.name))
          check(++calls.history <= (mode === "both" ? 1 : 2), "history_call_limit");
      }
      // Streaming SDKs dispatch from added/delta events, not just terminal.output.
      // Check that every streamed function belongs to the validated terminal pool.
      const deltas = new Map();
      for (const event of events) {
        if (event.item) {
          check(["message", "reasoning", "function_call"].includes(event.item.type), "unexpected_response_tool");
          if (event.item.type === "function_call") {
            const item = functions.find(item => item.id === event.item.id && item.call_id === event.item.call_id);
            check(item && item.name === event.item.name, "stream_tool_mismatch");
            if (event.type === "response.output_item.done") check(item.arguments === event.item.arguments, "stream_tool_mismatch");
          }
        }
        if (event.type === "response.function_call_arguments.delta") {
          check(functions.some(item => item.id === event.item_id), "stream_tool_mismatch");
          deltas.set(event.item_id, (deltas.get(event.item_id) ?? "") + event.delta);
        }
        if (event.type === "response.function_call_arguments.done") {
          check(functions.some(item => item.id === event.item_id && item.arguments === event.arguments), "stream_tool_mismatch");
        }
      }
      for (const [id, args] of deltas) check(functions.some(item => item.id === id && item.arguments === args), "stream_tool_mismatch");
      const current = scopeNow().scope;
      check(current.nonce === scope.nonce && current.session_id === scope.session_id && current.request_id === scope.request_id, "expired_response_scope");
      calls.tools += functions.length;
      calls.approved.push(...functions.map(item => ({name: item.name, args: parse(item.arguments)})));
      save(markerFile, {schema: 1, owner: scope.owner, nonce: scope.nonce, session_id: scope.session_id,
                        request_id: scope.request_id, model: MODEL});
      const usage = {};
      for (const key of ["input_tokens", "output_tokens", "total_tokens"])
        if (Number.isFinite(terminal.usage?.[key]) && terminal.usage[key] >= 0) usage[key] = terminal.usage[key];
      save(path.join(state, "guard-calls.json"), {nonce: scope.nonce, turns: calls.turns, tools: calls.tools, usage});
      const responseHeaders = new Headers(response.headers);
      responseHeaders.delete("content-encoding");
      responseHeaders.delete("content-length");
      return new Response(bytes, {status: response.status, headers: responseHeaders});
    } catch (error) {
      diagnostic(error instanceof GuardError ? error : new GuardError("guard_inference_refused"), scope.nonce);
      remove(markerFile);
      // Never propagate a parser error that may contain provider/request content.
      throw new Error("guard_inference_refused");
    } finally { inFlight = false; }
  });
  return {
    config: observed(async (config) => {
      check(config.model === "bemind-proxy/" + MODEL && config.small_model === config.model
        && same(config.instructions ?? [], []) && config.share === "disabled", "unsafe_worker_config");
      for (const [name, value] of Object.entries(config.mcp ?? {})) if (name !== "ledger") value.enabled = false;
      for (const [name, value] of Object.entries(config.agent ?? {})) if (!Object.keys(TOOLS).some(mode => name === "memory-luna-" + mode)) value.disable = true;
      for (const mode of Object.keys(TOOLS)) {
        const agent = config.agent?.["memory-luna-" + mode];
        check(typeof agent?.prompt === "string" && agent.model === "bemind-proxy/" + MODEL, "unsafe_private_agent");
        privatePrompts[mode] = agent.prompt;
      }
      save(path.join(state, "guard-ready.json"), {owner: cfg.owner, runtime: ROOT, model: MODEL});
    }, "unsafe_worker_config"),
    "chat.params": observed(async (input, output) => {
      const {scope} = scopeNow();
      check(input.sessionID === scope.session_id && input.agent === scope.agent
        && input.model?.providerID === "bemind-proxy" && input.model?.id === MODEL
        && input.model?.api?.id === MODEL, "unapproved_inference_context");
      paramsNonce = scope.nonce;
      output.maxOutputTokens = 1024;
      output.options = {...output.options, reasoningEffort: "low", textVerbosity: "low", parallelToolCalls: false};
    }, "unapproved_inference_context"),
    "tool.execute.before": observed(async (input, output) => {
      const {scope, mode} = scopeNow();
      const marker = read(markerFile);
      check(input.sessionID === scope.session_id && TOOLS[mode].includes(input.tool)
        && ["owner", "nonce", "session_id", "request_id"].every(key => marker[key] === scope[key])
        && marker.model === MODEL && calls.dispatched < scope.max_calls, "unverified_tool_dispatch");
      argumentsAllowed(scope, input.tool, output.args);
      const approved = calls.approved.findIndex(item => item.name === input.tool && sameArgs(item.args, output.args));
      check(approved >= 0, "unapproved_tool_dispatch");
      calls.approved.splice(approved, 1);
      calls.dispatched++;
    }, "unverified_tool_dispatch"),
    "experimental.chat.system.transform": observed(async (input, output) => {
      const {scope, mode} = scopeNow();
      check(input.sessionID === scope.session_id, "unapproved_system_context");
      check(typeof privatePrompts[mode] === "string", "missing_private_prompt");
      output.system = [privatePrompts[mode]];
    }, "unapproved_system_context"),
  };
};
