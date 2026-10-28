# Ledger-Memory Deployment For BeMind

Per-user install and uninstall scripts for BeMind's global MCP configuration.
They do not configure a separate OpenCode installation, import personal histories,
change the user's selected model, or restart BeMind. Luna mode pins only the private
memory worker to `bemind-proxy/gpt-6-luna`. No personal data or user-specific paths
are included in this deployment kit.

## Prerequisites

- An existing BeMind installation.
- `uv` on PATH, installed through your approved software process.
- Python 3.11 or newer. If needed, install the tested interpreter explicitly with
  `uv python install 3.13`. The launchers discover existing interpreters without downloading
  one. `BEMIND_DEPLOY_PYTHON` can select an absolute interpreter path.
- On Windows: PowerShell 7.2 or newer and permission to run the scripts under your
  organization's policy. Obtain approval/signing if policy blocks them; do not bypass it.

## Guided Mac Setup

The repository homepage provides the authenticated GitHub bootstrap command.
It resolves the approved source snapshot, verifies the selected release kit and
starts `manage.py`. A fresh installation is described before an explicit `yes`
or `ok` is accepted. Existing managed installations offer Upgrade, Uninstall or
Cancel. Empty input, EOF and non-terminal input never approve an action.

From an already verified, extracted kit with Python 3.11 or newer:

```sh
python3 -I -B manage.py
python3 -I -B manage.py --dry-run
python3 -I -B manage.py --embed-threads 6
```

Fresh installation and upgrade require an existing BeMind executable. Uninstall
does not require BeMind or uv. The guided workflow can offer uv installation
through an existing approved Homebrew installation, with separate consent; it
does not install Homebrew, use sudo, or silently change shell profiles.

Automatic embedding threads are one-third of the usable logical CPU count,
rounded down, with a minimum of one and maximum of 32. The resolved count is
saved at installation and preserved on upgrade. `--embed-threads N` selects an
explicit count; `--embed-threads auto` recalculates the automatic choice.
This controls inference concurrency, not an exact CPU-utilization quota. No
niceness change is made. Existing shared model workers retain their old setting
until safely restarted; the installer does not force-stop them.

The bootstrap manifest remains disabled until a reviewed source-only release kit
has a published tag, build commit and SHA-256. Reusing the same homepage command
then discovers future manifest-approved updates without editing that command.

## Runtime Distribution

The downloadable core profile is `bemind-runtime-1`, based on core `0.3.0rc6`.
Its `DISTRIBUTION.json` records the original archive hash and the one diagnostic
message wording override. Control and data logic are unchanged. Original release
artifacts are retained separately; the distribution has its own checksum.

From the repository root, prepare and audit the pinned runtime input before
building a kit (the approved local publication policy is required):

```sh
python3 deployment/prepare_distribution.py --archive builds/ledger-memory-0.3.0-rc6.zip --output builds/runtime/ledger-memory-0.3.0-rc6.zip --audit-root .
python3 deployment/build_bundle.py --output deployment/dist/bemind-ledger-memory-0.3.0-rc6-deployment.zip
```

The bundle builder defaults to `builds/runtime/`, not the original input archive.
It includes the guided manager and the strict outer-archive verifier.

The runtime dependency installation uses Python 3.13 by default and may download it
through uv if a different interpreter runs the launcher. `--python` selects another
runtime version; compatibility of the release's pinned dependencies must be checked.

## What To Distribute

The kit name is derived from `ARCHIVE_NAME` in `bemind_deploy.py`; for the configured
release it is `dist/bemind-ledger-memory-0.3.0-rc6-deployment.zip`, with an adjacent
SHA-256 sidecar. Extract a separately built and verified kit into a new directory before running it.

Alternatively, distribute these files together:

- `install.sh`, `uninstall.sh`, `run.sh` for macOS/Linux.
- `install.ps1`, `uninstall.ps1`, `run.ps1` for Windows.
- `bemind_deploy.py`, `jsonc_edit.py`, `MEMORY-RULES.md`, and this README.
- `gateway/*.py` and `gateway/*.mjs`, including `memory_gateway.py`, `ledger_backend.py`,
  `gateway_backend_contract.py`, and `worker_guard.mjs`. Do not include gateway tests,
  caches, private state or credentials. The installer copies only top-level runtime
  Python/JavaScript modules, excluding hidden files and `test_*` modules.
