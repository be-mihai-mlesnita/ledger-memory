# Ledger Memory For Mac

**Install, upgrade or uninstall BeMind's shared memory from Terminal.**
The installer downloads the verified `bemind-mac-setup-1` prerelease.
This repository is private: your GitHub account must have access to it.

**One line starts setup. The downloaded script handles the checks and prompts.**

## Start Here

1. Have BeMind installed for a new installation or upgrade. It is not required to
   uninstall an existing managed memory installation.
2. Complete the one-time GitHub CLI setup below, unless `gh` is already installed
   and signed into an account with repository access.
3. Open **Terminal**, paste the single line under One Command and press Return.
   Do not add `sudo`. The downloaded installer handles all remaining checks and
   prompts; approve any Python/uv setup separately.
4. For a new installation, read the displayed changes and type `yes` or `ok` to
   proceed. For an existing installation, choose **1. Upgrade**, **2. Uninstall**
   or **3. Cancel**, then confirm the displayed action. Enter or any other response
   cancels at an approval prompt. Earlier approved prerequisite changes may remain.
5. After a successful install or upgrade, restart BeMind yourself when your work
   can safely stop. Already-open instances do not automatically load the new setup.

**To upgrade later:** run the same command again and choose Upgrade. It finds the
currently approved release; no command edit is needed.

**To uninstall:** run the same command and choose Uninstall. Managed integration
and owned runtimes are removed; memory, histories, backups and receipts are retained.

Automatic indexing threads use one-third of usable logical CPUs, rounded down
(minimum one, maximum 32): a 12-CPU Mac gets four threads. Upgrades preserve the
saved choice. This is not a strict CPU-percentage cap, and running model workers
keep their previous setting until safely restarted. Model downloads remain opt-in.
The installer does not import your conversation history.

Tested on Apple Silicon. Intel-Mac runtime execution and real Luna authentication/
inference were not part of this release's validation.

## One Command

With GitHub CLI installed and authenticated, paste this one line. Reuse it for
installation, upgrades and uninstall:

```bash
(s=$(gh api --hostname github.com -H 'Accept: application/vnd.github.raw+json' repos/be-mihai-mlesnita/ledger-memory/contents/install.sh) && test -n "$s" && /bin/bash -c "$s")
```

The setup logic lives in `install.sh`, not in the pasted command. The command
executes nothing if the entry-script download fails or is empty. It does not
embed credentials or stream a partially downloaded script into a shell.

The entry script is fetched from the authenticated repository's default branch.
It then resolves `main` once and pins the bootstrap, manifest and verifier to that
full commit. The manifest selects the exact checksummed release. The initial
entry-script fetch and the later pinned snapshot are separate requests; both
trust reviewed repository content. No release is selected through a "latest" URL.

## One-Time GitHub Setup

Unlike a public installer, this private repository needs authenticated access
before its entry script can be downloaded. A browser login alone is insufficient.
If GitHub CLI is missing, obtain it through your approved software process, or
use an existing approved Homebrew installation:

```sh
brew install gh
```

Then sign in once using your account with repository access:

```sh
gh auth login --hostname github.com --web
```

Complete GitHub's browser/device flow yourself; never paste credentials into the
install command or chat. Already authenticated users skip these steps. A plain
anonymous `curl` command cannot download this private file.

An entry script downloaded manually through the authenticated GitHub website can
also be run with `/bin/bash install.sh`. That local-file path retains the guided
GitHub CLI installation/sign-in prompts; it does not make the private one-line
download available before authentication.

## If Setup Stops

| Message or situation | What to do |
| --- | --- |
| Cannot read the private repository | Check that GitHub CLI is signed into an account with repository access. A working browser login alone is insufficient. |
| No approved Homebrew or required tool available | Obtain the missing GitHub CLI, Python or uv through your approved software process, then rerun the same command. This installer does not install Homebrew. |
| Unmanaged installation, edited configuration or recovery marker | Stop and reconcile the reported state. Do not delete memory or receipts to force installation; automatic adoption/removal is deliberately refused. |
| An active session or worker prevents an operation | Let the work finish and close the relevant application sessions when safe, then retry. Do not force-delete runtimes or lock files. |

The manager provides the **install / update / uninstall** menu and the final
action confirmation. This bootstrap does not implement those operations itself.
It does not force a BeMind or model-worker restart. Any required application
restart is an explicit follow-up, not an automatic bootstrap action.

## Prerequisites

- Supported hosts: macOS on `arm64` or `x86_64` (including the active architecture under Rosetta).
- GitHub CLI authenticated to `github.com`, with permission to read the private repository and its release assets.
- GitHub CLI must be installed and authenticated before using the private one-line download. The downloaded script handles remaining setup; a manually downloaded entry file also supports confirmed GitHub CLI setup through existing approved Homebrew.
- Existing Python >=3.11 on PATH, or an existing `uv` installation able to find a suitable Python.
- An explicit existing `BEMIND_DEPLOY_PYTHON` executable takes precedence, matching the deployment launcher. An invalid override is refused rather than silently ignored.
- If Python and uv are missing but approved Homebrew already exists, the bootstrap offers `brew install uv` and waits for case-insensitive `yes` or `ok` via `/dev/tty`.
- If uv cannot find Python >=3.11, it separately offers `uv python install 3.13 --no-bin` and waits for `yes` or `ok`. The minor version is pinned; uv selects the available 3.13 patch version.
- Without Python, uv or approved Homebrew, the bootstrap stops with concrete prerequisite instructions. Obtain them through corporate software controls. It never bootstraps a package manager, invokes sudo, or changes shell profiles.

