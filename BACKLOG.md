# Project backlog

Deferred proposals only. Entries are not implementation authorization.

## LM-B001: Owner-controlled forgetting and redaction

- Recorded: 2026-10-23.
- Status: deferred by owner; design discussion only.
- Origin: work-side review of ledger-memory 0.3.0-rc1 and the owner's request to retain this proposal for later recall.
- Recall terms: forgetting, forget, deletion, redaction, sensitive memory, backup resurrection, right to forget.
- Scope: deferred design only; do not implement without a new instruction.

### Problem

Append-only history is useful for correction and accountability, but accidental storage of sensitive content needs an explicit owner-controlled removal path. Revising, disputing or hiding a record does not remove it. Current documented options are reverting to an earlier backup or starting a fresh store, both of which can discard unrelated information.

### Proposed semantics

| Action | Meaning |
| --- | --- |
| Revise | Correct a belief while retaining earlier versions and evidence. |
| Archive | Exclude it from ordinary recall while retaining recoverable history; this is not deletion. |
| Forget | Remove selected content and its managed copies, including the selected revision history. |

Archive and forget are proposed capabilities, not claims about existing tools. Forgetting would be an explicit exception to the application's append-only policy.

### Proposed workflow

1. **Build a precise preview.** Select whole records and their revision chains first. Traverse explicit dependencies into evidence, documents/chunks, handovers, decisions, summaries, events/audit payloads, derived indexes and managed backups. Show the exact affected scope and effects on retained records. Present suspected paraphrases or duplicates as review candidates, not automatic deletions. Preserve shared documents/evidence unless explicitly selected; their retention may mean the requested information is not fully forgotten.

2. **Obtain separate owner approval.** Bind confirmation to the exact preview and store version. Reconfirm if the scope or data changes. Do not expose a destructive forgetting MCP tool to the agent. Use an owner-controlled interface, recognizing that a CLI flag alone is not a security boundary against an agent with equivalent OS access. Decide the actual enforcement boundary before implementation.

3. **Retain only a content-free receipt.** Keep an opaque operation/record identifier, time, counts, and a generic owner-requested removal event. Do not retain original text, revealing titles, free-text explanations containing the content, or content fingerprints. The minimal receipt itself remains metadata and must be disclosed as retained.

4. **Remove managed representations under maintenance.** Stop concurrent sessions and indexing, prevent new access, and prevent pending/in-flight embedding jobs from republishing removed content. Prefer a validated clean-store rebuild from retained records for the first design, with referential-integrity checks and rebuilt keyword/vector indexes. Account for WAL/SHM files, temporary databases, exports, logs, model-worker state and old derived-index copies. This is logical removal from managed artifacts, not a promise of forensic erasure from physical storage.

5. **Resolve backups and crash recovery explicitly.** Preview managed backups containing affected content; recommend removing or sanitizing them only with explicit approval because recovery history will be lost. Do not silently create a permanent pre-deletion backup. Track any necessary transient recovery copies until cleanup is complete. Consider a minimal redaction registry outside normal database rollback to stop old managed backups resurrecting content; require sanitization or refuse restore before exposing the store. Define behavior if the registry is missing and for manual imports/new stores. This mechanism cannot control arbitrary external copies.

6. **Report completion by boundary.** Separately report live-store removal, derived-artifact cleanup, managed-backup cleanup, and unresolved/external copies. Existing conversations, source documents, exports, cloud/filesystem snapshots and external backups are not erased by a ledger-only operation. Require a fresh agent session and explicitly address re-ingestion sources to reduce accidental reintroduction. Do not claim permanent forgetting while relevant managed content or recovery copies remain.

### Acceptance criteria for a future implementation

- A selected synthetic fact and its selected revision history cannot be found via recall, history, document search, direct retained-table inspection, or rebuilt indexes.
- Explicitly selected dependent content is removed/redacted without silently losing unrelated or shared records.
- Pending/in-flight jobs cannot restore forgotten content, including across worker crashes and restart.
- Restoring an older managed backup either sanitizes the selected records before access or refuses safely.
- No deleted content survives in managed temporary/recovery artifacts after reported completion; incomplete cleanup is reported as incomplete, not success.
- Unrelated records, provenance and database integrity remain intact.
- Approval is invalidated when the preview scope changes.
- Reports disclose retained metadata and external-copy limitations.

### Decisions to revisit

- Whole-record and revision-chain removal first; sentence-level document redaction later, if needed.
- Which dependencies require removal versus explicit owner-reviewed rewriting, and how shared evidence is handled.
- Whether and how managed backups are sanitized or removed, and the approved recovery tradeoff.
- The maintenance/crash-recovery protocol, redaction-registry trust model and behavior when it is missing.
- How an owner confirmation is isolated from an agent with the same OS permissions.
- How re-ingestion and already-loaded conversation context are handled without retaining revealing content signatures.

Next action: revisit only when requested; complete the design and validation plan before implementation.
