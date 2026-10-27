# Deployment Validation

Validated October 26, 2026 against core 0.3.0-rc5 on macOS 27.0.1, Apple Silicon/arm64, Python 3.13.15,
uv 0.13.5. The live BeMind installation, configuration and memory were not used
as installation/uninstallation test targets. Native gateway tests used the existing
client's normal authentication without copying credentials; their memories and
configurations were disposable synthetic fixtures.

## Result

**955 deployment/gateway regression tests passed, with 2 opt-in tests skipped.**
The native rc4-to-rc5 gateway upgrade test was run separately and passed.
The unmodified core suite passed **538 tests, with 42 build-only skips**; its offline
self-test passed **32 checks, with 1 semantic skip**.

```sh
python -m pytest -q deployment/tests
BEMIND_GATEWAY_NATIVE_TEST=1 \
  BEMIND_GATEWAY_AUTH_DATA_HOME=/absolute/user-data-home \
  BEMIND_GATEWAY_BEMIND=/absolute/bemind \
  BEMIND_GATEWAY_LEGACY_MCP=/absolute/ledger-mcp \
  BEMIND_GATEWAY_SEED_PYTHON=/absolute/rc4-environment/bin/python \
  BEMIND_GATEWAY_PREVIOUS_KIT=/absolute/rc4-deployment.zip \
  python -m pytest -q deployment/tests/test_native_gateway.py
```

- JSONC parsing/editing preserves unrelated content and comments, supports trailing
  commas and rejects duplicate keys and invalid syntax without printing config values.
- Synthetic deployment tests cover checksums, hostile ZIP paths, unrelated settings,
  ownership conflicts, guarded rollback, concurrent edits, interruption markers,
  data retention, uninstall idempotence, runtime locks and uncertain process cleanup.
- Real synthetic child-process tests cover inherited and detached descendants.
  Uncertain cleanup always retains runtime files and prevents automatic retry.
- The native test uses a disposable configuration/store and paths containing spaces.
  It seeds an actual rc4 store, installs the prior rc4 gateway kit, then upgrades using
  the rc5 package. Reads and gateway write receipts survive; an identical write ID remains
  deduplicated across the upgrade. Final uninstall restores the exact original setup.
- Shell syntax and install/uninstall help entry points were checked.
- The native Luna test migrated an existing single-command ledger-mcp configuration,
  read facts and historical documents with the real pinned model, executed one exact
  synthetic write, deduplicated it before and after an upgrade, and restored the exact
  original configuration/instruction section on uninstall. The synthetic fact survived.
- Private request scopes, model identity, tool sets, canonical evidence, indeterminate
  writes, duplicate receipts, database replacement detection, and whole-request restore
  exclusion have dedicated regression coverage.
- Gateway project filtering is enforced on returned document evidence, including HR/QA
  whole-word cases; broader core fallback results cannot silently cross the project filter.
- A separate native core check upgraded a disposable rc4 store to rc5 with a verified
  pre-upgrade backup and migrations `003_write_receipts.sql` / `004_chunks_fts.sql`;
  repeated a same-ID write without duplication; checked committed/not-found status,
  full-question and one-word keyword search, private file modes, and rc4 rollback.
- Historical source/known-at/currentness fields are retained without inventing missing
  information; older-core compatibility remains tested.
- A separate real local ONNX check published 33 synthetic records in batches of 32 and 1,
  with zero failures/discards and no pending rows. The drain took 4.65 seconds after model
  activation. This is not a claimed production-corpus speedup.
- An independent targeted review closed the process-cleanup safety finding after the
  fail-closed handling and detached-child regression were added.

## Limits

PowerShell was unavailable on this test host. Windows launchers, Windows ACL/process
behavior and Linux execution were not tested natively. Validate them on representative
managed machines before rollout. No execution-policy or TLS bypass is provided.

The native gateway migration is keyword-only; the separate batch check used a verified
local model cache. Neither redownloaded weights, established corporate TLS interception,
nor certified a future package version. The standalone direct-mode installer was not
separately rerun natively for this upgrade.
Caching/coalescing is explicitly disabled in this initial gateway. The runtime refuses
Windows safely; the direct-mode Windows launcher remains provided but not natively validated.
The included rc5 archive is unchanged; its original SHA-256 is:

```text
a655954f8bf02079f0489dbfd1100f1e2600b8b6d74062a24547b7c6e69ce972
```
# Mac Setup Candidate

October 27, 2026: confirmed terminal setup, persistent one-third CPU selection and
authenticated GitHub bootstrap passed 1,306 regression tests, with six optional
tests skipped and 66 subtests passed. The skipped tests cover optional online and
native model-integrated paths, not failures. Shell syntax and full-kit extraction
were also verified.

A real disposable Mac lifecycle built four runtimes with offline `uv sync --frozen`:
auto four threads, explicit six, preserved six, reset auto four, then uninstall.
Each runtime's offline selftest passed 32 checks with one semantic skip. Memory and
write receipts were preserved; configuration was restored byte-for-byte. No live
user installation or model was changed. BeMind was a presence-only fixture.

Runtime profile `bemind-runtime-1` preserves control/data logic and changes exactly
one diagnostic-message string. Preparation passed 49 tests; selected restoration
tests passed 39 cases plus two smoke checks. Original artifacts remain unchanged.
Source and nested-package checks used the required local publication policy with
zero findings. Actual private-release download, real Luna inference/authentication,
and Intel-Mac runtime execution remain separate validation steps.
