# Project Status

Updated October 27, 2026.

## Core

- Active deployment core: `0.3.0-rc6` (candidate validated on this Mac; no final
  `0.3.0` archive has been delivered). Checked-in reference snapshot: `0.3.0-rc3`.
- `upstream/ledger-memory/` contains selected source and technical reference files with
  unchanged contents. `upstream/RELEASE.json` records their hashes and archive provenance.
- Work-Mac verification of the rc6 archive: 558 tests passed, 42 build-only skips;
  offline self-test: 32 passed, zero failed, one semantic skip.
- Restore exclusion was verified on a synthetic store: an open MCP session caused
  refusal; after it closed, restore succeeded and integrity checks passed.
- Keyword fallback uses whole-word/prefix matching. The gateway additionally enforces
  project scope on returned evidence and preserves historical provenance fields.
- A disposable rc4-to-rc5 migration created a verified backup, applied two additive
  migrations, replayed a same-ID write without duplication, distinguished committed
  and absent write IDs, searched synthetic documents, and passed rc4 rollback checks.
- The live store applied both migrations after a verified backup; `ledger check` reports
  integrity OK and all memory files private. Its 2,547 documents and 39,615 chunks remain.
- New database and sidecar files are created owner-private; existing file modes are preserved.
- rc6 required no new migration. The live store remains current and its integrity check passed.
- A real local rc6 worker indexed 20 synthetic passages at default two threads in
  200.244 seconds. Median size was 4,060 characters / 2,008.5 tokens; all records
  published, with no failed/discarded rows or pending work in that fixture.
- Embedding requests were token-bounded (maximum observed 8,077 tokens). Busy status
  appeared during work; transient readiness-unavailable reports also occurred.
- Native Windows core-lock validation and a specifically verified corporate TLS-inspection
  path remain unverified. Core source contents have not been patched.

## BeMind Deployment

- Global Luna gateway deployed, with shared memory retained and no history re-import.
- Public tools are `memory_read` and `memory_write`; the private worker is pinned to Luna.
- Reads and writes are allowed by the global config; plan-mode writes are denied.
- 955 deployment/gateway regressions passed, two optional native tests skipped; the
  separately run native rc5-to-rc6 gateway upgrade test passed, including gateway receipts, duplicate protection,
  read access and exact configuration restoration on uninstall.
- Locally built kit SHA-256:
  `b371145676a0257d8b9012ccb423de5af37191f838ed6b0c423f273798aeaf68`.
- Live facts/history access returned canonical evidence. Historical results can remain
  incomplete and carry warnings; this is not proof that all semantic indexing is complete.
- Existing BeMind instances require restart to load changed global settings; none was
  force-restarted. Caching/coalescing is disabled. Windows Luna mode is refused pending
  native support/validation; the package retains direct mode.
- Normal MCP-tool isolation is not an OS security boundary against the same user's shell.
- No verified billed-cost percentage is claimed.
- Previous runtimes and backups are retained. Bulk indexing was stopped during the
  upgrade, then explicitly resumed at two threads on October 27. No application
  sessions were force-restarted and no persistent tuning setting was changed.
- Initial rc6 live baseline: 46 passages committed in 606.63 seconds, in three batches
  of 15, 16 and 15, with zero failures and median model CPU of 198.15%. This short
  publication-window measurement is not a matched-input fleet-sizing benchmark.
- The kit was built from a clean deployed baseline, excluding the unfinished local
  CPU-thread/priority experiment. That source work remains stopped and unshipped.
- rc5's core `write_status` is additive, but the gateway retains its conservative
  receipt/indeterminate-write handling rather than automatically retrying uncertain
  or legacy writes. It has not adopted the new core receipt path yet.
- Passive check on October 27 at 12:36 EET: 22,359 vector receipts, 17,261 pending
  (including failed revisions), zero failure rows. Workers remained active without
  interruption; model CPU was approximately 199%. Since the baseline start, 108
  records completed in 1,176.39 seconds (5.51/minute), implying about 52 hours
  remaining if that rate continues without sleep or other interruptions. This is
  a throughput extrapolation, not a guaranteed completion time or fleet benchmark.
