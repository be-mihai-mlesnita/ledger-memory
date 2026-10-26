# Core Source Snapshot

`ledger-memory/` contains source, tests and selected technical reference files from the
verified rc3 archive identified in `RELEASE.json`. Included file contents are unchanged.
The manifest describes the selected files, not a complete archive extraction.

Do not silently overwrite local modifications when refreshing the snapshot. Verify the
input checksum, review the resulting diff, and preserve source-version provenance.

Binary release inputs are managed separately. Generic installation helpers are retained
where needed by source tests; use the top-level `deployment/` entry points for BeMind.
