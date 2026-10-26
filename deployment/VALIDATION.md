# Deployment Validation

Validated October 25-26, 2026 on macOS 27.0.1, Apple Silicon/arm64, Python 3.13.15,
uv 0.13.5. The live BeMind installation, configuration and memory were not used
as installation/uninstallation test targets. Native gateway tests used the existing
client's normal authentication without copying credentials; their memories and
configurations were disposable synthetic fixtures.

## Result

**845 tests passed in 273.40 seconds**, including native direct-mode and Luna-gateway tests:

```sh
BEMIND_DEPLOY_NATIVE_TEST=1 BEMIND_GATEWAY_NATIVE_TEST=1 \
  BEMIND_GATEWAY_AUTH_DATA_HOME=/absolute/user-data-home \
  BEMIND_GATEWAY_BEMIND=/absolute/bemind \
  BEMIND_GATEWAY_LEGACY_MCP=/absolute/ledger-mcp \
  python -m pytest -q deployment/tests
```

- JSONC parsing/editing preserves unrelated content and comments, supports trailing
  commas and rejects duplicate keys and invalid syntax without printing config values.
- Synthetic deployment tests cover checksums, hostile ZIP paths, unrelated settings,
  ownership conflicts, guarded rollback, concurrent edits, interruption markers,
  data retention, uninstall idempotence, runtime locks and uncertain process cleanup.
- Real synthetic child-process tests cover inherited and detached descendants.
  Uncertain cleanup always retains runtime files and prevents automatic retry.
- The real rc3 test uses a disposable HOME and paths containing spaces. It executes
  fresh install, repeated-install upgrade, an actual MCP handshake, refusal to uninstall
  while the MCP server holds a session, successful uninstall after closing it, repeated
  uninstall, reinstall, and a final uninstall. Its synthetic fact survives throughout.
- Shell syntax and install/uninstall help entry points were checked.
- The native Luna test migrated an existing single-command ledger-mcp configuration,
  read facts and historical documents with the real pinned model, executed one exact
  synthetic write, deduplicated it before and after an upgrade, and restored the exact
  original configuration/instruction section on uninstall. The synthetic fact survived.
- Private request scopes, model identity, tool sets, canonical evidence, indeterminate
  writes, duplicate receipts, database replacement detection, and whole-request restore
  exclusion have dedicated regression coverage.
- An independent targeted review closed the process-cleanup safety finding after the
  fail-closed handling and detached-child regression were added.

## Limits

PowerShell was unavailable on this test host. Windows launchers, Windows ACL/process
behavior and Linux execution were not tested natively. Validate them on representative
managed machines before rollout. No execution-policy or TLS bypass is provided.

The deployment smoke tests are keyword-only. They do not redownload the optional model,
exercise company-specific TLS interception, or certify every future package version.
Caching/coalescing is explicitly disabled in this initial gateway. The runtime refuses
Windows safely; the direct-mode Windows launcher remains provided but not natively validated.
The included rc3 archive is unchanged; its original SHA-256 is:

```text
ba95854518b304a65832c20691678c132814d03c3590e2d91cb79671c57c74aa
```
