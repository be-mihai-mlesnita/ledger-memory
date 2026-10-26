from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
import os
import sqlite3


# Authoritative tables hold the truth. Facts are superseded, never updated in place; events and audit
# entries are never rewritten.
AUTHORITATIVE_TABLES = (
    "actors",
    "events",
    "audit_entries",
    "relationships",
    "facts",
    "fact_evidence",
    "decisions",
    "decision_facts",
    "context_packages",
    "context_package_items",
    "config",
    "documents",
    "client_devices",
    "imported_sources",
    "source_attestations",
    "mental_models",
    "mental_model_versions",
    "mental_model_version_inputs",
    "mental_model_claims",
    "mental_model_claim_evidence",
    "mental_model_contradictions",
    "mental_model_evaluations",
    "extraction_runs",
    "extraction_candidates",
    "entities",
    "entity_aliases",
    "entity_record_links",
)

# Derived tables are projections of authoritative rows: droppable and rebuildable, never the only copy.
DERIVED_TABLES = (
    "search_fts",
    "search_index_rebuilds",
    "search_index_entries",
    "semantic_index_entries",
    "semantic_index_rebuilds",
    "chunks",
    "fact_glosses",
    "graph_edges",
    "graph_rebuilds",
)

# Operational: durable, but excluded from exports. The read log records THAT a read happened, never what it said.
OPERATIONAL_TABLES: tuple[str, ...] = ("read_audit_entries",)

# Tables carrying a `sensitivity_tier` column. Derived tables take their tier from the authoritative row
# they project, at rebuild time.
TIERED_TABLES = (
    "actors", "events", "audit_entries", "relationships", "facts", "fact_evidence", "decisions",
    "decision_facts", "context_packages", "context_package_items", "documents",
    "imported_sources", "source_attestations",
    "mental_models", "mental_model_versions", "mental_model_claims",
    "mental_model_contradictions", "mental_model_evaluations",
    "extraction_runs", "extraction_candidates",
    "entities", "entity_aliases", "entity_record_links",
)

UNTIERED_TABLE_REASONS = {
    "config": "settings, lineage ids and backend names; not memory content",
    "client_devices": "principal identity, not tiered content",
    "read_audit_entries": "the read log carries no content column by construction: only device, count and ceiling",
    "mental_model_claim_evidence": "evidence pointers (record type, id, hash); no content of its own",
    "mental_model_version_inputs": "its input_sensitivity_tier is a snapshot of the input's tier at compile time: receipt data, not content",
    "graph_edges": "a projection over tiered entity and link rows",
    "graph_rebuilds": "the projection's rebuild ledger; counts only",
}

DERIVED_TABLES_REBUILD_TIER = DERIVED_TABLES

REQUIRED_TABLES = AUTHORITATIVE_TABLES + DERIVED_TABLES + OPERATIONAL_TABLES


@dataclass(frozen=True)
class InitResult:
    path: Path
    applied_migrations: tuple[str, ...]


class ReadOnlyDatabaseError(RuntimeError):
    """A read-only DB inspection that cannot proceed cleanly (e.g. a WAL bundle missing its `-shm` on read-only
    media). Subclasses ``RuntimeError`` so existing ``pytest.raises(RuntimeError, …)`` stays valid; ``main`` catches
    it and refuses with one clean line instead of a traceback."""