- A controlled rc6 embedding-only comparison used the same eight real pending
  passages twice per setting, excluding warm-up (median 4,145 characters and
  2,005.5 tokens). Median times were 81.872 seconds at two threads, 30.100 at ten,
  and 39.927 at twenty. Ten threads were 2.72 times as fast as two; twenty took
  33% longer than ten. Average measured process CPU was approximately 197%, 915%
  and 1,021%, respectively. All outputs passed dimension, finite-value and norm checks.
- This small comparison excludes service/RPC and publication overhead; it is not
  a sustained full-index throughput test or a fleet-default decision. No core,
  installer, persistent thread setting or worker-state file was manually changed.
  Benchmark inputs stayed in RAM and production vector/failure counts stayed
  unchanged during the comparison. No thermal warning was reported before or after.
- The live drain resumed at its original two threads after the experiment. At
  12:52 EET on October 27, 15 further records had committed: 22,404 vector receipts,
  17,216 pending (including failed revisions), zero failure rows, model CPU 198.4%.
  Semantic indexing is still incomplete.
  A live historical read retained its incomplete-evidence warnings; successful
  semantic queries do not prove the lazy keyword index has populated.
- A second matched embedding-only comparison used eight new pending passages
  (median 3,784 characters / 1,908 tokens), ordered 10, 4, 6, 8, 10 threads with
  two measured runs per step after warm-up. Across individual runs, median times
  were 43.372 seconds at four threads, 34.278 at six, 30.557 at eight and 28.311
  at ten (four runs for ten, two for each other setting). Relative throughput was
  65%, 83%, 93% and 100%; mean process CPU was 384%, 578%, 757% and 878%.
  Ten-thread timings ranged from 27.761 to 32.554 seconds, so small differences
  remain indicative, not a sustained-load or fleet-wide default recommendation.
- Every measured output passed dimension, finite-value and norm checks. Production
  vector/failure counts were unchanged during the timed comparison. A private
  benchmark-launcher interrupt-inheritance issue and shutdown-lock timing issue
  were corrected; aborted attempts and successful results were retained privately.
  Core, deployment kit and installer defaults remain unchanged.
- After the second comparison, the live drain resumed at two threads. At 13:50:57
  EET on October 27, another 17 records had committed: 22,716 vector receipts,
  16,904 pending (including failed revisions), zero failure rows; model CPU 197.3%.

## Mac Installer

- New source entry point: `bootstrap.sh`; guided workflow: `deployment/manage.py`.
  The homepage command uses authenticated GitHub CLI, resolves `main` once, then
  pins the bootstrap, manifest and verifier to that commit. Release tag, asset hash
  and archive contents are verified before execution. Repeating the command can
  discover future manifest-approved upgrades.
- Fresh setup previews changes and requires terminal `yes`/`ok`; managed installations
  offer Upgrade, Uninstall or Cancel. Blank/EOF/non-terminal input cannot approve.
  Unmanaged or inconsistent ownership is refused, not adopted or deleted implicitly.
- Automatic threads are one-third of usable logical CPUs, rounded down and clamped
  to 1..32. Installation persists the resolved value; upgrades preserve it unless
  explicitly overridden or reset to auto. No niceness change or live worker retuning.
- Real disposable Mac Luna lifecycle passed with offline uv/runtime construction:
  automatic four threads, explicit six, preserved six on repeat upgrade, reset to four,
  then uninstall. Synthetic memory, backups and write receipts were retained; original
  JSONC was restored byte-for-byte. BeMind was a presence-only stand-in, not executed;
  real Luna inference/authentication and Intel-Mac runtime behavior remain unverified.
- Candidate regression suite: 1,306 passed, six optional tests skipped, 66 subtests passed.
  Runtime-distribution preparation: 49 tests; selected core restoration checks: 39 tests
  plus two smoke checks. Only one AST string constant differs in the selected module;
  94 other retained original files are byte-identical. Original release archives remain intact.
