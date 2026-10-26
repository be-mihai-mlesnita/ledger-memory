# Ledger-Memory

Source snapshots, tests and BeMind deployment tooling for a shared local memory service.

## Layout

| Area | Purpose |
| --- | --- |
| `upstream/ledger-memory/` | Verified core source and technical reference files |
| `upstream/RELEASE.json` | Provenance for the active core snapshot |
| `documentation/` | Technical usage documentation |
| `deployment/` | Installation, uninstallation, Luna gateway and migration code |
| `BACKLOG.md` | Deferred proposals, including forgetting |
| `repo_tools/` | Work-side repository safety tooling |

See `STATUS.md` for the current technical state and `deployment/README.md` for setup.

## Current Release

Core: **0.3.0-rc3**, original SHA-256:

```text
ba95854518b304a65832c20691678c132814d03c3590e2d91cb79671c57c74aa
```

Binary packages are managed separately from this source repository. Supply the verified
core ZIP and checksum when building or installing; `deployment/install.sh --archive PATH`
selects an explicit input. The BeMind entry point is `deployment/`, not the generic
installation helpers in the source snapshot.

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
