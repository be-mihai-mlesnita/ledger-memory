from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import Path

from ledger_memory.db import connect, connect_readonly
from ledger_memory.facts import CURRENT_FACT_SQL
from ledger_memory.tiers import TIER_OWNER_SENSITIVE


@dataclass(frozen=True)
class FtsSearchResult:
    record_type: str
    record_id: int
    title: str
    snippet: str


@dataclass(frozen=True)
class SearchIndexStatus:
    indexed_rows: int
    expected_rows: int
    metadata_rows: int
    missing_rows: int
    orphaned_rows: int
    stale_rows: int
    latest_rebuild_id: int | None
    latest_rebuild_status: str | None
    latest_rebuild_started_at: str | None
    latest_rebuild_completed_at: str | None
    latest_rebuild_indexed_rows: int | None
    empty: bool
    never_rebuilt: bool
    fresh: bool


@dataclass(frozen=True)
class SearchIndexDiffRow:
    category: str
    record_type: str
    record_id: int
    expected_hash: str | None
    actual_hash: str | None
    title: str | None


@dataclass(frozen=True)
class SearchIndexRebuildPreview:
    scope_type: str | None
    scope_id: int | None
    expected_rows: int
    current_indexed_rows: int
    current_metadata_rows: int
    missing_rows: int
    stale_rows: int
    orphaned_rows: int
    missing_fts_rows: int
    unexpected_fts_rows: int
    would_rebuild_rows: int


@dataclass(frozen=True)
class SearchIndexIntegrityReport:
    status: SearchIndexStatus
    missing_fts_rows: int
    unexpected_fts_rows: int
    recommendation: str
    diff_rows: list[SearchIndexDiffRow]


SEARCH_INDEX_DIFF_CATEGORIES = (
    "missing",
    "stale",
    "orphaned",
    "missing_fts_row",
    "unexpected_fts_row",
    "duplicate_fts_row",  # an earlier audit
)


def _fts_content_hashes(connection, record_type=None, record_id=None) -> dict:
    """{(record_type, record_id): content-hash} computed from the ACTUAL search_fts.title/body — shared by status,
    diff, and preview so FTS-content-corruption detection cannot drift between them."""
    result: dict[tuple[str, int], str] = {}
    for row in connection.execute("SELECT record_type, record_id, title, body FROM search_fts").fetchall():
        rt, rid = str(row["record_type"]), int(row["record_id"])
        if not _matches_scope(rt, rid, record_type, record_id):
            continue
        result[(rt, rid)] = _content_hash(rt, rid, str(row["title"] or ""), str(row["body"] or ""))
    return result


class SearchIndexScopeNotFoundError(Exception):
    def __init__(self, record_type: str, record_id: int) -> None:
        self.record_type = record_type
        self.record_id = record_id
        super().__init__(f"record not found: {record_type} {record_id}")


def rebuild_search_index(
    database_path: Path,
    *,
    record_type: str | None = None,
    record_id: int | None = None,
) -> int:
    if record_id is not None and record_type is None:
        raise ValueError("record_id requires record_type")

    with connect(database_path) as connection:
        rows = [
            row
            for row in _source_rows(connection)
            if _matches_scope(row[0], row[1], record_type, record_id)
        ]
        # Read on the SAME connection as the source rows, so the tier a row is projected with is the
        # tier it had in that read — not one a concurrent write moved underneath us.
        row_tiers = source_row_tiers(connection)
        if (
            record_type is not None
            and record_id is not None
            and not rows
            and not _scope_has_derived_rows(connection, record_type, record_id)
        ):
            raise SearchIndexScopeNotFoundError(record_type, record_id)

        rebuild_cursor = connection.execute(
            """
            INSERT INTO search_index_rebuilds (status, notes)
            VALUES (?, ?)
            """,
            ("running", _scope_notes(record_type, record_id)),
        )
        rebuild_id = int(rebuild_cursor.lastrowid)

        _delete_search_index_scope(connection, "search_fts", record_type, record_id)
        _delete_search_index_scope(
            connection,
            "search_index_entries",
            record_type,
            record_id,
        )
        connection.executemany(
            """
            INSERT INTO search_fts (record_type, record_id, title, body, sensitivity_tier)
            VALUES (?, ?, ?, ?, ?)
            """,
            # FTS is a SEPARATE store from `facts`, so a tier-blind FTS lane hands
            # owner-sensitive statements verbatim to a cloud-tier caller — the "second door" the Phase A
            # red team found. The projected tier is REBUILT from the authoritative row here; a record
            # whose type carries no tier column defaults to TIER_OWNER_SENSITIVE (restrictive), never to
            # shareable, so an unclassified projection cannot leak.
            [
                (record_type, record_id, title, body,
                 row_tiers.get((record_type, record_id), TIER_OWNER_SENSITIVE))
                for record_type, record_id, title, body in rows
            ],
        )
        connection.executemany(
            """
            INSERT INTO search_index_entries (record_type, record_id, content_hash)
            VALUES (?, ?, ?)
            """,
            [
                (record_type, record_id, _content_hash(record_type, record_id, title, body))
                for record_type, record_id, title, body in rows
            ],
        )
        count = len(rows)
        connection.execute(
            """
            UPDATE search_index_rebuilds
            SET status = ?,
                completed_at = CURRENT_TIMESTAMP,
                indexed_rows = ?
            WHERE id = ?
            """,
            ("completed", count, rebuild_id),
        )

    return count