- Runtime profile `bemind-runtime-1` has SHA-256
  `4ebb3c8f4c5e72c1741b78e15e4f57b8488d2219ca33ce88e08a2cf08ee2826b`.
  Source and nested package audits applied the required local policy with zero findings.
  This is a heuristic check, not a guarantee of perfect secrecy.
- Private prerelease `bemind-mac-setup-1` was published from reviewed source commit
  `643f96555661c84082e3a91df7ef5f9996b0dd0b`. The downloaded release ZIP, checksum,
  tag target, exact release notes and nested archive privacy were verified.
  The enabled manifest pins that verified source and asset; the README includes
  setup steps, upgrade/uninstall choices, cancellation and troubleshooting guidance.
  No production installation, upgrade or worker restart was performed.
- The separately named local kit is
  `deployment/dist/bemind-ledger-memory-0.3.0-rc6-mac-setup-1.zip`, SHA-256
  `601c2dc589c720b8510161bf35cf5cf65c3d0ab42cbd3ccc6e6b662fa989e3a5`.
  Original deployment kits remain available unchanged. The promoted project passed the
  same 1,306-test suite and nested-archive audit. Publishing the homepage command
  does not upgrade existing installations without their users' explicit confirmation.
- Passive live check at 16:02 EET on October 27: the original rc6 indexing process
  remained active, with 23,438 vector receipts, 16,182 pending and zero failure rows.
- The homepage paste block is replaced with a one-line
  authenticated download of root `install.sh`. The existing launcher logic is
  moved into that file; `bootstrap.sh`, the manager and release ZIP are unchanged.
  GitHub CLI installation/sign-in is a one-time prerequisite for the private
  download. The same entry serves installation, upgrade and uninstall.
- Short-entry checks passed 55 tests, with four optional online checks skipped.
  Coverage includes Bash/zsh invocation, complete/partial/empty downloads, missing
  authentication, source pinning, cancellation and unchanged published-kit handoff.
  No live installation, repository access policy or indexing process was changed.

## Public Homebrew-First Launcher

- The owner made the existing repository public. Anonymous raw-script retrieval
  returned HTTP 200. All four published default-branch commits and the release
  assets were rechecked with the required policy; no findings were reported.
- The public `install.sh` implements curl/wget downloads without GitHub
  CLI, tokens or login. It offers the official Homebrew installer when absent,
  finds standard Homebrew prefixes, then offers only missing Python 3.13/uv packages.
  Tool preparation precedes the manager menu, including uninstall. Approved
  prerequisite changes are retained after later cancellation or failure.
- The public manifest pins the existing archive SHA-256 and verifier source commit.
  No REST tag lookup or GitHub authentication is used. Homebrew's official HEAD
  installer is mutable HTTPS content, not checksum-pinned by this launcher.
- Local validation: 1,328 tests passed, six optional checks skipped, 142 subtests
  passed. The local candidate also completed real anonymous manifest/verifier/ZIP
  downloads, hash verification, extraction and the existing-installation menu;
  blank input cancelled, with checked configuration unchanged.
- Real native verification reused existing tools. Missing Homebrew/formula
  installation was exercised with inert fixtures, not real system installation.
  No live Ledger upgrade, worker restart or model invocation occurred.
- The existing setup-1 ZIP and runtime distribution remain unchanged. The
  Homebrew-first public entry supersedes the older GitHub-CLI entry at `94e2c3c`.
  No new kit, asset replacement or core revision is required. Publication does
  not upgrade existing installations or restart their workers.

## Repository

- Personal repository: `be-mihai-mlesnita/ledger-memory`, verified public after the owner's change.
- Git metadata is local to the work Mac, outside the synchronized working folder. Use a
  separate clone on other machines, not the machine-local `.git` pointer.
- The repository audit covers nested archives as well as plain files. Its two explicit
  literal exceptions are reviewed synthetic credential-rejection fixtures, not secrets.
- Source, tests and selected technical documentation are versioned. Audited source-only
  ZIP kits are public release assets; operational notes, original input packages,
  model weights, caches, runtime state and live databases remain local-only.
- Check Git/GitHub for actual publication state. No unattended publishing is configured.
