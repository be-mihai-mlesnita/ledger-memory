# ledger-memory

Long-term memory for AI agents that keeps its own history. One SQLite file per person, an MCP server any agent
can attach to, and a `ledger` command for the human. Built for opencode, and works with any MCP client.

## Why not just a memory file?

A notes file is fine until the first correction. Then the old belief is gone, nobody knows who changed it or
why, and a guess the agent wrote down months ago reads exactly like something you told it. This is a ledger
instead:

- **Corrections never erase.** `revise` replaces a belief with a new version and keeps the old one, along
  with who changed it, when, and why. `history` and `beliefs_as_of` answer "what did you believe last Tuesday,
  and what changed your mind?" Two sessions correcting the same fact at once cannot both win: one gets "already
  revised; revise that one"
- **Every fact says where it came from:** you said it, the agent inferred it, the agent observed it, or a
  document backs it. A correction says where the NEW statement comes from: the agent's correction of something you
  said, that a document backs, or that was observed is recorded as the agent's inference unless it says you stated
  it or cites a new document. A guess can't be promoted into "the user told me", and nobody's words are put in your
  mouth. Your own corrections (the `ledger` command) keep the original source unless you give another; a corrected
  document-backed fact becomes your statement, since the document no longer backs the new text. Saying "the user
  said it" is an honesty rule the agent follows, as with `remember`; the record shows which session wrote it.
- **There is no delete tool.** The agent can't lose or destroy memory, whether by mistake or because a web
  page told it to. This is a rule of the program, not of the file: the memory is an ordinary SQLite file that
  anyone with access to your account can change. It is not tamper-evident storage.
- **Search admits when it's degraded.** Keyword and full-text search always work. Meaning-based search is
  optional: it runs the bge-m3 model on your own CPU, in a small background service shared by all your
  sessions. Any degradation comes back as a warning or a status reason instead of a silent "nothing found".
- **Every write is checked first.** A refused write leaves no trace, every write is attributed (facts,
  revisions, disputes and decisions also get an audit entry), there's an integrity check, and backups are
  checksummed and verified before a restore; an empty or malformed checksum file is refused.
- **Stored locally.** The memory and its search indexes live in `~/.ledger-memory`, and meaning-based search runs on
  your own CPU. This program sends nothing anywhere, except the one-time model download you ask for
  (`ledger semantic enable`; official repository, pinned revision, every file checked) and the dependency download
  during installation. What the agent recalls does go into its conversation, so it reaches whatever model provider
  your agent client uses, like everything else in that conversation.

## Install