- The configured `ARCHIVE_NAME` release (`ledger-memory-0.3.0-rc6.zip`) and its unchanged
  `.zip.sha256` sidecar.

Within this source workspace, the scripts also find the release in `../builds/`.
Use `--archive /path/to/release.zip` for another approved release; its sidecar is
mandatory. Checksums detect corruption, not the trustworthiness of the distributor.
Do not distribute a user's memory, config backups, history snapshots, or model credentials.

## Install

From the extracted kit/deployment directory, preview first:

```sh
./install.sh --dry-run
./install.sh
```

On Windows:

```powershell
.\install.ps1 --memory-mode direct --dry-run
.\install.ps1 --memory-mode direct
```

The installer verifies the archive hash and safe member paths, prepares a unique
version directory, installs the frozen dependencies, and runs the offline self-test.
New installations default to **Luna mode**. Before activating `mcp.memory`, the installer
stages private settings and runs the gateway's zero-inference `preflight`. No paid/model
call occurs during installation. Public configuration contains only the Python gateway
`serve --settings ...` command, never the private worker configuration or raw ledger entry.
Existing unrelated content and comments are preserved. Public `ledger_*` is denied;
`memory_read` defaults to `allow`, `memory_write` to `ask`, and
`agent.plan.permission.memory_write` to `deny`. Existing matching denials remain denied.
Pass `--memory-write allow` to let sessions write without an approval prompt; upgrades
keep the previous choice until `--memory-write ask` is supplied. Plan-agent writes stay
denied, and a stronger existing user policy (`ask`/`deny`) still takes precedence.
No other agent configuration is changed. Private worker grants are scoped independently,
not achieved by overriding a Task parent's denial.

Use `--memory-mode direct` for the original `mcp.ledger` integration. Upgrades and
reinstalls preserve the receipt's mode unless `--memory-mode direct|luna` is supplied.
Existing schema-1 direct receipts remain supported. The BeMind executable is located on
PATH, then under `~/.bemind/bin/`; use `--bemind-command "/absolute/path/to/bemind"`
to select it explicitly. Luna mode is currently unsupported on Windows. The installer
and install dry-run refuse it before creating deployment state or downloading dependencies;
select `--memory-mode direct` on Windows. Existing Luna deployments must be managed on
a supported POSIX system, not force-converted past the runtime's platform checks.

Caching and request coalescing are **disabled** in this initial Luna gateway version.
The settings schema's `cache_ttl_seconds` field is not a promise of an active cache.
Repeated reads may perform separate model requests; do not assume cached results or
cache-based cost savings. Request serialization is separate from coalescing.

It initializes a new memory, or takes an integrity-checked SQLite backup of an existing
memory without running schema migrations on it. The package's normal startup migration
rules apply when the new MCP server first opens an older store. Previous runtime versions
are retained during upgrades so their embedded interpreter paths stay valid.

Successful completion says `Installation succeeded`. Restart BeMind when active work
can safely stop. Existing server instances cache global configuration; changing the file
does not immediately update all open projects or the current model's tool inventory.
Project/agent/managed-policy overrides can still take precedence over the global settings.

Default paths:

| Purpose | Path |
| --- | --- |
| BeMind config | `$XDG_CONFIG_HOME/bemind`, otherwise `~/.config/bemind` |
| Runtime versions | `$XDG_DATA_HOME/bemind-ledger-memory/versions`, otherwise `~/.local/share/bemind-ledger-memory/versions` |
| Shared memory | `$LEDGER_HOME`, otherwise `~/.ledger-memory` |
| Ownership/rules/recovery state | `<config-dir>/ledger-memory-deployment/` |
| Private gateway settings/state | `<config-dir>/ledger-memory-deployment/gateway/` |
| Private operation logs | `<app-root>/logs/` |

The receipt records the exact executable, active configuration and restoration manifest.
No global PATH entry or shell profile is changed. Direct-mode instructions name the CLI;
Luna-mode instructions explicitly prohibit a direct ledger CLI fallback.

Each gateway runtime gets its own private state directory. Before an upgrade switches
settings, the old gateway must stop its owned worker and block further requests. Durable
write receipts are copied unchanged into the new generation after quiescence and under
the deployment transaction; old receipts, histories, worker configuration and authentication
files are retained. BeMind's existing main authentication is used through its normal data
home (`XDG_DATA_HOME`, otherwise `~/.local/share`), never copied or deleted.