The bootstrap provisions Python only when no suitable interpreter exists. With
Python already available it does not require or install uv: the manager owns
install-only uv provisioning and its consent. Uninstall must not require uv or
an installed BeMind executable merely to reach the manager's uninstall flow.
Without Python, even uninstall needs an interpreter to run that manager.

No Python/uv provisioning or kit download occurs while the manifest is disabled
or invalid, the release tag has the wrong commit, or the exact published asset
is absent. GitHub CLI setup/login may already have been separately approved and
completed before the private entry script is fetched. All prerequisite confirmations
accept `yes` or `ok` case-insensitively and use `/dev/tty`.
Declining a prerequisite prompt stops the workflow. A successful earlier approved
prerequisite installation is not rolled back if a later step is cancelled or fails.
No noninteractive "yes" flag is supported. Piped stdin cannot grant consent.

## Distribution Interface

The bootstrap requires `--source-commit FULL_40_LOWERCASE_HEX_SHA`. It retrieves
`deployment/distribution.json` and `deployment/bootstrap_verify.py` from that
same immutable source ref in `be-mihai-mlesnita/ledger-memory` using
`gh api --hostname github.com`. The entry script resolves `main` once and supplies
that full SHA; the bootstrap never re-resolves a branch. Authenticated repository
access plus its approved `main` content is the trust model, not an independently
authenticated publisher signature. Every change to `main` requires review
because the same command intentionally follows its future changes.

The manifest has these fields:

| Field | Enabled Manifest Contract |
| --- | --- |
| `schema_version` | Integer `1` |
| `ready` | Boolean `true`; `false` disables new bootstrap downloads |
| `repository` | Exactly `be-mihai-mlesnita/ledger-memory` |
| `platform` | Exactly `darwin` |
| `architectures` | Exactly `["arm64", "x86_64"]` |
| `version` | Numeric `major.minor.patch`, optionally with a prerelease suffix |
| `tag` | Exact reviewed release tag, 1-128 ASCII letters/digits/dots/underscores/hyphens, starting with a letter/digit |
| `asset` | Exact reviewed ZIP filename, ASCII letters/digits/dots/underscores/hyphens, starting with a letter/digit, at most 164 characters |
| `sha256` | Exact asset SHA-256, 64 lowercase hexadecimal characters |
| `kit_commit` | Full 40-character lowercase commit to which `tag` must resolve |
| `layout` | `root` or `deployment` |

The source pin and `kit_commit` need not be the same commit. Build and approve the
kit first, then publish the manifest naming that kit on reviewed `main`. The next
run of the same homepage command resolves that new snapshot without a command
edit. This avoids a self-referential manifest or asset hash. A draft release is
refused; an explicit prerelease is valid.
GitHub tag resolution, published asset presence and the independently reviewed
manifest digest are checked. These checks do not claim artifact build attestation.

For `layout: "root"`, the extracted kit has `manage.py`, `bemind_deploy.py`,
`jsonc_edit.py`, `bootstrap_verify.py`, `install.sh`, `uninstall.sh`, `run.sh`,
`gateway/`, and the configured `ledger-memory-VERSION.zip` plus its `.sha256`.
For `layout: "deployment"`, all those entries are under `deployment/` and no
siblings are allowed. The verifier's fixed `FILES` allowlist also permits the
existing PowerShell launchers and neutral deployment documentation. The release
kit includes **`manage.py` and `bootstrap_verify.py`**; older kits without the
manager cannot be used with this bootstrap.

Only ZIP_STORED and ZIP_DEFLATED are supported; no ZIP extra metadata is accepted.
The limits are 128 members and 512 MiB compressed/uncompressed. Traversal, absolute
paths, backslashes, non-ASCII names, links/special files, duplicate/case-colliding
members, unknown files, special permission bits, and executable data/docs are
refused before extraction. The complete outer ZIP is SHA-256 checked with
`shasum -a 256` and again by the verifier, then CRC-checked before extraction.
The manager remains responsible for validating the inner core ZIP and its version
using the existing deployment validation. Changing the kit file list requires a
reviewed verifier change, not a manifest-provided executable allowlist.

The exact handoff is `SELECTED_PYTHON -I -B EXTRACTED_KIT/manage.py`, or
`SELECTED_PYTHON -I -B EXTRACTED_KIT/deployment/manage.py`. No action is injected.
The manager inherits stdin (the entry script uses `/dev/tty`) and owns its
menu, operation-specific consent, installation state and rollback. It must locate
kit assets relative to `__file__`, preserve the selected interpreter for children,
and finish copying anything it needs before it exits. The bootstrap preserves
the manager exit status and deletes only its own owner-private temporary tree.
It cannot clean up after SIGKILL or power loss; stale `ledger-bootstrap.*` and
`ledger-homepage.*` directories should be reviewed before manual removal.