@contextmanager
def connect(database_path: Path):
    """A SQLite connection as a CLOSING context manager.

    MUST be used as ``with connect(path) as conn:``. Yields a configured ``sqlite3.Connection`` and, on exit,
    commits on success / rolls back on exception (matching ``sqlite3.Connection.__exit__``) and then ALWAYS
    closes it. The raw-connection form leaked FDs: ``with`` on a bare ``sqlite3.Connection`` only commits — it
    never closes — so each connection's db + ``-wal`` + ``-shm`` FDs lingered until GC. Under the executor's
    high connection churn that outran GC and hit EMFILE (``[Errno 24] Too many open files``) at the 256 soft
    limit, crash-looping the bot. Deterministic close fixes it (verified: a loop of ``with`` blocks leaks 0 FDs).

    when the db file exists but is NOT writable (a backup, read-only media, a chmod'd
    forensic copy), transparently degrade to the read-only path (``connect_readonly``) — so **no** command, read or
    write, can create WAL/SHM sidecars on, or fail to open, a read-only database. This fixes the whole read-only-CLI
    class at the generator (``connect``) rather than at ~122 individual read helpers. A normal writable db is the
    unchanged R/W + WAL connection; a write attempted against a read-only file raises ``OperationalError``.
    """
    if os.path.exists(database_path) and not os.access(database_path, os.W_OK):
        with connect_readonly(database_path) as connection:
            yield connection
        return
    connection = sqlite3.connect(database_path)
    try:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        # Resilience: wait up to 5s for a held lock instead of failing
        # immediately with "database is locked" when the tick, an engagement runner,
        # and ledger-watch touch the db concurrently. Lock contention becomes a brief
        # wait, not a write failure.
        connection.execute("PRAGMA busy_timeout = 5000")
        # Write-efficiency (owner: minimise SSD writes on the M4, 2026-06-15). WAL +
        # synchronous=NORMAL is the standard low-write, crash-safe pairing: commits
        # append to the -wal and fsync only at checkpoint (not every transaction), so
        # the 60s tick costs far fewer fsyncs/page-rewrites than rollback-journal mode.
        # NORMAL is corruption-safe in WAL (only the last commit can be lost on power
        # loss) — fine for the watchdog records that dominate write volume.
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = NORMAL")
        yield connection
        connection.commit()
    except BaseException:
        # Roll back like sqlite3's own __exit__; guard it so it can never mask the original error.
        try:
            connection.rollback()
        except Exception:
            pass
        raise
    finally:
        connection.close()


@contextmanager
def connect_readonly(database_path: Path, *, immutable: bool | None = None):
    """A strictly READ-ONLY SQLite connection.

    Opened ``mode=ro`` + ``PRAGMA query_only = ON`` — it can never mutate the database. ``immutable`` decides whether
    SQLite also avoids creating/using the ``-wal``/``-shm`` sidecars:

    - ``None`` (default) → **auto**: ``immutable=1`` only when the file is **not writable AND has no live ``-wal``**
      (a checkpointed single-file backup / read-only media — reading the main file directly is then safe AND
      side-effect-free). Otherwise plain ``mode=ro``: a writable live db (concurrent writer → keep WAL coordination
      for a consistent snapshot), OR a non-writable file that still carries uncheckpointed ``-wal`` data — ``mode=ro``
      reads that committed WAL state (or fails explicitly on read-only media), never a silent stale/missing-table
      misread.
    - ``True`` / ``False`` → force it.

    So inspection of a writable live db is snapshot-safe, and inspection of a read-only file (forensics, package
    verification, restore checks, read-only media) creates **no** sidecars and works even in a non-writable
    directory. The caller must ensure the file exists. Yields a ``Row``-factory connection; always closes it. Use
    ``connect`` for anything that may write.
    """
    if immutable is None:
        # immutable=1 is side-effect-free but reads ONLY the main file — safe ONLY when there is no uncheckpointed
        # WAL data for it to miss. Auto-enable it for a non-writable file with no live `-wal`;
        # otherwise keep plain mode=ro, which reads the committed `-wal` state (or fails explicitly on read-only
        # media) rather than silently reading a stale/missing-table main file.
        wal = database_path.parent / (database_path.name + "-wal")
        has_wal_data = wal.exists() and wal.stat().st_size > 0
        immutable = (not os.access(database_path, os.W_OK)) and not has_wal_data
        if has_wal_data:
            # a WAL bundle read via mode=ro needs a `-shm`; if it is missing AND the directory is
            # not writable, SQLite cannot create it and raises a bare "unable to open database file". Fail early
            # with the contract instead of a surprising low-level error.
            shm = database_path.parent / (database_path.name + "-shm")
            if not shm.exists() and not os.access(database_path.parent, os.W_OK):
                raise ReadOnlyDatabaseError(
                    f"{database_path}: read-only WAL database is missing its -shm sidecar and the directory is not "
                    "writable — checkpoint it (PRAGMA wal_checkpoint) before read-only media, or include the -shm")
    # build the URI from an ESCAPED filesystem URI. Raw f"file:{path}?mode=ro" lets a path containing
    # `?`/`#`/`%` be parsed as the URI query/fragment, silently opening a DIFFERENT file. as_uri() percent-encodes the
    # path (and requires an absolute one, which resolve() guarantees).
    uri = database_path.resolve().as_uri() + "?mode=ro" + ("&immutable=1" if immutable else "")
    connection = sqlite3.connect(uri, uri=True)
    try:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only = ON")
        if not immutable:
            connection.execute("PRAGMA busy_timeout = 5000")
        yield connection
    finally:
        connection.close()






