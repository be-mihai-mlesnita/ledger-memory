"""The baseline schema: exactly the memory tables, referentially closed, and hash-locked."""
from __future__ import annotations

import sqlite3

from ledger_memory import db, migration_catalog

READ_PATH_TABLES = {"client_devices", "read_audit_entries", "source_attestations", "imported_sources", "mental_models",
                    "mental_model_versions", "mental_model_version_inputs", "mental_model_claims",
                    "mental_model_claim_evidence", "mental_model_contradictions", "mental_model_evaluations",
                    "extraction_runs", "extraction_candidates", "entities", "entity_aliases", "entity_record_links",
                    "graph_edges", "graph_rebuilds"}
BASELINE_TABLES = {"actors", "config", "events", "audit_entries", "facts", "fact_evidence", "fact_glosses", "decisions",
          "decision_facts", "context_packages", "context_package_items", "relationships", "documents", "chunks",
          "search_fts", "search_index_entries", "search_index_rebuilds", "semantic_index_entries",
          "semantic_index_rebuilds", "schema_migrations"}
TABLES = BASELINE_TABLES | READ_PATH_TABLES


def test_baseline_builds_exactly_the_memory_tables(tmp_path):
    p = tmp_path / "m.db"
    db.initialize_database(p)
    con = sqlite3.connect(p)
    names = {r[0] for r in con.execute("select name from sqlite_master where type = 'table'")}
    assert {n for n in names if not n.startswith(("sqlite_", "search_fts_"))} == TABLES
    missing = [(t, ref) for t in names for ref in {r[2] for r in con.execute(f"pragma foreign_key_list('{t}')")}
               if ref not in names]
    assert missing == []                                  # no foreign key points outside the package
    con.execute("pragma foreign_keys = on")
    assert con.execute("pragma foreign_key_check").fetchall() == []


def test_the_baseline_alone_is_the_0_1_0_schema(tmp_path):
    from importlib import resources
    con = sqlite3.connect(tmp_path / "old.db")
    con.executescript((resources.files("ledger_memory.migrations") / "001_baseline.sql").read_text())
    names = {r[0] for r in con.execute("select name from sqlite_master where type = 'table'")}
    assert {n for n in names if not n.startswith(("sqlite_", "search_fts_"))} == BASELINE_TABLES - {"schema_migrations"}


def test_migration_lock_matches_the_shipped_files():
    status, problems = migration_catalog.catalog_status()
    assert status == "ok", problems
