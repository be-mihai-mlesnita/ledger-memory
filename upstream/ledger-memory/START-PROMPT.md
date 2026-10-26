# Set up long-term memory (ledger-memory)

Paste everything below the line into opencode. The agent installs the memory, checks it, wires it into
opencode, and reports back.

---

Set up a long-term memory for yourself on this computer, using the **ledger-memory** package. The package is
the folder containing `install.sh`, `README.md` and `MEMORY-RULES.md`. If you don't know where it is, ask me
for the path before doing anything else.

Rules for this setup:
- Do the steps in order. If a step fails, stop and show me the error. Don't improvise a workaround.
- Don't delete anything, and don't use `sudo`. The installer backs things up and moves old files aside by
  itself.
- Ask me before installing or downloading anything that isn't named in these steps.
- For each step, show me the lines that prove the result, not just "done".

**Step 1: uv.** Run `uv --version`. If uv is missing, ask me whether to install it with the official
installer, `curl -LsSf https://astral.sh/uv/install.sh | sh` (or `brew install uv` if I use Homebrew). After
installing, check that `uv --version` works. The installer may say to open a new shell or add `~/.local/bin`
to PATH; do what it says for this session.

**Step 2: install.** In the package folder, run `./install.sh`. It copies the program to
`~/.local/share/ledger-memory`, installs pinned dependencies (and Python 3.11+ if needed), creates my memory
at `~/.ledger-memory`, runs a self-test of about 30 checks on a throwaway store, links `ledger` into `~/.local/bin`, and
registers the memory in opencode's global config after backing that file up.
- Success means the output ends with `==> Done` and the self-test summary says `0 failed`. SKIP lines for
  semantic search are expected.
- If it ends with `ACTION NEEDED` (my opencode config is JSONC or not plain JSON), everything else is
  installed, and it printed a snippet. Show me the snippet and, once I agree, merge it into that file by
  hand: back the file up first and keep everything already in it.

**Step 3: meaning-based search (optional).** Ask me whether to turn on meaning-based search. Tell me it
downloads the bge-m3 model once (about 2.3 GB, from its official repository, checksums verified) and then uses
about 1.7 GB of RAM while it is in use, released after 5 idle minutes.
- If I agree, run `~/.local/bin/ledger semantic enable`, then `~/.local/bin/ledger semantic status` until
  `state` is `ready` (it may show `downloading`, `warming` or `indexing` first). Show me the final status.
- If the download fails, show me the error and stop this step. Don't retry with other sources or turn off
  certificate checks. The README section "Meaning-based search" explains the company-proxy setting
  (`LEDGER_CA_BUNDLE`) and the offline import.
- If I decline, skip this step. The memory works without it (keyword and full-text search), and it can be
  turned on later with `ledger semantic enable`.
- The self-test always reports the semantic check as SKIP; that is expected.

**Step 4: verify the wiring.** Show me the `mcp.ledger` entry and the `instructions` entry in
`~/.config/opencode/opencode.json`. Check that the `command` path exists and is executable. Then run
`~/.local/bin/ledger status`.

**Step 5: report.** Give me a short summary: what was installed where, the self-test result, whether
meaning-based search is on, and the name of the config backup if one was made. Then tell me to **restart
opencode**, because the memory tools only load at startup.

**How to use the memory from now on.** For each question I ask, call the `brief` tool once with my question and
answer from what it returns. Call `recall_more` only when the briefing lacks something, with a short, specific
query (a few calls per question at most; the next `brief` starts a new allowance). `MEMORY-RULES.md` has the full
rules.

After the restart, try it: ask "What do you remember about me?" (empty at first). Then tell it something
lasting, such as "I prefer short answers with the command first", start a new session, and ask again.