def _apply_one_migration(connection: sqlite3.Connection, name: str, sql: str) -> bool:
    """Apply ONE migration atomically: its DDL **and** its ``schema_migrations`` row
    commit together, or roll back together.

    ``executescript`` implicitly COMMITs any pending work *before* running, so a multi-statement migration would
    otherwise commit incrementally — a mid-migration failure leaving a partial schema with no version row (then a
    re-``init`` re-runs it and hits "table already exists"). Prefixing ``BEGIN;`` makes executescript start a
    transaction it does NOT close (no trailing COMMIT in the script), so the version INSERT lands in the same
    transaction and a single ``commit`` seals both. Any failure → ``rollback`` discards the partial migration,
    leaving the db exactly as before so the next ``init`` retries cleanly. (No migration contains a trigger, so the
    ``BEGIN;`` prefix never collides with in-script transaction control — verified.)
    """
    if "'" in name or ";" in name:
        raise ValueError(f"unsafe migration name: {name!r}")
    try:
        connection.executescript(f"BEGIN IMMEDIATE;\nINSERT INTO schema_migrations (version) VALUES ('{name}');\n{sql}")
        connection.commit()
        return True
    except sqlite3.IntegrityError:
        connection.rollback()
        if _migration_applied(connection, name):
            return False          # another process applied it while we waited for the write lock
        raise
    except Exception:
        connection.rollback()
        raise


def initialize_database(database_path: Path) -> InitResult:
    from ledger_memory.emission import emit_audit_and_event  # local import to avoid circular dependency

    database_path.parent.mkdir(parents=True, exist_ok=True)
    applied: list[str] = []

    with connect(database_path) as connection:
        _ensure_migration_table(connection)
        for migration_name, migration_sql in _load_migrations():
            if _migration_applied(connection, migration_name):
                continue
            if _apply_one_migration(connection, migration_name, migration_sql):
                applied.append(migration_name)
        _seed_owner_actor(connection)
        _seed_system_actor(connection)

        if applied:
            owner_row = connection.execute(
                "SELECT id FROM actors WHERE name = 'owner' LIMIT 1"
            ).fetchone()
            owner_actor_id = int(owner_row["id"]) if owner_row is not None else None
            emit_audit_and_event(
                connection,
                action_type="database.initialized",
                target_type="database",
                target_id=None,
                actor_id=owner_actor_id,
                event_type="database.initialized",
                summary="Database initialized",
            )


    return InitResult(path=database_path, applied_migrations=tuple(applied))


def _seed_owner_actor(connection: sqlite3.Connection) -> None:
    existing = connection.execute(
        "SELECT id FROM actors WHERE name = 'owner' LIMIT 1"
    ).fetchone()
    if existing is not None:
        return
    connection.execute(
        """
        INSERT INTO actors (name, actor_type, notes)
        VALUES ('owner', 'human', 'Default owner actor')
        """
    )


def _seed_system_actor(connection: sqlite3.Connection) -> None:
    """Seed the ledger-system actor used by the deterministic tick runtime.

    Inserted idempotently: no-op if the actor already exists.
    """
    existing = connection.execute(
        "SELECT id FROM actors WHERE name = 'ledger-system' LIMIT 1"
    ).fetchone()
    if existing is not None:
        return
    connection.execute(
        """
        INSERT INTO actors (name, actor_type, notes)
        VALUES ('ledger-system', 'system', 'System actor for automated maintenance')
        """
    )