There is deliberately no local-kit bypass or automatic fallback to unreviewed
source. Offline bootstrap tests use a fake GitHub CLI. A separate disposable Mac
test performed real install/upgrade/uninstall operations with offline dependencies,
preserved synthetic memory and restored configuration. The published release's
downloaded bytes, provenance and extraction have also been verified. No production
installation or real model authentication/inference was performed by those checks.

## Offline Tests

Run with an existing Python >=3.11 and `jq` (only the test fixture needs standalone
jq; production uses the GitHub CLI's built-in jq):

```bash
python3 -I -B -m unittest discover -s deployment/tests -p 'test_bootstrap.py' -v
/bin/bash -n bootstrap.sh
```

Offline process tests use an isolated PATH and HOME, a fake `gh`, and a harmless
manager fixture. Homebrew, uv and authentication changes are stubs; the suite
installs nothing. Repeated runs of the unchanged homepage command are tested
against different synthetic `main` commits and require one coherent snapshot
per run.

Two integration checks are opt-in:

- Set `LEDGER_BOOTSTRAP_TEST_KIT` to an absolute candidate-local path to a complete kit built by `deployment/build_bundle.py`. The complete unchanged ZIP passes through the homepage/bootstrap/verifier flow with fake downloads. Python dispatch intercepts the real manager before execution and compares its bytes with the built kit; no real manager action runs.
- Set `LEDGER_BOOTSTRAP_LIVE_READONLY=1` to run `LiveReadOnlyTests` with the installed `gh`. These issue only GET requests, keep HOME/cache temporary, and read existing GitHub CLI credentials without logging in or changing them. Synthetic manifests are passed only to client-side `--jq`, never to the server. The tests cover enabled/disabled/invalid manifests and the raw-JSON contents API contract at a resolved full commit. If private access is unavailable, `LEDGER_BOOTSTRAP_API_REPOSITORY=cli/cli` checks the same engine/contents contract against GitHub's public CLI repository; it does not prove access to the private project.

```bash
LEDGER_BOOTSTRAP_LIVE_READONLY=1 python3 -I -B deployment/tests/test_bootstrap.py LiveReadOnlyTests -v
```

## Source Layout


| Area | Purpose |
| --- | --- |
| `upstream/ledger-memory/` | Verified core source and technical reference files |
| `upstream/RELEASE.json` | Provenance for the active core snapshot |
| `documentation/` | Technical usage documentation |
| `deployment/` | Installation, uninstallation, Luna gateway and migration code |
| `BACKLOG.md` | Deferred proposals, including forgetting |
| `repo_tools/` | Work-side repository safety tooling |

See `STATUS.md` for the current technical state and `deployment/README.md` for setup.

## Versions

Deployment target: **0.3.0-rc6** (release candidate; final 0.3.0 not yet delivered),
original archive SHA-256:

```text
6ff85f5f0a90afd7dcdd97d10af0fcbaad8845448cf2dac67dabc6564ed35934
```

The checked-in core reference snapshot remains **0.3.0-rc3**, as identified by
`upstream/RELEASE.json`. It is versioned separately from the active deployment target;
do not assume the reference snapshot is the running package.

Audited source-only installation kits are distributed separately from the Git tree.
The runtime distribution profile is recorded in `deployment/core-distribution.json`.
Supply a verified core ZIP and checksum for manual setup; `deployment/install.sh --archive PATH`
selects an explicit input. The BeMind entry point is `deployment/`, not the generic
installation helpers in the source snapshot.

Publishing this release does not upgrade an existing installation. Run the
homepage command and confirm Upgrade when ready; production installations are
not changed by repository publication.

## Deliberately Not In Git

Downloaded model bundles under `builds/models/`, live memory databases, raw conversation
stores, credentials, runtime state and caches remain local. Their exclusion is deliberate;
no Git LFS or additional storage product is enabled. The core release contains the pinned
model acquisition/verification logic, and deployment supports explicit local-model import.

Operational notes, binary packages and historical run artifacts are local-only. Published
files are selected and audited independently; a checksum alone does not establish that
an artifact is appropriate for version control.

## Keeping This Current

Record relevant technical changes, inspect the diff, run the repository audit, and publish
only after approval of the exact target and contents. Never silently reset or overwrite
local edits or another contributor's work.

Provide the approved local policy at `.repo-local/publication-policy.json` before using
the reviewed-snapshot workflow:

```sh
python repo_tools/audit_repository.py --require-policy --report .repo-local/audit.json
python repo_tools/stage_audited.py --report .repo-local/audit.json
python repo_tools/audit_repository.py --staged --require-policy --report .repo-local/staged-audit.json
git diff --cached --stat
```

Staging does not commit or publish. A changed file invalidates the earlier audit. The
publication preview and approval still apply after these checks.

On the work Mac, Git metadata is kept outside the synchronized working folder; `.git` is a
local pointer. Treat this as a single-writer worktree. On another machine, clone the
private GitHub repository separately rather than reusing that machine-local pointer.