For custom locations, pass `--config-dir`, `--app-root`, and/or `--store`. Use the same
location options for subsequent upgrade and uninstall commands. Destinations must not
overlap destructively or contain symlinks/junctions; resolve system aliases such as
macOS `/var` to their real paths when choosing temporary custom locations.

## Existing Store Permissions

For Luna on POSIX, the selected store and checked files must belong to the current OS
user and be private: use **0700 for the store directory** and **0600 for database and
checked sidecar/lock files**. This includes `memory.db` and any existing checked SQLite
WAL/SHM/journal or session-lock files. Links, non-regular database files and hard-linked
files are not a substitute for these ownership/permission checks.

The installer does **not** chmod an existing memory store or its files, including an
existing empty store directory. An operator must explicitly harden checked files before
using Luna; a permissions refusal is not authorization for automatic changes.

1. Stop all sessions and indexing jobs using the selected store. Confirm its absolute
   path and inspect ownership, file type, link count and permissions for each checked
   database/sidecar/lock file. Stop if ownership or links are unexpected.
2. After verifying those exact paths and approving the change, set the store to 0700
   and each existing checked file to 0600 individually. For example, for verified paths:

```sh
chmod 700 "/absolute/path/to/memory"
chmod 600 "/absolute/path/to/memory/memory.db"
```

3. Apply the same 0600 mode individually to verified existing sidecar/lock files, such
   as `memory.db-wal`, `memory.db-shm`, `memory.db-journal` or `.ledger.sessions.lock`
   when present. Do not use recursive/wildcard chmod, create missing sidecars, or change
   another user's files. Recheck permissions before retrying installation/preflight.

For fresh installations, the CLI uses umask 077 for itself and its children and restores
the previous umask in `finally`, including refusal/interruption paths. This does not alter
the caller's shell umask or retroactively harden an existing store. Backend/cache ownership
and the existing BeMind data-home/authentication settings remain unchanged.

## Explicit Migration

Unmanaged ledger entries are refused by default. To adopt a manually installed ledger's
**configuration only**, explicitly choose the store and any old references to replace:

```sh
./install.sh --memory-mode luna --store "/absolute/path/to/memory" \
  --migrate-existing-ledger \
  --replace-instruction "/absolute/path/to/previous/MEMORY-RULES.md" \
  --agents-file "/absolute/path/to/AGENTS.md" \
  --agents-section '## Shared long-term memory (all projects)'
```

`--replace-instruction` is repeatable and matches exact existing instruction strings.
`--agents-file` and `--agents-section` must be supplied together; only that exact Markdown
section is replaced. Omit them when no legacy block needs replacement. No prior rules file
contents are edited. The selected legacy entry must be a local two-element `ledger serve`
command (also `ledger.exe serve`), or a single absolute `ledger-mcp` / `ledger-mcp.exe`
executable path. Its `environment.LEDGER_HOME`, or default `LEDGER_HOME`/`~/.ledger-memory`,
must resolve to the selected store. Extra arguments, ambiguous commands and cross-store
migrations are refused.

Migration records the previous raw entry, permission values, instruction items/positions
and selected AGENTS block. Uninstall restores them when managed portions are unchanged,
including original bytes when there are no intervening edits. Unrelated later edits are
preserved; overlapping/ambiguous edits cause refusal, never a guessed overwrite. Old
manual runtimes, scripts and memory are not adopted or deleted.

This is same-user normal-tool isolation, not protection against that user's shell or
SQLite access. A hard access boundary requires a separate account or host. Historical
content remains untrusted, and existing model prechecks, approvals and privacy rules apply.

## Optional Semantic Search

The existing setup-1 release leaves model download **off by default**. The upcoming
installer 0.3.1 uses the pinned curl/verified-cache flow after final install or
upgrade confirmation; it does not fetch the model during uninstall. The optional
inference libraries are installed either way, and keyword memory works without
a downloaded model. For manual package launchers, explicitly opt in:

```sh
./install.sh --semantic
./install.sh --semantic --model-dir "/path/to/verified/bge-m3-bundle"
./install.sh --semantic --ca-bundle "/path/to/approved-ca-bundle.pem"
```

