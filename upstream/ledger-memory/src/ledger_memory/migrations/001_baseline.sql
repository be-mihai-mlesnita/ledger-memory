-- ledger-memory baseline schema: memory tables only.
-- Foreign keys to tables outside the memory module were removed; the columns remain (NULL).

CREATE TABLE actors (
    id INTEGER PRIMARY KEY,
    actor_type TEXT NOT NULL,
    name TEXT NOT NULL,
    external_ref TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
, notes TEXT, sensitivity_tier INTEGER NOT NULL DEFAULT 1);

CREATE TABLE config (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    key        TEXT NOT NULL UNIQUE,
    value      TEXT NOT NULL,
    actor_id   INTEGER REFERENCES actors(id),
    reason     TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE events (
    id INTEGER PRIMARY KEY,
    event_type TEXT NOT NULL,
    task_id INTEGER,
    task_run_id INTEGER,
    worker_id INTEGER,
    actor_id INTEGER,
    occurred_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    summary TEXT NOT NULL,
    metadata TEXT,
    source_type TEXT,
    source_ref TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, sensitivity_tier INTEGER NOT NULL DEFAULT 1,
    FOREIGN KEY (actor_id) REFERENCES actors(id)
);

CREATE TABLE audit_entries (
    id INTEGER PRIMARY KEY,
    actor_id INTEGER,
    action_type TEXT NOT NULL,
    target_type TEXT NOT NULL,
    target_id INTEGER,
    reason TEXT,
    changed_fields TEXT,
    occurred_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    event_id INTEGER,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, sensitivity_tier INTEGER NOT NULL DEFAULT 1,
    FOREIGN KEY (actor_id) REFERENCES actors(id),
    FOREIGN KEY (event_id) REFERENCES events(id)
);

CREATE TABLE facts (
    id INTEGER PRIMARY KEY,
    statement TEXT NOT NULL,
    fact_type TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    known_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    valid_from TEXT,
    valid_to TEXT,
    superseded_by_fact_id INTEGER,
    confidence REAL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    created_by_actor_id INTEGER, origin TEXT DEFAULT 'local', source_db TEXT, source_record_id INTEGER, sensitivity_tier INTEGER NOT NULL DEFAULT 1, authority_class TEXT NOT NULL DEFAULT 'legacy_unclassified'
    CHECK (authority_class IN ('evidence_attested','actor_asserted','model_inferred','system_observed','legacy_unclassified')), materialization TEXT NOT NULL DEFAULT 'materialization_unclassified'
    CHECK (materialization IN ('authoritative_claim','durable_interpretation','rebuildable_projection','materialization_unclassified')), asserted_by_actor_id INTEGER REFERENCES actors(id) ON DELETE SET NULL, quarantined INTEGER NOT NULL DEFAULT 0 CHECK (quarantined IN (0, 1)), content_author TEXT, submitting_principal_id INTEGER, instruction_shape INTEGER NOT NULL DEFAULT 0
    CHECK (instruction_shape IN (0, 1)), artifact_category TEXT,
    FOREIGN KEY (superseded_by_fact_id) REFERENCES facts(id),
    FOREIGN KEY (created_by_actor_id) REFERENCES actors(id)
);

CREATE TABLE fact_evidence (
    id INTEGER PRIMARY KEY,
    fact_id INTEGER NOT NULL,
    evidence_type TEXT NOT NULL,
    evidence_id INTEGER NOT NULL,
    relationship TEXT NOT NULL DEFAULT 'supports',
    summary TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, sensitivity_tier INTEGER NOT NULL DEFAULT 1,
    FOREIGN KEY (fact_id) REFERENCES facts(id)
);

CREATE TABLE fact_glosses (
    id                          INTEGER PRIMARY KEY AUTOINCREMENT,
    fact_id                     INTEGER NOT NULL REFERENCES facts(id),
    gloss                       TEXT NOT NULL,
    gloss_model                 TEXT NOT NULL,
    source_statement_sha256     TEXT NOT NULL,
    created_at                  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(fact_id)
);

CREATE TABLE decisions (
    id INTEGER PRIMARY KEY,
    title TEXT NOT NULL,
    summary TEXT NOT NULL,
    rationale TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
, origin TEXT DEFAULT 'local', source_db TEXT, source_record_id INTEGER, sensitivity_tier INTEGER NOT NULL DEFAULT 1);

CREATE TABLE decision_facts (
    id INTEGER PRIMARY KEY,
    decision_id INTEGER NOT NULL,
    fact_id INTEGER NOT NULL,
    relationship TEXT NOT NULL DEFAULT 'supports',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, sensitivity_tier INTEGER NOT NULL DEFAULT 1,
    FOREIGN KEY (decision_id) REFERENCES decisions(id),
    FOREIGN KEY (fact_id) REFERENCES facts(id),
    UNIQUE (decision_id, fact_id, relationship)
);

CREATE TABLE context_packages (
    id INTEGER PRIMARY KEY,
    task_id INTEGER,
    task_run_id INTEGER,
    objective TEXT NOT NULL,
    status_summary TEXT NOT NULL,
    constraints_summary TEXT,
    facts_summary TEXT,
    events_summary TEXT,
    open_questions TEXT,
    risks TEXT,
    recommended_next_actions TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, origin TEXT DEFAULT 'local', source_db TEXT, source_record_id INTEGER, sensitivity_tier INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE context_package_items (
    id INTEGER PRIMARY KEY,
    context_package_id INTEGER NOT NULL,
    item_type TEXT NOT NULL,
    item_id INTEGER NOT NULL,
    relationship TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, sensitivity_tier INTEGER NOT NULL DEFAULT 1,
    FOREIGN KEY (context_package_id) REFERENCES context_packages(id),
    UNIQUE (context_package_id, item_type, item_id, relationship)
);

CREATE TABLE relationships (
    id INTEGER PRIMARY KEY,
    source_type TEXT NOT NULL,
    source_id INTEGER NOT NULL,
    target_type TEXT NOT NULL,
    target_id INTEGER NOT NULL,
    relationship TEXT NOT NULL,
    summary TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
, sensitivity_tier INTEGER NOT NULL DEFAULT 1);

CREATE TABLE documents (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    origin_type          TEXT NOT NULL,
    uri                  TEXT,
    media_type           TEXT NOT NULL,
    sensitivity_class    TEXT NOT NULL DEFAULT 'personal'
                             CHECK (sensitivity_class IN ('public', 'personal', 'sensitive')),
    content_sha256       TEXT NOT NULL UNIQUE,
    content              TEXT NOT NULL,
    retrieved_at         TEXT NOT NULL,
    ingested_by_actor_id INTEGER NOT NULL REFERENCES actors(id),
    created_at           TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
, origin TEXT DEFAULT 'local', source_db TEXT, source_record_id INTEGER, sensitivity_tier INTEGER NOT NULL DEFAULT 1);

CREATE TABLE chunks (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id        INTEGER NOT NULL REFERENCES documents(id),
    chunk_index        INTEGER NOT NULL,
    content            TEXT NOT NULL,
    content_sha256     TEXT NOT NULL,
    chunking_recipe    TEXT NOT NULL,
    projection_version TEXT NOT NULL,
    created_at         TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
, origin TEXT DEFAULT 'local', source_db TEXT, source_record_id INTEGER);

CREATE VIRTUAL TABLE search_fts USING fts5(
    record_type UNINDEXED,
    record_id UNINDEXED,
    title,
    body,
    sensitivity_tier UNINDEXED
);

CREATE TABLE search_index_entries (
    record_type TEXT NOT NULL,
    record_id INTEGER NOT NULL,
    content_hash TEXT NOT NULL,
    indexed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (record_type, record_id)
);

CREATE TABLE search_index_rebuilds (
    id INTEGER PRIMARY KEY,
    started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    completed_at TEXT,
    indexed_rows INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL,
    notes TEXT
);

CREATE TABLE semantic_index_entries (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    record_type         TEXT    NOT NULL,
    record_id           INTEGER NOT NULL,
    content_hash        TEXT    NOT NULL,
    embedding_model     TEXT    NOT NULL,
    embedding_dimensions INTEGER NOT NULL,
    projection_version  INTEGER NOT NULL DEFAULT 1,
    embedded_at         TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (record_type, record_id, embedding_model)
);

CREATE TABLE semantic_index_rebuilds (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    status          TEXT    NOT NULL,
    embedding_model TEXT,
    scope_type      TEXT,
    scope_id        INTEGER,
    indexed_rows    INTEGER,
    notes           TEXT,
    started_at      TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    completed_at    TEXT
);

CREATE INDEX idx_audit_entries_action_type ON audit_entries(action_type);
CREATE INDEX idx_audit_entries_actor_id ON audit_entries(actor_id);
CREATE INDEX idx_audit_entries_event_id ON audit_entries(event_id);
CREATE INDEX idx_audit_entries_target ON audit_entries(target_type, target_id);
CREATE INDEX idx_chunks_content_sha256 ON chunks(content_sha256);
CREATE INDEX idx_chunks_document_id ON chunks(document_id);
CREATE INDEX idx_config_key ON config(key);
CREATE INDEX idx_context_package_items_item ON context_package_items(item_type, item_id);
CREATE INDEX idx_context_package_items_package_id ON context_package_items(context_package_id);
CREATE INDEX idx_context_package_items_relationship ON context_package_items(relationship);
CREATE INDEX idx_context_packages_created_at ON context_packages(created_at);
CREATE INDEX idx_context_packages_task_id ON context_packages(task_id);
CREATE INDEX idx_context_packages_task_run_id ON context_packages(task_run_id);
CREATE INDEX idx_decision_facts_decision_id ON decision_facts(decision_id);
CREATE INDEX idx_decision_facts_fact_id ON decision_facts(fact_id);
CREATE INDEX idx_decisions_created_at ON decisions(created_at);
CREATE INDEX idx_decisions_status ON decisions(status);
CREATE INDEX idx_documents_sensitivity_class ON documents(sensitivity_class);
CREATE INDEX idx_events_actor_id ON events(actor_id);
CREATE INDEX idx_events_event_type ON events(event_type);
CREATE INDEX idx_events_occurred_at ON events(occurred_at);
CREATE INDEX idx_events_task_id ON events(task_id);
CREATE INDEX idx_events_task_run_id ON events(task_run_id);
CREATE INDEX idx_events_worker_id ON events(worker_id);
CREATE INDEX idx_fact_evidence_evidence ON fact_evidence(evidence_type, evidence_id);
CREATE INDEX idx_fact_evidence_fact_id ON fact_evidence(fact_id);
CREATE INDEX idx_fact_evidence_relationship ON fact_evidence(relationship);
CREATE INDEX idx_fact_glosses_fact_id ON fact_glosses(fact_id);
CREATE INDEX idx_facts_artifact_category ON facts(artifact_category);
CREATE INDEX idx_facts_fact_type ON facts(fact_type);
CREATE INDEX idx_facts_instruction_shape ON facts(instruction_shape);
CREATE INDEX idx_facts_known_at ON facts(known_at);
CREATE INDEX idx_facts_quarantined ON facts(quarantined);
CREATE INDEX idx_facts_status ON facts(status);
CREATE INDEX idx_facts_superseded_by_fact_id ON facts(superseded_by_fact_id);
CREATE INDEX idx_facts_valid_from ON facts(valid_from);
CREATE INDEX idx_facts_valid_to ON facts(valid_to);
CREATE INDEX idx_relationships_relationship
    ON relationships(relationship);
CREATE INDEX idx_relationships_source
    ON relationships(source_type, source_id);
CREATE INDEX idx_relationships_target
    ON relationships(target_type, target_id);
CREATE INDEX idx_semantic_index_entries_embedded_at
    ON semantic_index_entries (embedded_at);
CREATE INDEX idx_semantic_index_entries_embedding_model
    ON semantic_index_entries (embedding_model);
CREATE INDEX idx_semantic_index_entries_record
    ON semantic_index_entries (record_type, record_id);
CREATE INDEX idx_semantic_index_rebuilds_embedding_model
    ON semantic_index_rebuilds (embedding_model);
CREATE INDEX idx_semantic_index_rebuilds_status
    ON semantic_index_rebuilds (status);
