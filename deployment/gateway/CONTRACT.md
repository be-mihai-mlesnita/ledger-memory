# Luna Gateway Internal Contract

Implementation contract, not a model prompt. Schema version 1. Public MCP name `memory`;
public functions `read` and `write`, exposed to BeMind as `memory_read`/`memory_write`.

## Settings And Entry Point

Installed entry: `<runtime>/gateway/memory_gateway.py` using `<runtime>/.venv/bin/python`.
Commands: `serve`, `preflight`, `status`, `stop`, `enable`, each with `--settings PATH`.
`serve` is stdio MCP. `preflight` must make no model call. `stop` blocks new requests and
stops only its owned worker, refusing an active request. `enable` re-enables new requests.
An idle worker may stop automatically; a later request may restart it when enabled.

Settings JSON fields:

```json
{
  "schema": 1,
  "owner": "installation-uuid",
  "store": "/absolute/existing-or-new-memory-home",
  "ledger_python": "/absolute/runtime/.venv/bin/python",
  "bemind_command": ["/absolute/bemind"],
  "bemind_data_home": "/absolute/user-XDG-data-home",
  "state_dir": "/absolute/private/gateway-state",
  "model": "bemind-proxy/gpt-6-luna",
  "enabled": true,
  "request_timeout_seconds": 180,
  "idle_seconds": 300,
  "read_result_chars": 6000,
  "cache_ttl_seconds": 60
}
```

No authentication secret is stored in settings. Runtime control secrets are separately
owner-private. Existing BeMind authentication is used through the installed client's
normal path; do not copy its tokens. Config/cache/session DB of the private worker are
isolated; its sessions must not be imported into memory. No project-specific paths or
fixed user names in packaged code.

## Public Operations

- `read(question, project="global", purpose="facts")`, purposes `facts`, `history`, `both`.
- `write(operation, payload, request_id)`, operations `remember`, `revise`, `dispute`,
  `record_decision`, `link_records`, `create_handover`, `ingest_document`.
- A write is exact caller-supplied data, not a free-form instruction. Unknown fields,
  filesystem ingestion, invalid types and obvious credentials are rejected before a
  pending request is stored. Do not change caller attribution or allow Luna to change
  operation/arguments. `ingest_document` permits inline text/URI only, never `path`.
- The public read tool cannot write. Read-only/plan restrictions are enforced by
  separate tool permissions and private operation scopes, not by natural-language labels.

## Request Scope

All frontends sharing settings serialize whole requests with a cross-process request
lock. Only one private BeMind worker is active for that settings/store. Never use a
generic Task subagent or load normal user/project AGENTS/skills into the worker.

Before model inference, the controller writes owner-private `active-request.json`:

```json
{
  "schema": 1,
  "owner": "installation-uuid",
  "request_id": "stable-write-id-or-generated-read-id",
  "nonce": "random-per-attempt",
  "kind": "read",
  "purpose": "facts",
  "project": "global",
  "query": "global: exact caller question",
  "store_identity": {"device": 1, "inode": 1},
  "session_id": "private-client-session-id",
  "agent": "memory-luna-facts",
  "expires_at": 0,
  "max_calls": 1,
  "budget_tokens": 800,
  "document_limit": 2,
  "operation": null,
  "arguments": null,
  "payload_hash": null
}
```

Write scope has kind `write`, agent `memory-luna-write`, max_calls 1, a validated
operation/arguments/payload_hash, and no query. Only `ledger_apply_approved_write` is
exposed for that mode; Luna submits the request ID, not a copy of the payload.

The worker plugin pins both provider and model, enforces caps and tool exposure on the
actual outgoing request, buffers/validates the reported response model before allowing
tool execution, and writes `verified-model.json` containing matching owner, nonce,
session_id, request_id and model `gpt-6-luna`. No fallback or other model is allowed.
This cannot prove the honesty of an upstream provider or isolate against the same OS user.

Private backend `ledger_backend.py` exposes:

