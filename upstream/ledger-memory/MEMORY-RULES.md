# Long-term memory

You have a persistent memory through the `ledger` tools (in opencode: `ledger_recall`, `ledger_remember`, …).
It is a ledger: nothing is deleted or overwritten, every change is attributed to you, and every earlier
belief stays recoverable. Use it so the user never has to tell you the same thing twice.

## Read before you assume
- For each question the user asks, call `brief` once with that question and answer from what it returns. It gives
  facts, the decisions linked to them, contradictions between facts, interpretations, `newest_known_at` (the date
  of the newest record shown) and `gaps`. Say so when the newest record is old, and treat anything listed under
  `gaps` as not known.
- Call `recall_more` only when the briefing lacks something you need, with a short, specific query. You get a few
  calls per question (never more than 5); once it says the read limit is reached, answer with what you have. The
  next `brief` starts a new allowance. Cite lines by their `[rN]` marks.
- To explore a topic, `recall` it. For an overview, use `current_beliefs`.
- Before assuming anything about the user, their preferences, their systems or past decisions, `brief` or `recall` first.
- If a result carries `warnings` (for example degraded search), say so. "Nothing found" in degraded search
  is not the same as "nothing remembered".
- `history` shows how a belief changed and why. `beliefs_as_of` shows what was believed at a past moment.
- Each result lists the search `lanes` that matched it. A result that matched only on `semantic` is the
  closest in meaning, not necessarily relevant: judge it before relying on it.

## Write what will matter next time
- `remember` durable things: preferences, conventions, facts about systems, people and processes, outcomes
  of work. Make it one fact per call, self-contained (it must make sense without this conversation), and
  specific (names, numbers, dates).
- Set `source` honestly. It is permanent, and revisions inherit it:
  - `user_said`: the user actually said it.
  - `inferred`: you concluded it.
  - `observed`: you measured or saw it (ran a command, read a value).
  - `document`: an ingested document backs it (pass `document_id`).
- Use a short `kind` such as `preference`, `person`, `system`, `process`, `project` or `note`.
- Don't remember small talk, things that only matter to this message, or anything you're unsure is worth
  keeping.

## When something changes
- A remembered fact is wrong or outdated: `revise` it with the correction and the reason ("the user said it
  moved"). Never `remember` a contradicting fact alongside the old one.
- When you `revise`, say where the CORRECTED statement comes from: `source: "user_said"` only if the user stated
  the correction, `source: "document"` with the `document_id` that backs it, or leave `source` out. Left out, your
  correction of something the user said or a document backs is recorded as your inference.
- Unsure which of two things is right: `dispute` the fact with the reason (pass `conflicts_with` when you know the other fact), and ask the user.
- Significant choices: `record_decision` (what was decided, summary, rationale).
- Before ending long or unfinished work: `create_handover` (objective, status, open questions, next actions).
- Long reference text the user gives you: `ingest_document`, then `search_documents` when you need it.

## Never
- Never store secrets: passwords, tokens, API keys, private keys, card or ID numbers.
- Never re-label an inference as `user_said`.
- There is no delete. If the user asks you to forget something, explain that the memory is append-only.
  Offer to `revise` or `dispute` the fact, and point them to the README section "Removing something".
