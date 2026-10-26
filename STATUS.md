# Project Status

Updated October 26, 2026.

## Core

- Core baseline: `0.3.0-rc3`.
- `upstream/ledger-memory/` contains selected source and technical reference files with
  unchanged contents. `upstream/RELEASE.json` records their hashes and archive provenance.
- Work-Mac verification of the original archive: 484 tests passed, 42 build-only skips;
  offline self-test: 32 passed, zero failed, one semantic skip.
- Restore exclusion was verified on a synthetic store: an open MCP session caused
  refusal; after it closed, restore succeeded and integrity checks passed.
- Keyword fallback is an AND of query words. File modes may reflect the caller's umask
  even under a private store directory.
- Native Windows core-lock validation and a specifically verified corporate TLS-inspection
  path remain unverified. Core source contents have not been patched.

## BeMind Deployment

- Global Luna gateway deployed, with shared memory retained and no history re-import.
- Public tools are `memory_read` and `memory_write`; the private worker is pinned to Luna.
- Reads are allowed, writes request approval, and plan-mode writes are denied.
- 845 tests passed, including native direct/gateway lifecycles, write deduplication,
  upgrade receipt preservation and exact legacy configuration restoration.
- Locally built kit SHA-256:
  `7243ce7a16581a25d279aaa2f6e355a8147d1a75fc76ccfe15cb28b195f2f5e1`.
- Live facts/history access returned canonical evidence. Historical results can remain
  incomplete and carry warnings; this is not proof that all semantic indexing is complete.
- Existing BeMind instances require restart to load changed global settings; none was
  force-restarted. Caching/coalescing is disabled. Windows Luna mode is refused pending
  native support/validation; the package retains direct mode.
- Normal MCP-tool isolation is not an OS security boundary against the same user's shell.
- No verified billed-cost percentage is claimed.

## Repository

- Intended personal repository: `be-mihai-mlesnita/ledger-memory`, private.
- Git metadata is local to the work Mac, outside the synchronized working folder. Use a
  separate clone on other machines, not the machine-local `.git` pointer.
- The repository audit covers nested archives as well as plain files. Its two explicit
  literal exceptions are reviewed synthetic credential-rejection fixtures, not secrets.
- Source, tests and selected technical documentation are versioned. Binary artifacts,
  operational notes, model weights, caches, runtime state and live databases are local-only.
- Check Git/GitHub for actual publication state. No unattended publishing is configured.
