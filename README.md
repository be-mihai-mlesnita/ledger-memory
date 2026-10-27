# Ledger Memory For Mac

**Publication pending. The command below is not currently a live installer.**
The distribution manifest is deliberately disabled. An approved, publication-audited
kit and its exact tag, build commit and SHA-256 are required before enabling it.
No release, commit or checksum has been fabricated or approved by this command.

## One Command

After publication, paste the entire block from the private project's GitHub
homepage into Terminal. Reuse the same command for future approved updates.
Each run authenticates to the fixed repository and resolves `main` exactly once.
It validates that full commit SHA, then fetches the bootstrap, manifest and
verifier at that immutable snapshot. This trusts the authenticated repository's
approved `main` content, not a permanently frozen source commit or an arbitrary
release. The snapshot's manifest selects one exact kit, including prereleases;
the bootstrap never chooses the GitHub "latest release".

```bash
/bin/bash <<'BASH'
set -euo pipefail
umask 077
export GH_HOST=github.com GH_PROMPT_DISABLED=1
unset GH_DEBUG
fail() { printf '%s\n' "$*" >&2; exit 1; }
confirm() {
  local reply
  printf '%s [yes/ok to continue; anything else cancels]: ' "$1" >/dev/tty
  IFS= read -r reply </dev/tty || fail 'Cancelled.'
  case "$reply" in [yY][eE][sS]|[oO][kK]) ;; *) fail 'Cancelled.' ;; esac
}
[ "$(uname -s)" = Darwin ] || fail 'This command supports macOS only.'
case "$(uname -m)" in arm64|x86_64) ;; *) fail 'Supported Mac architectures: arm64 and x86_64.' ;; esac
( : </dev/tty ) 2>/dev/null || fail 'Run this command in Terminal; /dev/tty is required for consent and the manager menu.'
if ! command -v gh >/dev/null 2>&1; then
  command -v brew >/dev/null 2>&1 || fail 'Obtain GitHub CLI (gh) through your approved software channel; no approved Homebrew was found.'
  confirm 'Use your existing approved Homebrew to download/install GitHub CLI and its dependencies with brew install gh?'
  HOMEBREW_NO_AUTO_UPDATE=1 HOMEBREW_NO_INSTALL_CLEANUP=1 brew install gh || fail 'GitHub CLI installation failed.'
  command -v gh >/dev/null 2>&1 || fail 'gh is not on PATH after installation. No shell profile was changed.'
fi
if ! gh auth status --hostname github.com >/dev/null 2>&1; then
  confirm 'Browser login alone does not authenticate Terminal. Start interactive gh auth login --hostname github.com --web and save GitHub CLI authentication?'
  ( unset GH_PROMPT_DISABLED; gh auth login --hostname github.com --web </dev/tty >/dev/tty 2>/dev/tty ) || fail 'GitHub CLI login failed or was cancelled.'
  gh auth status --hostname github.com >/dev/null 2>&1 || fail 'GitHub CLI authentication is still unavailable.'
fi
commit=$(gh api --hostname github.com --method GET repos/be-mihai-mlesnita/ledger-memory/commits/main --jq .sha) || fail 'Cannot read this private repository; check your account access.'
[[ "$commit" =~ ^[0-9a-f]{40}$ ]] || fail 'The repository returned an invalid full source commit.'
d=$(mktemp -d "${TMPDIR:-/tmp}/ledger-homepage.XXXXXXXX")
trap 'rm -rf -- "$d"' EXIT
trap 'exit 130' INT
trap 'exit 143' TERM HUP
gh api --hostname github.com --method GET "repos/be-mihai-mlesnita/ledger-memory/contents/bootstrap.sh?ref=$commit" -H 'Accept: application/vnd.github.raw+json' >"$d/bootstrap.sh" || fail 'Bootstrap download failed; nothing downloaded will be executed.'
test -s "$d/bootstrap.sh" || fail 'Empty bootstrap download; nothing will be executed.'
/bin/bash "$d/bootstrap.sh" --source-commit "$commit" </dev/tty
BASH
```

This downloads a complete file before running it with the system Bash; there is
no `curl | bash`, embedded credential, token-bearing URL, or evaluation of remote
manifest values as shell code. Authentication uses the installed `gh` credential
mechanism. A browser session alone does not authenticate the terminal. The account
must also have access to this private repository. If GitHub CLI is missing, the
command may install it using **existing approved Homebrew**, but only after
explicit `/dev/tty` consent. If authentication is missing, a separate consent
starts interactive `gh auth login --hostname github.com --web`; complete its
official browser/device flow and terminal prompts directly, never by putting
credentials into this command or chat. Declining either prompt stops immediately.
Nothing installs GitHub CLI or changes authentication without that consent.

The manager provides the **install / update / uninstall** menu and the final
action confirmation. This bootstrap does not implement those operations itself.
It does not force a BeMind or model-worker restart. Any required application
restart is an explicit follow-up, not an automatic bootstrap action.

## Prerequisites

- Supported hosts: macOS on `arm64` or `x86_64` (including the active architecture under Rosetta).
- GitHub CLI authenticated to `github.com`, with permission to read the private repository and its release assets.
- The homepage command can offer `brew install gh` through existing approved Homebrew, and separately offer interactive GitHub CLI web login. Homebrew automatic updates and cleanup are disabled.
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
completed so that the private manifest can be read. All prerequisite confirmations
accept `yes` or `ok` case-insensitively and use `/dev/tty`.
Declining a prerequisite prompt stops the workflow. A successful earlier approved
prerequisite installation is not rolled back if a later step is cancelled or fails.
No noninteractive "yes" flag is supported. Piped stdin cannot grant consent.

## Distribution Interface

The bootstrap requires `--source-commit FULL_40_LOWERCASE_HEX_SHA`. It retrieves
`deployment/distribution.json` and `deployment/bootstrap_verify.py` from that
same immutable source ref in `be-mihai-mlesnita/ledger-memory` using
`gh api --hostname github.com`. The homepage resolves `main` once and supplies
that full SHA; the bootstrap never re-resolves a branch. Authenticated repository
access plus its approved `main` content is the trust model, not an independently
authenticated publisher signature. Every change to `main` requires review
because the same command intentionally follows its future changes.

The manifest has these fields:

| Field | Enabled Manifest Contract |
| --- | --- |
| `schema_version` | Integer `1` |
| `ready` | Boolean `true`; checked-in value is `false` |
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
existing PowerShell launchers and neutral deployment documentation. The parent
bundle builder must include **`manage.py` and `bootstrap_verify.py`**; existing
kits without the manager cannot be bootstrapped.

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
The manager inherits stdin (the homepage command uses `/dev/tty`) and owns its
menu, operation-specific consent, installation state and rollback. It must locate
kit assets relative to `__file__`, preserve the selected interpreter for children,
and finish copying anything it needs before it exits. The bootstrap preserves
the manager exit status and deletes only its own owner-private temporary tree.
It cannot clean up after SIGKILL or power loss; stale `ledger-bootstrap.*` and
`ledger-homepage.*` directories should be reviewed before manual removal.

There is deliberately no local-kit bypass or automatic fallback to unreviewed
source. Tests exercise enabled local fixtures through a fake GitHub CLI without
downloads, authentication changes or real manager actions. A real end-to-end
installation remains pending an approved artifact and separately owned manager.

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

The active installation remains on its previously validated rc6 kit. The new
Mac installer and runtime distribution are separately validated source changes;
editing this repository does not install or activate them.

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