You need macOS or Linux and [uv](https://docs.astral.sh/uv/). The installer fetches Python 3.11+ itself if
needed. For Windows, see "Windows (release candidate)" below.

**Easiest:** open opencode and paste the contents of `START-PROMPT.md`. It walks the agent through the
steps below and reports back.

**By hand:**

```bash
./install.sh
```

Then restart opencode. The installer:

1. copies the program to `~/.local/share/ledger-memory` and installs the exact pinned dependencies (`uv.lock`),
   including the runtime for meaning-based search (no model is downloaded);
2. creates your memory at `~/.ledger-memory` (an existing memory is backed up first, then left as is);
3. runs `ledger selftest`: about 30 checks on a throwaway store, and the install stops if any fail
   (steps 1 and 3 happen in a new folder; your working install is switched over only after both pass);
4. links `ledger` into `~/.local/bin`;
5. adds the `ledger` MCP server and `MEMORY-RULES.md` to opencode's global config
   (`~/.config/opencode/opencode.json`, backed up first; existing settings are kept).

Re-running `./install.sh` from a newer copy is how you upgrade. Each version is installed and self-tested in its
own folder under `~/.local/share/ledger-memory-versions/`, and `~/.local/share/ledger-memory` (a link) switches to it
only when it passed. If a download or the self-test fails, for example behind a company proxy, your current version
keeps working and the script says so. Nothing is deleted: earlier versions stay in that folder until you remove them.
An install from before 0.3.0-rc2 (a plain folder) is moved there once as `legacy-<time>`.

## What the agent gets

In opencode, the tools appear as `ledger_<name>`.

| Tool | What it does | Writes? |
|---|---|---|
| `brief` | Call once per user question (budgets below 200 tokens are raised to 200; the whole returned payload stays within the budget): facts, decisions linked to them (no rationale), contradictions between shown facts, interpretations, the date of the newest record shown, and the gaps, within a token budget (default 1500). Also opens a new allowance of `recall_more` calls | no |
| `recall_more` | A short, bounded extra look-up when the briefing lacks something: at most 3 reads per question (configurable, never more than 5), numbered `[r0]`, `[r1]`, … After that it says "read limit reached" until the next `brief` | no |
| `recall` | Search facts, decisions, handovers and links, by keyword and by meaning | no |
| `current_beliefs` | The newest current facts, optionally of one kind | no |
| `remember` | Store one fact, with its source (`user_said`, `inferred`, `observed`, `document`) | yes |
| `revise` | Replace a fact with a corrected version (a reason is required; the old version is kept). Optional `source` says where the correction comes from (see "Every fact says where it came from") | yes |
| `dispute` | Flag a fact as contested, with a reason (and, optionally, the other fact it contradicts, so `brief` can show the pair) | yes |
| `history` | Every version of a fact: who changed it, when and why | no |
| `beliefs_as_of` | What was believed at a past moment, each belief with its source, when it became known, and whether it is still current | no |
| `record_decision` | A decision, with its summary and rationale | yes |
| `link_records` | Link two records (fact, decision, handover, document) | yes |
| `create_handover` | Bundle context for the next session or agent; returns Markdown | yes |
| `ingest_document` | Store a text document (paste the text, or give a file path: see the rule below) so its contents become searchable | yes |
| `search_documents` | Search inside stored documents. Without meaning-based search it ranks passages by how many of the question's words they contain (common words like "what" and "the" are ignored; words of one or two letters match whole words only), so a full question works as well as keywords. A full-text index over the passages keeps this fast as the store grows; a word matches whole or as the start of a longer word ("approval" finds "approvals"), not from the middle of one ("port" does not find "support") | no |
| `memory_status` | Counts, location, and whether search is healthy | no |
| `write_status` | Whether a write sent with a `request_id` was saved: `committed` (with the record's type and id) or `not_found` (no write was recorded under that id within the retention window; sending it again is safe) | no |

Read-only tools carry the MCP `readOnlyHint`. No tool is marked destructive, because none is. Bad input is
refused with a reason the agent can act on ("a revision must say why", "no fact 999"), before anything is
written. `MEMORY-RULES.md` tells the agent when to use each tool.

The token budget bounds `brief`, and the read allowance bounds `recall_more`. `recall`, `current_beliefs` and the
other read tools have their own `limit` instead; `MEMORY-RULES.md` asks the agent to start from `brief` and use them
sparingly. That is a rule the agent follows, not a cap the server enforces. When meaning-based search is turned on but
not ready, `brief` says so at the top of its `gaps`, so the agent knows the facts it got were found by keyword only.

## The `ledger` command

Writes made with `ledger` are attributed to you, the owner, not to the agent. Output is JSON; add `--plain`
before the command for readable search results.

| Command | |
|---|---|
| `ledger status` | counts, where the memory lives, index health |
| `ledger recall "query"` | search (`ledger --plain recall "query"` for one line per hit) |
| `ledger brief "question" [--budget 1500]` | the same briefing the agent gets |
| `ledger recall-more "query"` | one bounded extra look-up (each command is a fresh process, so it starts a fresh allowance) |
| `ledger config KEY [VALUE]` | show or set `context.tool_reads_max` (1 to 5, default 3), `context.briefing_budget_tokens` or `writes.receipt_days` (default 30) |
| `ledger write-status REQUEST_ID` | whether a write sent with `--request-id` was saved |
| `ledger remember "fact" [--source user_said]` | store a fact as you |
| `ledger revise ID "corrected fact" --reason "why" [--source user_said\|inferred\|observed\|document --document-id N]` | correct a fact |
| `ledger dispute ID --reason "why"` | flag a fact as contested |
| `ledger history ID` · `ledger as-of 2026-09-30T14:00:00` · `ledger beliefs` | look back |
| `ledger decide "title" --summary … --rationale …` | record a decision |
| `ledger ingest FILE` · `ledger search-docs "query"` | documents |
| `ledger backup` · `ledger restore ARCHIVE --out FILE` | backups (see below) |
| `ledger check` | integrity check, plus `files`: any memory file or folder other users of this computer could read (a report only; it never changes a mode) |
| `ledger reindex` | rebuild search indexes, including a full repair of the vectors file if needed |
| `ledger semantic enable` · `ledger semantic disable` (aliases `on` / `off`) | turn meaning-based search on or off (see below) |
| `ledger semantic status` | `disabled`, `downloading`, `warming`, `indexing`, `ready` or `degraded`, with pending and failed counts and a reason. Never starts anything |
| `ledger semantic import DIRECTORY` | install the model from a folder you copied in, instead of downloading it (see below) |
| `ledger acceptance --out report.json` | run the release checks on throwaway stores and write a report (never touches your memory) |
| `ledger selftest [--offline]` | prove the install works (throwaway store). `--offline` forces keyword-only checks and contacts nothing |

Refusals print `refused: <reason>` and exit with status 2.

## Writes are safe to retry

Every write tool (`remember`, `revise`, `dispute`, `record_decision`, `link_records`, `create_handover`,
`ingest_document`) and the matching `ledger` commands (`--request-id`) accept an optional `request_id`. The memory
saves a receipt for it in the same transaction as the write itself:
- sending the same write again with the same id saves nothing new and returns the original record, marked
  `"replayed": true`;
- the same id with a different write, or from a different agent, is refused;
- `write_status` answers `committed` or `not_found`. Because the receipt and the write are saved together, `not_found`
  means no write was recorded under that id within the retention window, so sending it again is safe.

Receipts are kept 30 days (`ledger config writes.receipt_days N`); a retry after that is treated as a new write. A
document is also recognised by its content, so ingesting the same text twice never stores it twice.

## Which files the agent may read

The `ingest_document` tool accepts a file path only if, after symlinks are resolved, the file is inside the folder
the memory server was started in (the project the agent is working on) or inside `~/.ledger-memory/inbox/`, and no
part of the path inside that folder starts with a dot (so `.git`, `.ssh`, `.aws`, `.config` are refused). Anything
else is refused with a message telling the agent to paste the text instead. Pasted text is unaffected. The `ledger
ingest FILE` command has no such limit: you typed the path. To hand the agent a file from elsewhere, copy it into the
inbox folder.

## Searching with a natural question

`recall`, `brief` and `recall_more` search with the whole question *and* with each of its keywords, then merge the
results, so "NAS password" finds "Changed the NAS admin password" even though the words are not adjacent. This
works with meaning-based search off.

## Privacy tiers: present, not used

Every record carries a privacy tier, and every read passes through one authorised, logged door that checks it.
This memory is for one person and one local agent, so it uses exactly one tier: every record is stored in the same
tier, and the local reader (a single credential created the first time the memory starts) can see all of it.
Every tool call refreshes that reader's presence before it reads, so nothing is ever hidden from your own agent.

- The reader's token is in `~/.ledger-memory/reader.token`, readable only by you (on Windows, protected by your
  user profile). It is never printed, never logged and never returned by a tool.
- Each read is logged once in the memory (that a read happened, how many records, never what they said).
- **Do not change any record's tier, the reader's ceiling, or the `context.tool_reads_t1` setting.** They exist so
  the tier checks are real, but this configuration does not use them, and changing one would hide records from the
  agent.

## Upgrading from 0.1.0

Automatic. The first time version 0.2.0 or later starts on an existing memory, it takes a checksummed backup into
`~/.ledger-memory/backups/` (named `ledger-pre-upgrade-<time>.db.xz`), then adds the new tables. Nothing in your
existing records is altered, and the backup restores like any other (`ledger restore`). `ledger init` reports it.

## Upgrading from 0.2.0

Automatic, with nothing to migrate. 0.3.0 no longer uses LM Studio. If you had turned meaning-based search on
with LM Studio, the memory still starts normally with keyword and full-text search, and `ledger semantic status`
says why meaning-based search is off. Run `ledger semantic enable` to switch to the built-in model. Nothing
contacts LM Studio any more.

## Meaning-based search (optional)

Keyword and full-text search work without it. Meaning-based search finds a fact by what it means, across
wording and languages: "How does the lab track rain?" finds "The station measures rainfall every morning".

```bash
ledger semantic enable     # downloads the model once (about 2.3 GB), verifies it, starts the service
ledger semantic status     # wait for "ready"
```

- **The model** is [BAAI/bge-m3](https://huggingface.co/BAAI/bge-m3) (multilingual, 1024 dimensions), at a pinned
  revision. Every file's size and SHA-256 is checked before it is used and again before it is loaded. A failed or
  partial download never replaces a working model. Downloads use your operating system's certificate store; behind
  a company proxy that inspects TLS, set `LEDGER_CA_BUNDLE` to your company's root certificate file. Verification
  is never turned off.
- **No network at all?** Copy the model folder in and import it: `ledger semantic import DIRECTORY`, then
  `ledger semantic enable`. The folder must hold exactly the pinned files (`README.md` and the eight files under
  `onnx/`); anything else is refused. Import verifies and caches the files but does not turn search on.
- **One service per user.** The model runs in one background process shared by every session and every memory
  of your OS user. It loads on first use (about half a second when warm), uses about 1.7 GB of RAM while loaded,
  and exits after 5 minutes without requests, releasing that memory. Sessions talk to it over a private local
  socket (a named pipe on Windows) protected by a random key in your user's cache folder. It receives text,
  returns numbers, and never opens your memory file or stores the text.
- **Writes never wait for the model.** `remember`, `ingest` and the other writes save immediately. Indexing
  happens in the background: status shows `indexing` with a pending count, and until a record is indexed it is
  still found by keyword. A record changed while it was being indexed is re-indexed, never published stale. A
  record the model keeps failing on is retried at most three times per version and then counted under
  `failed_rows`; running `ledger semantic enable` again retries it. When the service itself is unavailable
  (not downloaded, not starting), nothing is counted against any record: the work waits for the next write or
  session start.
- **Long texts are not cut.** A text longer than the model's limit is split into pieces of at most 8,192 tokens;
  every piece is embedded and the pieces are combined into one vector, weighted by length. Nothing is silently
  dropped.
- **Measured** on an Apple Silicon Mac, 20 synthetic English/Romanian questions whose wording differs from the
  stored fact: keyword search put the right fact in the top 5 for 10 of 20; meaning-based search for 20 of 20.
  This is a small check, not a benchmark of every kind of question. Reproduce it with
  `tools/benchmark_embeddings.py`.

Semantic results are the *closest in meaning*, so a result found only by meaning may be loosely related.
`ledger semantic disable` turns it off again (the model stays cached; enabling again does not re-download).

## Where things live

| Path | |
|---|---|
| `~/.ledger-memory/memory.db` | the memory (the only authoritative file). Created owner-only (0600), as are its `-wal`/`-shm` files and `memory.vectors.db`, whatever your umask; an existing file's mode is never changed |
| `~/.ledger-memory/reader.token` | the local reader's credential (owner-only; never shown) |
| `~/.ledger-memory/memory.vectors.db` | embeddings for meaning-based search (derived; `ledger reindex` rebuilds it) |
| `~/.ledger-memory/memory.db.connections.lock`, `~/.ledger-memory/.ledger.sessions.lock`, `~/.ledger-memory/.ledger.lock` | empty lock files: every open connection, every session and start-up/upgrade use them so a restore never overlaps live work. Leave them in place |
| `~/.ledger-memory/backups/` | `ledger-<UTC time>.db.xz` with a `.sha256` file beside each |
| `~/.local/share/ledger-memory/` | the program |
| `~/.config/opencode/opencode.json` | the `mcp.ledger` entry and the rules file under `instructions` |
| `~/Library/Caches/ledger-memory/` (macOS), `~/.cache/ledger-memory/` (Linux), `%LOCALAPPDATA%\ledger-memory\` (Windows) | the model (about 2.3 GB) and the search service's private socket and key. Owner-only. Safe to delete while no session is running: search falls back to keyword until you enable again |

`*.pre-replacement-*` and `*.before-restore-*` files next to the vectors file are earlier copies of the derived
embeddings, kept rather than deleted when the vectors are rebuilt. You can delete them.

## Backup and restore

```bash
ledger backup                                   # compressed copy + sha256, in ~/.ledger-memory/backups
```

To roll the memory back, close opencode (and any other agent client using this memory) first:

```bash
ledger backup                                   # keep a copy of the current state too
ledger restore ~/.ledger-memory/backups/ledger-<time>.db.xz --out ~/.ledger-memory/memory.db --force
```

Restore checks the checksum before writing, and refuses a tampered or unverified archive. Restoring over the live
memory also rebuilds the embeddings, so meaning-based search can never return something the restore removed. To
inspect an old backup without touching your memory, restore it to another file (`--out ~/old-memory.db`); that works
while sessions are open.

**Restoring over the live memory refuses while anything is using it.** Every process that uses the memory holds a
shared session lock for as long as it runs: the MCP server your agent client starts, each `ledger` command, and the
background indexing job. A restore over the live memory takes that lock exclusively and, if any of them holds it,
refuses with "this memory is in use by another session … close them and retry" before touching anything. While a
restore runs, a starting session waits for it to finish (a `ledger` command waits up to 30 seconds, the MCP server up
to 2 minutes), and background indexing steps aside and resumes later.

**An interrupted restore is finished or reported.** A restore over the live memory records its progress in
`restore-in-progress.json` in the memory folder. If it is interrupted (a crash, a power cut), the next start sees the
file and either confirms that nothing was replaced or completes the remaining steps. The program that does it reports
it (for `ledger status`, a `recovery` note in its output). A damaged progress file stops the memory from starting until
you look at it.

These locks are advisory, as on any desktop system: they coordinate every program that follows the protocol, but
they do not stop a program that ignores it. If you use the Python library directly, wrap your work in
`with ledger.session():` so a restore can see you.

## Removing something

The memory is append-only by design, and there is no delete in the agent tools or the CLI. To correct
something, `revise` it. If something sensitive was stored by mistake, the honest options are:

- restore a backup taken before it was written (later writes are lost), or
- move `~/.ledger-memory` aside and start a fresh memory.

Targeted redaction is not part of this version.

## Settings

| Variable | Default | |
|---|---|---|
| `LEDGER_HOME` | `~/.ledger-memory` | where the memory lives |
| `LEDGER_AGENT_NAME` | `agent` (`opencode` in the installed config) | the name agent writes are attributed to |
| `LEDGER_EMBEDDING_BACKEND` | unset (uses `ledger semantic` setting) | force `bge-m3` or `none` for one process |
| `LEDGER_CA_BUNDLE` | unset | an extra root certificate file trusted for the model download (company proxies) |
| `LEDGER_EMBED_THREADS` | `2` | CPU threads the model may use (1 to 32) |
| `LEDGER_EMBED_BATCH` | `4` | texts per model call (1 to 4) |
| `LEDGER_WORKER_IDLE_SECONDS` | `300` | how long the search service stays loaded without requests (1 to 86400) |
| `LEDGER_VECTORS_PATH` | next to `memory.db` | where the embeddings file lives |
| `LEDGER_APP_DIR` | `~/.local/share/ledger-memory` | install location (installer only) |

## Other MCP clients

Any MCP client can launch the same server: the command is the absolute path to
`~/.local/share/ledger-memory/.venv/bin/ledger-mcp` (stdio), with `LEDGER_HOME` in its environment. Run
`./install.sh --no-opencode` to skip the opencode wiring. Then give the agent the contents of `MEMORY-RULES.md`
as standing instructions; most clients have a rules or custom-instructions file for this.

## Uninstall

```bash
~/.local/share/ledger-memory/uninstall.sh
```

This removes the opencode entries (backing up the config first) and the `ledger` link, and moves the program
aside. Your memory in `~/.ledger-memory` is never touched.

## Several sessions, several people

Several agent sessions on one machine can share a memory: SQLite waits for locks instead of failing, and all
sessions share one search service. It is not a network server. Give each person their own memory on their own
machine.

## How it works

- **Records:** facts (superseded, never updated in place), decisions, handovers, links, documents and their
  chunks. Every write appends an event with the acting actor; facts, revisions, disputes and decisions also get an
  audit entry (handovers and links are recorded as events only).
- **Time:** every fact carries when it became known, and a superseded fact points to its successor.
  `beliefs_as_of` replays that chain. Disputes are events; "disputed" is derived from them, never stored as
  a status.
- **Search:** three lanes, merged with reciprocal-rank fusion. `LIKE` is the floor that always works, then
  SQLite FTS5, then cosine similarity over local bge-m3 embeddings. The keyword indexes are refreshed with each
  write; embeddings are computed in the background. The embeddings file records which version of each record
  and which model every vector came from, and a gate refuses to serve meaning-based results that don't
  exactly match the database.
- **Schema:** the baseline migration plus one read-path migration, hash-locked in `migrations/MIGRATIONS.lock`. A changed migration file
  is detected, never applied silently.
- **Speed:** measured on an Apple Silicon Mac with 5,000 synthetic facts (about a year of heavy use) and
  meaning-based search off: `remember` takes about 16 ms and `recall` about 130 ms. Both grow roughly linearly
  with the store. With meaning-based search on, writes take the same time (indexing is in the background) and a
  query adds one call to the local model.

## Development

```bash
uv sync --extra dev --extra semantic
uv run --extra dev pytest   # offline: no test downloads a model or contacts a service
uv run ledger selftest --offline
```

The tests cover the MCP server over real stdio (tool list, no delete tool, read-only annotations, refusals
reaching the agent, nothing written to the working directory), every refused write leaving no rows, time
travel, restore and embeddings consistency, schema closure, and a guard that no shipped file carries internal
references.

Tested on macOS (Apple Silicon) with Python 3.13 and opencode's documented config format. Linux uses the same
code paths but hasn't been tested.

## Windows (release candidate)

This version adds a Windows installer, but it has not yet been checked on a real Windows machine, behind a
company proxy or with antivirus scanning. Treat it as a release candidate until that report exists.

In PowerShell 7.2 or later, with uv installed, from the unpacked package folder:

```powershell
./install.ps1 -SemanticDependencies
```

It installs the dependencies into the package's own `.venv` and nothing else: it does not create a memory,
change client settings or `PATH`, or download the model. Then point your MCP client at
`.venv\Scripts\ledger-mcp.exe` (stdio) with `LEDGER_HOME` set, give the agent `MEMORY-RULES.md` as standing
instructions, and run `.venv\Scripts\ledger.exe semantic enable` if you want meaning-based search.

Please run `.venv\Scripts\ledger.exe acceptance --out acceptance.json` and send back the report. Checks it
cannot perform (native service behaviour, proxy, antivirus) are reported as unverified, never as passed.

## Model and license

The bge-m3 model is made by BAAI and published under the MIT license, according to its model card at the pinned
revision. It is not part of this package: it is downloaded or imported on request, together with its `README.md`
(the model card), which is kept beside the model files. The Python dependencies have their own licenses.