def database_report(database_path: Path) -> dict[str, object]:
    if not database_path.exists():
        return {
            "path": database_path,
            "exists": False,
            "migration_count": 0,
            "table_counts": {},
        }

    with connect_readonly(database_path) as connection:  # F006: pure read (exist-guarded above)
        migration_count = _table_count(connection, "schema_migrations")
        table_counts = {
            table_name: _table_count(connection, table_name)
            for table_name in AUTHORITATIVE_TABLES
            if _table_exists(connection, table_name)
        }

    return {
        "path": database_path,
        "exists": True,
        "migration_count": migration_count,
        "table_counts": table_counts,
    }


def database_is_initialized(database_path: Path) -> bool:
    if not database_path.exists():
        return False

    try:
        with connect_readonly(database_path) as connection:  # F006: pure read (exist-guarded above)
            if not _table_exists(connection, "schema_migrations"):
                return False
            return all(_table_exists(connection, table) for table in REQUIRED_TABLES)
    except sqlite3.DatabaseError:
        return False


def migration_status(database_path: Path) -> tuple[str, list[str]]:
    """Three-state migration check, distinguishing the BENIGN "behind" case from the broken
    "uninitialized" case so the gate can be honest + self-heal a consumer db.

    Returns ``(state, pending)`` where ``state`` is:
    - ``"uninitialized"`` — no db file / no ``schema_migrations`` / nothing applied (genuinely needs init-db);
    - ``"behind"`` — ≥1 migration applied but newer ones are pending (additive, lossless, self-healable);
    - ``"current"`` — every available migration applied AND every ``REQUIRED_TABLE`` present.
    ``pending`` is the list of available-but-unapplied migration filenames.
    """
    available = [name for name, _ in _load_migrations()]
    if not database_path.exists():
        return ("uninitialized", available)
    try:
        with connect_readonly(database_path) as connection:  # F006: pure read (exist-guarded above)
            if not _table_exists(connection, "schema_migrations"):
                return ("uninitialized", available)
            applied = {
                str(row[0])
                for row in connection.execute("SELECT version FROM schema_migrations").fetchall()
            }
            pending = [name for name in available if name not in applied]
            if not applied:
                return ("uninitialized", pending)
            if pending:
                return ("behind", pending)
            if all(_table_exists(connection, table) for table in REQUIRED_TABLES):
                return ("current", [])
            # All migrations applied yet a required table is missing → genuinely broken (not a pending
            # migration we can apply); force an explicit init rather than silently "self-heal" nothing.
            return ("uninitialized", [])
    except sqlite3.DatabaseError:
        return ("uninitialized", available)


def _ensure_migration_table(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version TEXT PRIMARY KEY,
            applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )


def _load_migrations() -> list[tuple[str, str]]:
    """the CATALOG decides what exists, not a directory listing.

    This used to enumerate whatever `importlib.resources` exposed from the editable checkout, which made
    "available migrations" a listing of a working tree: any `.sql` file appearing there — committed or not,
    finished or not — instantly moved every consumer database's pending set, and `_database_ready` gates
    177 call sites on that. Three of the last four migrations wedged the owner's store this way,
    one of them an untracked file that existed in a single checkout.

    `load_catalog` returns only manifest-listed files, in manifest order, with their raw bytes
    hash-verified. An unlisted file is ignored here and surfaced by `catalog_status` instead.
    """
    from ledger_memory.migration_catalog import load_catalog

    return load_catalog()


def _migration_applied(connection: sqlite3.Connection, version: str) -> bool:
    row = connection.execute(
        "SELECT 1 FROM schema_migrations WHERE version = ?",
        (version,),
    ).fetchone()
    return row is not None


def _table_exists(connection: sqlite3.Connection, table_name: str) -> bool:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table_name,),
    ).fetchone()
    return row is not None


def _table_count(connection: sqlite3.Connection, table_name: str) -> int:
    if not _table_exists(connection, table_name):
        return 0
    row = connection.execute(f"SELECT COUNT(*) AS count FROM {table_name}").fetchone()
    return int(row["count"])