def get_search_index_status(database_path: Path) -> SearchIndexStatus:
    with connect_readonly(database_path) as connection:  # pure read (integrity composes this)
        source_rows = _source_rows(connection)
        expected_hashes = {
            (record_type, record_id): _content_hash(record_type, record_id, title, body)
            for record_type, record_id, title, body in source_rows
        }
        indexed_rows = _count_rows(connection, "search_fts")
        metadata = {
            (str(row["record_type"]), int(row["record_id"])): str(row["content_hash"])
            for row in connection.execute(
                """
                SELECT record_type, record_id, content_hash
                FROM search_index_entries
                """
            ).fetchall()
        }
        # hash the ACTUAL search_fts.title/body (shared helper) so a corrupted FTS row is detected.
        fts_hashes = _fts_content_hashes(connection)
        latest_rebuild = connection.execute(
            """
            SELECT id, status, started_at, completed_at, indexed_rows
            FROM search_index_rebuilds
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()

    expected_keys = set(expected_hashes)
    metadata_keys = set(metadata)
    missing_rows = len(expected_keys - metadata_keys)
    orphaned_rows = len(metadata_keys - expected_keys)
    stale_rows = sum(
        1
        for key in expected_keys & metadata_keys
        # stale if the SOURCE drifted from the index, OR the FTS row's own content no longer
        # matches its stored hash (corruption / a missing FTS row for an indexed key).
        if expected_hashes[key] != metadata[key] or fts_hashes.get(key) != metadata[key]
    )
    expected_rows = len(expected_hashes)
    metadata_rows = len(metadata)
    never_rebuilt = latest_rebuild is None
    empty = indexed_rows == 0
    fresh = (
        not never_rebuilt
        and missing_rows == 0
        and orphaned_rows == 0
        and stale_rows == 0
        and indexed_rows == expected_rows
        and metadata_rows == expected_rows
    )

    return SearchIndexStatus(
        indexed_rows=indexed_rows,
        expected_rows=expected_rows,
        metadata_rows=metadata_rows,
        missing_rows=missing_rows,
        orphaned_rows=orphaned_rows,
        stale_rows=stale_rows,
        latest_rebuild_id=None if latest_rebuild is None else int(latest_rebuild["id"]),
        latest_rebuild_status=None if latest_rebuild is None else str(latest_rebuild["status"]),
        latest_rebuild_started_at=None
        if latest_rebuild is None
        else str(latest_rebuild["started_at"]),
        latest_rebuild_completed_at=None
        if latest_rebuild is None or latest_rebuild["completed_at"] is None
        else str(latest_rebuild["completed_at"]),
        latest_rebuild_indexed_rows=None
        if latest_rebuild is None
        else int(latest_rebuild["indexed_rows"]),
        empty=empty,
        never_rebuilt=never_rebuilt,
        fresh=fresh,
    )


def preview_search_index_rebuild(
    database_path: Path,
    *,
    record_type: str | None = None,
    record_id: int | None = None,
) -> SearchIndexRebuildPreview:
    if record_id is not None and record_type is None:
        raise ValueError("record_id requires record_type")

    with connect_readonly(database_path) as connection:  # pure read (preview)
        source_rows = [
            row
            for row in _source_rows(connection)
            if _matches_scope(row[0], row[1], record_type, record_id)
        ]
        expected_hashes = {
            (source_record_type, source_record_id): _content_hash(
                source_record_type,
                source_record_id,
                title,
                body,
            )
            for source_record_type, source_record_id, title, body in source_rows
        }
        metadata = {
            (str(row["record_type"]), int(row["record_id"])): str(row["content_hash"])
            for row in connection.execute(
                """
                SELECT record_type, record_id, content_hash
                FROM search_index_entries
                """
            ).fetchall()
            if _matches_scope(
                str(row["record_type"]),
                int(row["record_id"]),
                record_type,
                record_id,
            )
        }
        fts_keys = {
            (str(row["record_type"]), int(row["record_id"]))
            for row in connection.execute(
                """
                SELECT record_type, record_id
                FROM search_fts
                """
            ).fetchall()
            if _matches_scope(
                str(row["record_type"]),
                int(row["record_id"]),
                record_type,
                record_id,
            )
        }
        # detect FTS-content corruption in the preview too (shared helper with status/diff).
        fts_hashes = _fts_content_hashes(connection, record_type, record_id)

    expected_keys = set(expected_hashes)
    metadata_keys = set(metadata)
    missing_rows = len(expected_keys - metadata_keys)
    orphaned_rows = len(metadata_keys - expected_keys)
    stale_rows = sum(
        1
        for key in expected_keys & metadata_keys
        # source drift OR FTS-content corruption (matches status/diff).
        if expected_hashes[key] != metadata[key] or fts_hashes.get(key) != metadata[key]
    )

    return SearchIndexRebuildPreview(
        scope_type=record_type,
        scope_id=record_id,
        expected_rows=len(expected_hashes),
        current_indexed_rows=len(fts_keys),
        current_metadata_rows=len(metadata),
        missing_rows=missing_rows,
        stale_rows=stale_rows,
        orphaned_rows=orphaned_rows,
        missing_fts_rows=len(metadata_keys - fts_keys),
        unexpected_fts_rows=len(fts_keys - metadata_keys),
        would_rebuild_rows=len(expected_hashes),
    )


def get_search_index_diff(database_path: Path) -> list[SearchIndexDiffRow]:
    with connect_readonly(database_path) as connection:  # pure read (integrity composes this)
        source_rows = _source_rows(connection)
        source = {
            (record_type, record_id): (
                _content_hash(record_type, record_id, title, body),
                title,
            )
            for record_type, record_id, title, body in source_rows
        }
        metadata = {
            (str(row["record_type"]), int(row["record_id"])): str(row["content_hash"])
            for row in connection.execute(
                """
                SELECT record_type, record_id, content_hash
                FROM search_index_entries
                """
            ).fetchall()
        }
        fts_rows = {}
        fts_hashes = {}  # hash the actual FTS content to detect a corrupted-but-present FTS row
        fts_counts: dict[tuple[str, int], int] = {}  # count rows per key to detect duplicates
        for row in connection.execute(
            "SELECT record_type, record_id, title, body FROM search_fts"
        ).fetchall():
            key = (str(row["record_type"]), int(row["record_id"]))
            fts_rows[key] = str(row["title"])
            fts_hashes[key] = _content_hash(key[0], key[1], str(row["title"] or ""), str(row["body"] or ""))
            fts_counts[key] = fts_counts.get(key, 0) + 1

    source_keys = set(source)
    metadata_keys = set(metadata)
    fts_keys = set(fts_rows)
    diff_rows: list[SearchIndexDiffRow] = []

    for key in source_keys - metadata_keys:
        record_type, record_id = key
        actual_hash, title = source[key]
        diff_rows.append(
            SearchIndexDiffRow(
                category="missing",
                record_type=record_type,
                record_id=record_id,
                expected_hash=None,
                actual_hash=actual_hash,
                title=title,
            )
        )

    for key in source_keys & metadata_keys:
        actual_hash, title = source[key]
        expected_hash = metadata[key]
        if expected_hash != actual_hash:
            record_type, record_id = key
            diff_rows.append(
                SearchIndexDiffRow(
                    category="stale",
                    record_type=record_type,
                    record_id=record_id,
                    expected_hash=expected_hash,
                    actual_hash=actual_hash,
                    title=title,
                )
            )

    for key in metadata_keys - source_keys:
        record_type, record_id = key
        diff_rows.append(
            SearchIndexDiffRow(
                category="orphaned",
                record_type=record_type,
                record_id=record_id,
                expected_hash=metadata[key],
                actual_hash=None,
                title=None,
            )
        )

    for key in metadata_keys - fts_keys:
        record_type, record_id = key
        source_hash_title = source.get(key)
        diff_rows.append(
            SearchIndexDiffRow(
                category="missing_fts_row",
                record_type=record_type,
                record_id=record_id,
                expected_hash=metadata[key],
                actual_hash=None if source_hash_title is None else source_hash_title[0],
                title=None if source_hash_title is None else source_hash_title[1],
            )
        )

    for key in fts_keys - metadata_keys:
        record_type, record_id = key
        diff_rows.append(
            SearchIndexDiffRow(
                category="unexpected_fts_row",
                record_type=record_type,
                record_id=record_id,
                expected_hash=None,
                actual_hash=None,
                title=fts_rows[key],
            )
        )

    for key in metadata_keys & fts_keys:
        # the FTS row is present and its SOURCE is unchanged, but the FTS row's own content hash no
        # longer matches the stored hash → the FTS content was corrupted. (Source-stale / orphaned keys are already
        # reported above, so skip anything whose source no longer matches the stored hash.)
        if fts_hashes.get(key) == metadata[key]:
            continue
        if source.get(key, (None,))[0] != metadata[key]:
            continue
        record_type, record_id = key
        diff_rows.append(
            SearchIndexDiffRow(
                category="stale",
                record_type=record_type,
                record_id=record_id,
                expected_hash=metadata[key],
                actual_hash=fts_hashes.get(key),
                title=source[key][1],
            )
        )

    for key, count in fts_counts.items():
        # a key with >1 search_fts row is a DUPLICATE — the dict/set view hid it, so status read
        # "not fresh" (indexed > expected) with an empty diff. Report it explicitly so the repair target is visible.
        if count > 1:
            record_type, record_id = key
            diff_rows.append(
                SearchIndexDiffRow(
                    category="duplicate_fts_row",
                    record_type=record_type,
                    record_id=record_id,
                    expected_hash=None,
                    actual_hash=None,
                    title=fts_rows.get(key),
                )
            )

    category_rank = {
        category: index for index, category in enumerate(SEARCH_INDEX_DIFF_CATEGORIES)
    }
    return sorted(
        diff_rows,
        key=lambda row: (
            category_rank[row.category],
            row.record_type,
            row.record_id,
        ),
    )


def search_index_diff_row_to_dict(row: SearchIndexDiffRow) -> dict[str, object]:
    return {
        "category": row.category,
        "record_type": row.record_type,
        "record_id": row.record_id,
        "expected_hash": row.expected_hash,
        "actual_hash": row.actual_hash,
        "title": row.title,
    }


def get_search_index_integrity_report(
    database_path: Path,
    *,
    limit: int = 20,
    category: str | None = None,
) -> SearchIndexIntegrityReport:
    status = get_search_index_status(database_path)
    all_diff_rows = get_search_index_diff(database_path)
    missing_fts_rows = sum(1 for row in all_diff_rows if row.category == "missing_fts_row")
    unexpected_fts_rows = sum(
        1 for row in all_diff_rows if row.category == "unexpected_fts_row"
    )

    diff_rows = all_diff_rows
    if category is not None:
        diff_rows = [row for row in diff_rows if row.category == category]
    diff_rows = diff_rows[: max(limit, 0)]

    if status.never_rebuilt:
        recommendation = "run_rebuild_search_index_manually"
    elif status.fresh and not all_diff_rows:
        recommendation = "no_action_needed"
    else:
        recommendation = "review_diff_or_rebuild_manually"

    return SearchIndexIntegrityReport(
        status=status,
        missing_fts_rows=missing_fts_rows,
        unexpected_fts_rows=unexpected_fts_rows,
        recommendation=recommendation,
        diff_rows=diff_rows,
    )


def search_index_integrity_report_to_dict(
    report: SearchIndexIntegrityReport,
) -> dict[str, object]:
    status = report.status
    return {
        "fresh": status.fresh,
        "never_rebuilt": status.never_rebuilt,
        "indexed_rows": status.indexed_rows,
        "expected_rows": status.expected_rows,
        "metadata_rows": status.metadata_rows,
        "missing_rows": status.missing_rows,
        "stale_rows": status.stale_rows,
        "orphaned_rows": status.orphaned_rows,
        "missing_fts_rows": report.missing_fts_rows,
        "unexpected_fts_rows": report.unexpected_fts_rows,
        "latest_rebuild": {
            "id": status.latest_rebuild_id,
            "status": status.latest_rebuild_status,
            "started_at": status.latest_rebuild_started_at,
            "completed_at": status.latest_rebuild_completed_at,
            "indexed_rows": status.latest_rebuild_indexed_rows,
        },
        "recommendation": report.recommendation,
        "diff": [search_index_diff_row_to_dict(row) for row in report.diff_rows],
    }


def search_index_row_count(database_path: Path) -> int:
    with connect_readonly(database_path) as connection:  # pure read (row count)
        row = connection.execute("SELECT COUNT(*) AS row_count FROM search_fts").fetchone()
    return int(row["row_count"])


def fts_search(database_path: Path, query: str) -> list[FtsSearchResult]:
    """The literal lane: precise AND first, widened to OR only when the AND finds NOTHING.

    Measured on the live store 2026-08-01: `"first" AND "heart" AND "problem" AND "started"` matched 0
    records while `"heart"` alone matched 7. Every added term makes a precisely-phrased memory LESS
    findable, so a natural question silently lost the whole lexical lane and recall fell back to
    embeddings — where a question ranks nearest to other questions, not to its answer.

    Precision is still preferred: whenever the AND matches anything at all, its rows are returned
    unchanged. The widening is one extra query on the empty case, never a merge of both, and FTS5's
    `bm25` ranking puts the rarer term first without any weighting of ours.
    """
    fts_query = _literal_fts_query(query)
    sql = """
            SELECT record_type, record_id, title, body
            FROM search_fts
            WHERE search_fts MATCH ?
            ORDER BY rank
            """
    with connect_readonly(database_path) as connection:  # pure read (fts search)
        rows = connection.execute(sql, (fts_query,)).fetchall()
        if not rows:
            widened = _widened_fts_query(query)
            if widened is not None:
                rows = connection.execute(sql, (widened,)).fetchall()

    return [
        FtsSearchResult(
            record_type=str(row["record_type"]),
            record_id=int(row["record_id"]),
            title=str(row["title"]),
            snippet=str(row["body"]),
        )
        for row in rows
    ]


# The salient-term filter shared with `about_me_recall._keywords`. It lives HERE, in the lower-level
# module, and is imported upward — two copies of a stopword list is the drift class this project keeps
# paying for, and arm C is defined as "drops the stopwords `_keywords` already drops", so a second list
# would silently make that definition false.
_STOPWORDS = frozenset(
    "the a an and or of to in on for with about from is are was were do does did what which who whom whose when where "
    "why how i me my mine you your yours we our us it its this that these those have has had be been being will would "
    "should could can may might not no yes please tell know".split())

# C3 — a SEPARATE Romanian function-word set, deliberately NEVER merged into `_STOPWORDS`
# above. Measured defect: `_salient_query_tokens` filtered only the English set, so a Romanian query's
# function words (`mă`, `și`, `de`, `la`, `nu`...) became MANDATORY AND terms in the literal FTS query
# — precision-AND collapsed to zero, and the OR-widening flooded with folded stopwords instead of
# content. Kept apart because the two lists collide: `mai` and `la` are legitimate English tokens (a
# proper noun, a place name like "LA") that must keep matching in an English query — merging the sets
# would silently break that. `about_me_recall._keywords` imports this SAME frozenset for the identical
# reason `_STOPWORDS` is shared rather than duplicated.
_STOPWORDS_RO = frozenset(
    "și să de la cu ce nu mai este sunt fost am mă îmi mi te vă ne se un o el ea ei noi voi pentru din pe "
    "care cum când unde dacă dar sau ca al ai ale lui".split())


def source_row_tiers(connection) -> dict[tuple[str, int], int]:
    """`{(record_type, record_id): sensitivity_tier}` for every tiered content table.

    DELIBERATELY separate from `_source_rows` rather than a fifth tuple element. `_source_rows` is
    consumed positionally in five modules and ~190 tests; widening its arity broke all of them at once,
    which is a ripple no privacy feature should require. The tier is looked up alongside instead, so the
    projection gains a filter column while every existing consumer keeps working byte-identically.

    A record type absent from this map has no tier column (see `ledger_memory.db.UNTIERED_TABLE_REASONS`) and is
    therefore not projected into a tier-filtered lane; callers default such rows to the RESTRICTIVE tier
    rather than assuming they are shareable.
    """
    from ledger_memory.db import TIERED_TABLES

    # The index's record_type strings map 1:1 onto table names for every tiered content table.
    record_type_tables = {
        "event": "events",
        "fact": "facts",
        "decision": "decisions", "context_package": "context_packages", "actor": "actors",
        "relationship": "relationships", "audit_entry": "audit_entries",
    }
    tiers: dict[tuple[str, int], int] = {}
    for record_type, table in record_type_tables.items():
        if table not in TIERED_TABLES:
            continue
        for row in connection.execute(f"SELECT id, sensitivity_tier FROM {table}").fetchall():
            tiers[(record_type, int(row["id"]))] = int(row["sensitivity_tier"])
    return tiers


def _literal_fts_query(query: str) -> str:
    """Build the FTS5 MATCH string for the literal lane.

    Previously the whole question was wrapped in quotes, so `"what are my standing guardrails"` matched
    only a record containing that exact phrase — 1 of 20 published questions, 2 rows. Arm C drops the
    stopwords and ANDs what is left (`"standing" AND "guardrails"`): 9 of 20 questions, 444 rows, at
    identical cost. Measured in `docs/audit/20260726-task-222-does-not-unlock-the-fanout.md`.

    **Every token stays individually quoted.** A bare per-token AND raised `OperationalError` on 22 of 60
    queries, because FTS5 reads unquoted `AND`/`OR`/`NOT`, `*` and punctuation as syntax. Quoting makes
    each token a literal and removes that class entirely.

    **An all-stopword question falls back to the phrase literal** rather than producing an empty MATCH.
    An empty match string is a syntax error and a bare `""` matches nothing — either way the lane would
    return zero while looking fast and healthy, which is the failure mode this codebase has been bitten by
    most often. The fallback makes the worst case *today's behaviour*, never worse.

    This narrows the gap to the keyword fan-out; it does not close it. The fan-out is an OR across separate
    searches and this is an AND, so no per-token scheme can substitute for it.
    """
    tokens = _salient_query_tokens(query)
    if not tokens:
        escaped_query = query.replace('"', '""')
        return f'"{escaped_query}"'
    return " AND ".join(tokens)


def _salient_query_tokens(query: str) -> list[str]:
    """The quoted, stopword-stripped tokens of *query*. Shared by the AND query and its OR fallback.

    an earlier fix C3: gained a `len >= 2` floor and Romanian (`_STOPWORDS_RO`) filtering alongside the
    existing English (`_STOPWORDS`) filtering — measured defect: a bare 1-2 letter fold-artifact or an
    unfiltered Romanian function word (`mă`, `și`, `de`...) became a MANDATORY AND term, collapsing
    precision-AND to zero on Romanian queries. `_literal_fts_query`/`_widened_fts_query` are unchanged;
    both call this function and inherit the fix for free.
    """
    from ledger_memory.textfold import looks_romanian

    # Review fix (2026-08-07): the RO set fires ONLY when the query carries a Romanian diacritic —
    # unconditional filtering would swallow English collision tokens ('mai'≈May, 'care', 'am', 'la')
    # and silently change English behavior, the exact thing C3 forbids.
    romanian = looks_romanian(query)
    tokens = [token.strip("?.,!'\"") for token in query.lower().split()]
    tokens = [
        token for token in tokens
        if token and len(token) >= 2 and token not in _STOPWORDS
        and not (romanian and token in _STOPWORDS_RO)
    ]
    return ['"' + token.replace('"', '""') + '"' for token in tokens]


def _widened_fts_query(query: str) -> str | None:
    """The OR widening of *query*, or None when there is nothing to widen.

    Returns None for a single salient token (the AND and the OR are the same query — re-running it would
    be a second identical scan pretending to be a fallback) and for the all-stopword case, which keeps
    arm C's phrase-literal behaviour.
    """
    tokens = _salient_query_tokens(query)
    if len(tokens) < 2:
        return None
    return " OR ".join(tokens)


def _index_types(connection) -> set[str] | None:
    """The `search.index_types` allowlist (a set of record types), or None = project everything.

    Read straight from the core DB's config table via *connection* (no separate open). Unset, empty, or
    all-whitespace value → None, i.e. index everything (default → ZERO behavior change). When set, only
    the listed record types are projected, keeping the FTS + semantic lanes knowledge-clean on a personal
    store while telemetry stays queryable structurally. an earlier fix (store-arch W3)."""
    try:
        row = connection.execute(
            "SELECT value FROM config WHERE key = 'search.index_types'"
        ).fetchone()
    except Exception:
        return None
    if row is None:
        return None
    raw = (row["value"] or "").strip()
    if not raw:
        return None
    parsed = {t.strip() for t in raw.split(",") if t.strip()}
    return parsed or None   # an all-comma/whitespace value ("," etc.) → all, never "index nothing"


def _episodic_salience_on(connection) -> bool:
    """is selective episodic embedding enabled? config `search.episodic_salience`
    (on/1/true/yes). Default off → no salient-episode projection, ZERO behavior change."""
    try:
        row = connection.execute(
            "SELECT value FROM config WHERE key = 'search.episodic_salience'"
        ).fetchone()
    except Exception:
        return False
    return bool(row) and (row["value"] or "").strip().lower() in {"on", "1", "true", "yes"}


# the salience rules for an event type, as substrings matched case-insensitively. Previously
# spelled as five `lower(event_type) LIKE '%…%'` clauses inside one SQL statement.
_SALIENT_EVENT_SUBSTRINGS = ("fail", "error", "escalat", "recover", "cancel")


def _salient_episodes(connection) -> set[tuple[str, int]]:
    """the (record_type, record_id) episodes worth semantically recalling, per
    the §5 v1 rules. Used to add salient telemetry back into a knowledge-only projection. Each query is
    defensive (→ skipped on error) so a partial DB never crashes the projection. Low-entropy boilerplate
    (tick.completed, run started/finished) is excluded by construction."""
    salient: set[tuple[str, int]] = set()

    def _collect(sql, mapper):
        try:
            for row in connection.execute(sql).fetchall():
                salient.add(mapper(row))
        except Exception:
            pass

    # escalation / recovery / cancellation / failure events.
    # the predicate is a property of the TYPE, not of the row, and `event_type` has ~82 distinct
    # values against ~173k rows. Five leading-wildcard LIKEs wrapped in lower() cannot use an index and
    # recompute lower() per row per pattern — 85 ms. Resolving the types once and selecting by equality is
    # 4 ms for an identical id set. The five substring rules MOVE; they do not change.
    try:
        event_types = [
            row["event_type"]
            for row in connection.execute("SELECT DISTINCT event_type FROM events").fetchall()
        ]
        salient_types = [
            event_type
            for event_type in event_types
            if any(needle in (event_type or "").lower() for needle in _SALIENT_EVENT_SUBSTRINGS)
        ]
        # No matching type selects NOTHING. An empty IN () is a syntax error, and omitting the clause would
        # silently select every event — the failure mode worth being explicit about.
        if salient_types:
            placeholders = ",".join("?" * len(salient_types))
            for row in connection.execute(
                f"SELECT id FROM events WHERE event_type IN ({placeholders})", salient_types
            ).fetchall():
                salient.add(("event", int(row["id"])))
    except Exception:
        pass  # same defensive posture as _collect: a partial DB never crashes the projection
    # records that are evidence for a fact
    _collect(
        "SELECT evidence_type, evidence_id FROM fact_evidence",
        lambda r: (str(r["evidence_type"]), int(r["evidence_id"])),
    )
    return salient


def _episodic_dedup_on(connection) -> bool:
    """is offline episodic dedup enabled? config `search.episodic_dedup`
    (on/1/true/yes). Default off → salient episodes project exactly as in 0321 (ZERO behavior change)."""
    try:
        row = connection.execute(
            "SELECT value FROM config WHERE key = 'search.episodic_dedup'"
        ).fetchone()
    except Exception:
        return False
    return bool(row) and (row["value"] or "").strip().lower() in {"on", "1", "true", "yes"}


def _norm(text) -> str:
    """Lowercase + whitespace-collapse for exact-content dedup keys."""
    return " ".join(str(text or "").lower().split())


def _dedup_episodes(rows: list[tuple[str, int, str, str]]) -> list[tuple[str, int, str, str]]:
    """collapse EXACT content-duplicate episodes (same record_type + normalized title + body) to a
    single representative: the most recent (highest id). Returned id-sorted for stable output. Only exact
    same-type same-content rows merge; semantic near-dup is the deferred model half."""
    best: dict[tuple[str, str, str], tuple[int, tuple[str, int, str, str]]] = {}
    for row in rows:
        key = (row[0], _norm(row[2]), _norm(row[3]))
        rid = int(row[1])
        if key not in best or rid > best[key][0]:
            best[key] = (rid, row)
    return [row for _, row in sorted(best.values(), key=lambda pair: pair[0])]


def _id_filter(ids: set[int] | None) -> tuple[str, list]:
    """an `id IN (…)` clause, or an empty clause when *ids* is None (fetch everything).

    An EMPTY set is not None: it means "no rows are wanted", and must produce a clause that matches nothing
    rather than silently falling back to a full scan.
    """
    if ids is None:
        return "", []
    ordered = sorted(ids)
    if not ordered:
        return "WHERE 0", []
    return f"WHERE id IN ({','.join('?' * len(ordered))})", ordered


def _source_rows(connection) -> list[tuple[str, int, str, str]]:
    # (record_type, builder) — built at call time so the builder names resolve regardless of file order.
    # gated the huge telemetry tables out; an earlier fix then re-admitted their SALIENT rows,
    # and until an earlier fix did so by scanning the whole table and filtering in Python. The gated
    # builders now take an id set and filter in SQL.
    builders = (
        ("event", _event_rows),
        ("fact", _fact_rows),
        ("decision", _decision_rows),
        ("context_package", _context_package_rows),
        ("actor", _actor_rows),
        ("relationship", _relationship_rows),
        ("audit_entry", _audit_entry_rows),
    )
    allowed = _index_types(connection)   # None = all (default); else only these record types
    # when an allowlist is active AND episodic salience is on, add the SALIENT rows of the
    # gated-out types back in (knowledge + salient episodes). Empty otherwise → no extra scan, no change.
    salient = (
        _salient_episodes(connection)
        if (allowed is not None and _episodic_salience_on(connection))
        else set()
    )
    # dedup the salient episodes only when salience AND dedup are both on. Off → extend inline,
    # byte-identical to 0321.
    dedup = bool(salient) and _episodic_dedup_on(connection)
    rows: list[tuple[str, int, str, str]] = []
    salient_rows: list[tuple[str, int, str, str]] = []
    for record_type, builder in builders:
        if allowed is None or record_type in allowed:
            rows.extend(builder(connection))
        elif salient:
            # select ONLY the salient ids. Previously `builder(connection)` ran in full and was
            # filtered here, so a gated type still scanned its whole table — 343,446 rows built to keep 232,
            # ~63% of every search's freshness check. Same rows, same order; only the fetch changed.
            wanted = {int(rid) for rtype, rid in salient if rtype == record_type}
            selected = builder(connection, wanted)
            (salient_rows if dedup else rows).extend(selected)
    if dedup:
        rows.extend(_dedup_episodes(salient_rows))
    return rows


def _content_hash(record_type: str, record_id: int, title: str, body: str) -> str:
    hash_input = "\0".join((record_type, str(record_id), title, body))
    return hashlib.sha256(hash_input.encode("utf-8")).hexdigest()


def _count_rows(connection, table_name: str) -> int:
    row = connection.execute(f"SELECT COUNT(*) AS row_count FROM {table_name}").fetchone()
    return int(row["row_count"])


def _matches_scope(
    record_type: str,
    record_id: int,
    scope_type: str | None,
    scope_id: int | None,
) -> bool:
    if scope_type is not None and record_type != scope_type:
        return False
    if scope_id is not None and record_id != scope_id:
        return False
    return True


def _scope_notes(record_type: str | None, record_id: int | None) -> str | None:
    if record_type is None:
        return None
    if record_id is None:
        return f"scope_type={record_type}"
    return f"scope_type={record_type} scope_id={record_id}"


def _delete_search_index_scope(
    connection,
    table_name: str,
    record_type: str | None,
    record_id: int | None,
) -> None:
    if record_type is None:
        connection.execute(f"DELETE FROM {table_name}")
        return
    if record_id is None:
        connection.execute(f"DELETE FROM {table_name} WHERE record_type = ?", (record_type,))
        return
    connection.execute(
        f"DELETE FROM {table_name} WHERE record_type = ? AND record_id = ?",
        (record_type, record_id),
    )


def _scope_has_derived_rows(connection, record_type: str, record_id: int) -> bool:
    fts_row = connection.execute(
        """
        SELECT 1
        FROM search_fts
        WHERE record_type = ? AND record_id = ?
        LIMIT 1
        """,
        (record_type, record_id),
    ).fetchone()
    metadata_row = connection.execute(
        """
        SELECT 1
        FROM search_index_entries
        WHERE record_type = ? AND record_id = ?
        LIMIT 1
        """,
        (record_type, record_id),
    ).fetchone()
    return fts_row is not None or metadata_row is not None




def _event_rows(connection, ids: set[int] | None = None) -> list[tuple[str, int, str, str]]:
    # when *ids* is given, filter in SQL. The salience branch used to call this with no filter
    # and discard 99%+ of the result in Python — events alone built 169k rows to keep 0.
    where, params = _id_filter(ids)
    rows = connection.execute(
        f"""
        SELECT id, event_type, summary
        FROM events
        {where}
        ORDER BY id
        """,
        params,
    ).fetchall()
    return [
        ("event", int(row["id"]), str(row["event_type"]), _join(row["summary"]))
        for row in rows
    ]










def _fact_rows(connection, ids: set[int] | None = None) -> list[tuple[str, int, str, str]]:
    # index ONLY active facts — match the note projection (projection.py filters
    # status='active'). A non-active fact (proposed/rejected/superseded/disputed) must not leak into ANY
    # search lane (this row-builder feeds the FTS lane AND, via search_index._source_rows, the semantic
    # lane + unified_search). Enforced cross-lane by test_non_active_facts_absent_from_all_search_lanes.
    # CURRENT_FACT_SQL additionally excludes a pointer-superseded dedup member (closing
    # the pre-existing 27-fact leak — a member keeps status='active' but is no longer "current").
    # this builder already has a WHERE, so the id filter is appended with AND rather than
    # replacing it — the CURRENT_FACT_SQL predicate is load-bearing and must
    # never be dropped.
    import hashlib

    def content_sha256(statement: str) -> str:
        return hashlib.sha256(statement.encode("utf-8")).hexdigest()

    id_where, params = _id_filter(ids)
    extra = id_where.replace("WHERE ", "AND ", 1) if id_where else ""
    # correlated SCALAR subqueries, not a JOIN — `fact_glosses` has its own `id` column, and
    # a JOIN would make the `id_where`/CURRENT_FACT_SQL bare-column references above ambiguous (two
    # tables, one `id`/no shared columns otherwise). A scalar subquery keeps `facts` the ONLY table in
    # the FROM clause, so every bare column reference (this function's own, and every caller's `--type
    # fact --id N` filter) keeps resolving exactly as before — zero behavior change for every other
    # caller of this query shape. `fact_id` is UNIQUE in `fact_glosses` (the baseline schema's), so at most one
    # row backs each subquery — no fan-out risk.
    rows = connection.execute(
        f"""
        SELECT id, fact_type, statement, status,
               (SELECT gloss FROM fact_glosses WHERE fact_id = facts.id) AS gloss,
               (SELECT source_statement_sha256 FROM fact_glosses WHERE fact_id = facts.id) AS gloss_sha
        FROM facts
        WHERE {CURRENT_FACT_SQL}
        {extra}
        ORDER BY id
        """,
        params,
    ).fetchall()
    result = []
    for row in rows:
        statement = row["statement"]
        parts = [statement, row["status"]]
        # A gloss rides in the indexed FTS BODY only when FRESH: its recorded source-statement hash
        # must match the statement AS READ HERE. A stale/orphaned gloss (a superseded fact's old row,
        # or a hypothetical in-place statement edit) is silently excluded — never appended, never
        # surfaced — exactly like an absent gloss. This is the ONLY place a gloss enters any search
        # lane; it never reaches `title` (still bare `fact_type`,
        # unchanged) and never reaches `tiered_read`/`about_me_recall` output, which read `facts`
        # directly and never join `fact_glosses` at all.
        if row["gloss"] is not None and row["gloss_sha"] == content_sha256(str(statement)):
            parts.append(row["gloss"])
        result.append(("fact", int(row["id"]), str(row["fact_type"]), _join(*parts)))
    return result


def _decision_rows(connection, ids: set[int] | None = None) -> list[tuple[str, int, str, str]]:
    where, params = _id_filter(ids)   # uniform builder signature; None = fetch everything
    rows = connection.execute(
        f"""
        SELECT id, title, summary, rationale, status
        FROM decisions
        {where}
        ORDER BY id
        """,
        params,
    ).fetchall()
    return [
        (
            "decision",
            int(row["id"]),
            str(row["title"]),
            _join(row["summary"], row["rationale"], row["status"]),
        )
        for row in rows
    ]


def _context_package_rows(connection, ids: set[int] | None = None) -> list[tuple[str, int, str, str]]:
    where, params = _id_filter(ids)   # uniform builder signature; None = fetch everything
    rows = connection.execute(
        f"""
        SELECT id, objective, status_summary, constraints_summary,
               facts_summary, events_summary, open_questions, risks,
               recommended_next_actions, created_at
        FROM context_packages
        {where}
        ORDER BY id
        """,
        params,
    ).fetchall()
    # a context package materializes fact CONTENT at creation time (facts_summary), and a
    # fact superseded later stays retrievable through this lane verbatim — CURRENT_FACT_SQL protects the
    # fact rows, not a summary that copied them, and 3 of 5 live packages carry no
    # fact-item links to filter by (fact 679). Mark-not-hide: every hit from this
    # builder renders as the dated historical snapshot it is. The label rides in the indexed TITLE, so
    # FTS, the semantic lane and unified search all inherit it from this one source; the LIKE lane in
    # search.py applies the identical prefix at emission.
    return [
        (
            "context_package",
            int(row["id"]),
            f"[snapshot {str(row['created_at'])[:10]}] {row['objective']}",
            _join(
                row["status_summary"],
                row["constraints_summary"],
                row["facts_summary"],
                row["events_summary"],
                row["open_questions"],
                row["risks"],
                row["recommended_next_actions"],
            ),
        )
        for row in rows
    ]


def _actor_rows(connection, ids: set[int] | None = None) -> list[tuple[str, int, str, str]]:
    where, params = _id_filter(ids)   # uniform builder signature; None = fetch everything
    rows = connection.execute(
        f"""
        SELECT id, name, actor_type, external_ref, notes
        FROM actors
        {where}
        ORDER BY id
        """,
        params,
    ).fetchall()
    return [
        (
            "actor",
            int(row["id"]),
            str(row["name"]),
            _join(row["actor_type"], row["external_ref"], row["notes"]),
        )
        for row in rows
    ]




def _relationship_rows(connection, ids: set[int] | None = None) -> list[tuple[str, int, str, str]]:
    where, params = _id_filter(ids)   # uniform builder signature; None = fetch everything
    rows = connection.execute(
        f"""
        SELECT id, source_type, source_id, target_type, target_id,
               relationship, summary
        FROM relationships
        {where}
        ORDER BY id
        """,
        params,
    ).fetchall()
    return [
        (
            "relationship",
            int(row["id"]),
            str(row["relationship"]),
            _join(
                row["source_type"],
                row["source_id"],
                row["target_type"],
                row["target_id"],
                row["summary"],
            ),
        )
        for row in rows
    ]


def _audit_entry_rows(connection, ids: set[int] | None = None) -> list[tuple[str, int, str, str]]:
    # when *ids* is given, filter in SQL. The salience branch used to call this with no filter
    # and discard 99%+ of the result in Python — audit_entries alone built 169k rows to keep 0.
    where, params = _id_filter(ids)
    rows = connection.execute(
        f"""
        SELECT id, action_type, target_type, target_id, reason, changed_fields
        FROM audit_entries
        {where}
        ORDER BY id
        """,
        params,
    ).fetchall()
    return [
        (
            "audit_entry",
            int(row["id"]),
            str(row["action_type"]),
            _join(row["target_type"], row["target_id"], row["reason"], row["changed_fields"]),
        )
        for row in rows
    ]


def _join(*parts) -> str:
    return " ".join(str(part) for part in parts if part is not None and str(part))