Use the corresponding `.ps1` launcher with `--memory-mode direct` and the same semantic
arguments on Windows. The local
model directory must contain the package's expected manifest files, including `onnx/`.
The package verifies it before import. Without `--model-dir`, `--semantic` can make a
large official model download. TLS verification stays enabled.

Semantic setup runs after the base installation is committed. If it fails, exit code 2
and the message explicitly say that keyword memory remains installed. Do not assume an
error means installation was rolled back. Normal readiness/indexing checks still apply.

## Uninstall

Close BeMind instances using the memory and allow background indexing/model work to
finish or become idle. Then run:

```sh
./uninstall.sh --dry-run
./uninstall.sh
```

```powershell
.\uninstall.ps1 --dry-run
.\uninstall.ps1
```

Uninstall removes only this installer's unchanged MCP entry, restores its recorded
instruction/permission/AGENTS changes, and removes its receipt-listed runtime directories.
It verifies ownership markers and holds runtime/session/worker locks during removal.
Luna uninstall first runs the owned gateway's `stop`, refusing a busy/unproved stop without
detachment or binary deletion. The stop command may terminate its own idle worker; it
never signals an unrelated process. Dry-run performs no gateway commands and cannot
certify that the gateway will be idle when actual uninstall runs.

**Memory, imported histories, backups, shared model cache, gateway write receipts and recovery state are always
preserved.** There is no data-purge option. Uninstall is not a forgetting mechanism.
Repeated uninstall is safe. Reinstall uses the retained memory.

For deregistration without removing binaries, use `--keep-runtime`. In direct mode it
permits detachment while runtimes are busy. In Luna mode the owned gateway must still
quiesce successfully. Open clients may retain the old connection until
they restart. Run ordinary uninstall later, with the same location options, to remove
the retained owned runtimes once they are idle.

An edited MCP entry, duplicated managed instruction, mismatched ownership marker, or
unmanaged installation is not guessed away. Reconcile it explicitly before retrying.
Adopting legacy configuration requires the explicit migration flag; its old runtime
files never become installer-owned.

## Failures And Recovery

Configuration/rules/settings/selected AGENTS/receipt writes use a single manifest with
private backups, atomic replacement and checks
for concurrent edits. `pending.json` makes an interrupted transaction explicit.
If an external command fails or times out, process cleanup is attempted, but detached
descendants cannot always be accounted for. In that case the runtime is retained and
the pending marker blocks retry rather than deleting files a process might still use.
Gateway stop's settings change is included in the transaction. If activation or uninstall
fails after a successful stop, unchanged old settings/configuration are rolled back and
the old gateway is re-enabled when safe. Unproved cleanup or recovery leaves a pending
marker and retains binaries. Private receipts/backups may contain original configuration
snapshots; never publish them with a distribution or issue report.

Keep the private logs and pending marker. For a configuration transaction, compare its
recorded before/after hashes and backups; never restore over newer user edits. For a
process-cleanup marker, verify the recorded process/runtime state before any cleanup.
Have the deployment operator review recovery before removing a pending marker.
Do not delete the marker blindly or bypass an in-use refusal.

Exit codes: `0` success/preflight/no-op, `1` refusal or failure requiring attention,
`2` base install retained after semantic failure or partial runtime cleanup, and `130`
for a caught interruption when no uncertain child cleanup needs to be reported.

## Validation

macOS/Apple Silicon: synthetic safety tests plus opt-in real installation/upgrade tests.
The real test covers install, repeat-install upgrade, an actual MCP handshake, refusing
uninstall with an open session, uninstall, repeated uninstall and reinstall while
preserving a synthetic fact. Tests never target the live development memory.

```sh
python -m pytest -q deployment/tests
```

Run these from the workspace root using a test environment containing pytest and the
release's MCP dependency. The historical native smoke validates direct semantics; its
install invocations must explicitly pass `--memory-mode direct` before opting into it
with `BEMIND_DEPLOY_NATIVE_TEST=1`. Do not drop its MCP/session-lock coverage. The real
test also needs uv. See `VALIDATION.md` for the proposed native gateway plan; no live-user
gateway deployment is claimed by these synthetic tests. PowerShell/Windows ACL and process
handling, and native Linux behavior, still require native validation before rollout.
No native Windows or Linux pass is claimed from the macOS tests.