- `brief()`; facts/both. The backend supplies the scope query and 800-token budget.
- `search_documents(query)`; history/both. Only a bounded keyword subset of the approved
  question/project is permitted; the host fixes the result limit at two. Explicit
  project terms must remain present. No new topic, SQL wildcard or filesystem scope.
- `history(fact_id)` and `beliefs_as_of(when)`; history/both, within the same call budget.
- `apply_approved_write(request_id)`; writes only, exact scope ID and validated payload.

Backend entry: `python ledger_backend.py --settings PATH`. It imports no live store on
startup; use the explicit settings store only after a scope and verified model marker
are validated. Use actual ledger package APIs. Backend execution is serialized with a
separate cross-process operation lock; recheck expiry/scope before touching the store.

## Captured Evidence

`request_artifact(state, request_id)` is defined by `gateway_backend_contract.py` as
`state / "requests" / (sha256(request_id) + ".json")`. The backend updates:

```json
{
  "schema": 1,
  "nonce": "current-scope-nonce",
  "calls": 1,
  "pool": {"fact:2": {"ref": "fact:2", "type": "fact", "id": 2, "statement": "...", "source": "observed", "known_at": "...", "status": "active"}},
  "warnings": [],
  "outcomes": [{"tool": "brief", "ok": true}]
}
```

Read output from Luna is only `{"refs":["fact:2"],"missing":false}`. Controller validates
refs against the captured pool and hydrates canonical evidence itself; Luna cannot
invent or rewrite evidence. Parent receives bounded canonical evidence plus mandatory
warnings. Over-budget items are omitted with an explicit incomplete warning, not cut
mid-statement. Remove raw request artifacts after completion. No raw tool-result logs.

Caching is optional only with a reliable authoritative knowledge fingerprint that also
detects external changes/restores; never rely on a timestamp or this gateway's writes
alone. If a safe fingerprint is unavailable, disable caching explicitly. Do not cache a
result if knowledge changes during retrieval. Identical queued requests may reuse a valid
cache result. Never treat cached evidence as a fresh model or ledger call.

## Durable Write Receipts

`receipt_path(state, request_id)` is `state / "receipts" / (sha256(request_id) + ".json")`.
Controller/backend share `gateway_backend_contract.py` helpers for validation and paths.
Receipts include schema, request_id, operation, payload_hash, state and compact result.

- Controller creates `queued` before invoking Luna. Same ID/different hash is rejected.
- Backend persists `executing` before calling the actual API, using a per-request actor.
- Backend persists `committed` plus canonical compact result only after the API returns.
- A lost reply after committed reuses the receipt without another model/write call.
- The whole request holds rc3's shared store-session lease, not just each tool call.
  Scope and receipts carry the database device/inode identity; a replacement/restore
  invalidates old receipts for retry/acknowledgment until reconciled.
- `executing`/`indeterminate` is NEVER automatically retried. Report the uncertainty and
  preserve the receipt for operator reconciliation; never claim failure means no write.
- No core-schema changes, raw SQL mutations or blind dual-database transaction claims.
- If no mutation was attempted and session termination is proved, queued can be safely
  retried. If termination cannot be proved, quarantine the worker/gateway rather than
  accept a new scope. Expire/invalidate scope before releasing the request lock.
- Write receipts survive uninstall; memory, backups and shared model files always survive.

## Backend Shared Helpers

`gateway_backend_contract.py` will provide:

- `validate_write(operation, payload) -> dict` canonical defaults/arguments or `ValueError`.
- `write_hash(operation, arguments) -> str`.
- `request_artifact(state: Path, request_id: str) -> Path`.
- `receipt_path(state: Path, request_id: str) -> Path`.
- `load_json(path, default=None)` / `save_json(path, value)` owner-private atomic I/O.
- `knowledge_fingerprint(store: Path) -> str | None`; return None rather than unsafe cache keys.
- `store_session(store: Path)` holds the actual rc3 shared session lock.
- `store_identity(store: Path)` returns the protected database's device/inode metadata.

Unknown inputs must fail closed; logs contain only safe IDs/codes/counts, not payloads,
queries, retrieved text or credentials. Keep installation and runtime ownership explicit.
