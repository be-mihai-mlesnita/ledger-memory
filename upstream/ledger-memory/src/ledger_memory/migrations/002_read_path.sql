-- Read-path tables added in 0.2.0: reader devices, the read log, provenance sources, mental models,
-- extraction, entities and the knowledge graph. Additive only: no existing table is altered.

CREATE TABLE IF NOT EXISTS client_devices (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT NOT NULL,
    token_hash   TEXT NOT NULL UNIQUE,
    created_at   TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_seen_at TEXT,
    revoked_at   TEXT
, device_class TEXT NOT NULL DEFAULT 'interactive', platform TEXT, capabilities TEXT NOT NULL DEFAULT '[]', app_version TEXT, tier_ceiling INTEGER NOT NULL DEFAULT 3 CHECK (tier_ceiling IN (1, 2, 3)), principal_kind TEXT NOT NULL DEFAULT 'device' CHECK (principal_kind IN ('device', 'ai_peer')), subject_actor_id INTEGER REFERENCES actors(id), identity_assurance TEXT, owner_bound_at TEXT, owner_bound_by_actor_id INTEGER REFERENCES actors(id), last_activity_at TEXT);

CREATE INDEX IF NOT EXISTS idx_client_devices_revoked_at ON client_devices(revoked_at);
CREATE INDEX IF NOT EXISTS idx_client_devices_token_hash ON client_devices(token_hash);

CREATE TABLE IF NOT EXISTS "read_audit_entries" (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id           INTEGER REFERENCES client_devices(id),
    record_count        INTEGER NOT NULL,
    effective_ceiling   INTEGER NOT NULL CHECK (effective_ceiling IN (0, 1, 2, 3)),
    surface             TEXT NOT NULL DEFAULT 'unknown',
    created_at          TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_read_audit_entries_created_at ON read_audit_entries(created_at);
CREATE INDEX IF NOT EXISTS idx_read_audit_entries_device_id ON read_audit_entries(device_id);

CREATE TABLE IF NOT EXISTS source_attestations (
    id INTEGER PRIMARY KEY,
    source_kind TEXT NOT NULL,
    source_identity TEXT NOT NULL,
    source_content_hash TEXT NOT NULL,
    evidence_locator TEXT NOT NULL,
    field_name TEXT NOT NULL,
    raw_value TEXT NOT NULL,
    normalized_value TEXT NOT NULL,
    normalized_type TEXT NOT NULL,
    unit TEXT,
    verification_status TEXT NOT NULL CHECK (verification_status IN ('verified', 'rejected', 'unverified')),
    verification_reason TEXT,
    normalization_contract TEXT NOT NULL,
    verifier_name TEXT NOT NULL,
    verifier_version TEXT NOT NULL,
    candidate_identity TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
, sensitivity_tier INTEGER NOT NULL DEFAULT 1);

CREATE UNIQUE INDEX IF NOT EXISTS idx_source_attestations_candidate_contract
ON source_attestations (source_kind, candidate_identity, field_name, normalization_contract);
CREATE INDEX IF NOT EXISTS idx_source_attestations_evidence_locator
ON source_attestations (source_content_hash, evidence_locator);
CREATE INDEX IF NOT EXISTS idx_source_attestations_status ON source_attestations (verification_status);

CREATE TABLE IF NOT EXISTS imported_sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    import_origin TEXT NOT NULL,
    source_kind TEXT,
    conversation_id TEXT,
    conversation_title TEXT,
    message_id TEXT,
    author_role TEXT,
    evidence_level TEXT,
    source_timestamp TEXT,
    source_file TEXT,
    attachment_path TEXT,
    text TEXT NOT NULL,
    text_sha256 TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
, sensitivity_tier INTEGER NOT NULL DEFAULT 1);

CREATE INDEX IF NOT EXISTS idx_imported_sources_conversation ON imported_sources(conversation_id);
CREATE INDEX IF NOT EXISTS idx_imported_sources_timestamp ON imported_sources(source_timestamp);

CREATE TABLE IF NOT EXISTS mental_models (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    subject TEXT NOT NULL,
    scope TEXT NOT NULL,
    purpose TEXT NOT NULL,
    subject_actor_id INTEGER NOT NULL REFERENCES actors(id),
    sensitivity_tier INTEGER NOT NULL DEFAULT 1 CHECK (sensitivity_tier IN (1, 2, 3)),
    created_by_actor_id INTEGER REFERENCES actors(id),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (subject_actor_id, subject, scope)
);


CREATE TABLE IF NOT EXISTS mental_model_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mental_model_id INTEGER NOT NULL REFERENCES mental_models(id),
    version_number INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'proposed'
        CHECK (status IN ('proposed', 'active', 'stale', 'retired')),
    sensitivity_tier INTEGER NOT NULL DEFAULT 1 CHECK (sensitivity_tier IN (1, 2, 3)),
    compiler_identity TEXT NOT NULL,
    compiler_version TEXT NOT NULL,
    model_identity TEXT NOT NULL,
    recipe_sha256 TEXT NOT NULL,
    window_started_at TEXT NOT NULL,
    window_ended_at TEXT NOT NULL,
    created_by_actor_id INTEGER REFERENCES actors(id),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (mental_model_id, version_number)
);

CREATE INDEX IF NOT EXISTS idx_mm_versions_model ON mental_model_versions(mental_model_id);

CREATE TABLE IF NOT EXISTS mental_model_version_inputs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    version_id INTEGER NOT NULL REFERENCES mental_model_versions(id),
    record_type TEXT NOT NULL,
    record_id INTEGER NOT NULL,
    content_sha256 TEXT NOT NULL,
    authority_class TEXT NOT NULL,
    epistemic_status TEXT NOT NULL,
    input_sensitivity_tier INTEGER NOT NULL CHECK (input_sensitivity_tier IN (1, 2, 3)),
    UNIQUE (version_id, record_type, record_id)
);

CREATE INDEX IF NOT EXISTS idx_mm_version_inputs_version ON mental_model_version_inputs(version_id);

CREATE TABLE IF NOT EXISTS mental_model_claims (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    version_id INTEGER NOT NULL REFERENCES mental_model_versions(id),
    claim_index INTEGER NOT NULL,
    text TEXT NOT NULL,
    kind TEXT NOT NULL
        CHECK (kind IN ('biographical', 'temporal', 'causal', 'treatment', 'identity', 'general')),
    confidence REAL NOT NULL CHECK (confidence >= 0.0 AND confidence <= 1.0),
    sensitivity_tier INTEGER NOT NULL DEFAULT 1 CHECK (sensitivity_tier IN (1, 2, 3)),
    content_sha256 TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (version_id, claim_index)
);

CREATE INDEX IF NOT EXISTS idx_mm_claims_version ON mental_model_claims(version_id);

CREATE TABLE IF NOT EXISTS mental_model_claim_evidence (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    claim_id INTEGER NOT NULL REFERENCES mental_model_claims(id),
    record_type TEXT NOT NULL,
    record_id INTEGER NOT NULL,
    content_sha256 TEXT NOT NULL,
    UNIQUE (claim_id, record_type, record_id)
);

CREATE INDEX IF NOT EXISTS idx_mm_claim_evidence_claim ON mental_model_claim_evidence(claim_id);

CREATE TABLE IF NOT EXISTS mental_model_contradictions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    claim_id INTEGER NOT NULL REFERENCES mental_model_claims(id),
    contradicts_claim_id INTEGER NOT NULL REFERENCES mental_model_claims(id),
    rationale TEXT NOT NULL,
    sensitivity_tier INTEGER NOT NULL DEFAULT 1 CHECK (sensitivity_tier IN (1, 2, 3)),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK (claim_id != contradicts_claim_id),
    UNIQUE (claim_id, contradicts_claim_id)
);

CREATE INDEX IF NOT EXISTS idx_mm_contradictions_claim ON mental_model_contradictions(claim_id);

CREATE TABLE IF NOT EXISTS mental_model_evaluations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    version_id INTEGER NOT NULL REFERENCES mental_model_versions(id),
    dimension TEXT NOT NULL,
    score REAL NOT NULL,
    rationale TEXT NOT NULL,
    sensitivity_tier INTEGER NOT NULL DEFAULT 1 CHECK (sensitivity_tier IN (1, 2, 3)),
    evaluated_by_actor_id INTEGER REFERENCES actors(id),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_mm_evaluations_version ON mental_model_evaluations(version_id);

CREATE TABLE IF NOT EXISTS extraction_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id INTEGER NOT NULL REFERENCES imported_sources(id),
    extractor TEXT NOT NULL,
    extractor_version TEXT NOT NULL,
    recipe_sha256 TEXT NOT NULL,
    model_name TEXT,
    source_text_sha256 TEXT NOT NULL,
    candidate_count INTEGER NOT NULL DEFAULT 0,
    sensitivity_tier INTEGER NOT NULL DEFAULT 1 CHECK (sensitivity_tier IN (1, 2, 3)),
    created_by_actor_id INTEGER REFERENCES actors(id),
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_extraction_runs_source ON extraction_runs(source_id);

CREATE TABLE IF NOT EXISTS extraction_candidates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES extraction_runs(id),
    kind TEXT NOT NULL CHECK (kind IN ('speaker', 'time', 'entity', 'relation', 'statement',
                                       'number', 'date', 'currency', 'percent')),
    start_offset INTEGER NOT NULL CHECK (start_offset >= 0),
    end_offset INTEGER NOT NULL CHECK (end_offset > start_offset),
    span_sha256 TEXT NOT NULL,
    value TEXT NOT NULL,
    confidence REAL NOT NULL DEFAULT 1.0 CHECK (confidence >= 0.0 AND confidence <= 1.0),
    status TEXT NOT NULL DEFAULT 'proposed' CHECK (status IN ('proposed', 'promoted', 'rejected',
                                                              'superseded')),
    supersedes_candidate_id INTEGER REFERENCES extraction_candidates(id),
    sensitivity_tier INTEGER NOT NULL DEFAULT 1 CHECK (sensitivity_tier IN (1, 2, 3)),
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_extraction_candidates_run ON extraction_candidates(run_id);
CREATE INDEX IF NOT EXISTS idx_extraction_candidates_status ON extraction_candidates(status);

CREATE TABLE IF NOT EXISTS entities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    display_name TEXT NOT NULL,
    entity_kind TEXT NOT NULL CHECK (entity_kind IN ('person', 'org', 'place', 'thing')),
    status TEXT NOT NULL DEFAULT 'proposed' CHECK (status IN ('proposed', 'confirmed', 'retired')),
    canonical_entity_id INTEGER REFERENCES entities(id),
    protected INTEGER NOT NULL DEFAULT 0 CHECK (protected IN (0, 1)),
    sensitivity_tier INTEGER NOT NULL DEFAULT 1 CHECK (sensitivity_tier IN (1, 2, 3)),
    created_by_actor_id INTEGER REFERENCES actors(id),
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_entities_canonical ON entities(canonical_entity_id);
CREATE INDEX IF NOT EXISTS idx_entities_status ON entities(status);

CREATE TABLE IF NOT EXISTS entity_aliases (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_id INTEGER NOT NULL REFERENCES entities(id),
    alias TEXT NOT NULL,
    normalized_alias TEXT NOT NULL,
    sensitivity_tier INTEGER NOT NULL DEFAULT 1 CHECK (sensitivity_tier IN (1, 2, 3)),
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (entity_id, normalized_alias)
);

CREATE INDEX IF NOT EXISTS idx_entity_aliases_normalized ON entity_aliases(normalized_alias);

CREATE TABLE IF NOT EXISTS entity_record_links (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_id INTEGER NOT NULL REFERENCES entities(id),
    record_type TEXT NOT NULL,
    record_id INTEGER NOT NULL,
    confidence REAL NOT NULL CHECK (confidence >= 0.0 AND confidence <= 1.0),
    method TEXT NOT NULL,
    method_version TEXT NOT NULL,
    sensitivity_tier INTEGER NOT NULL DEFAULT 1 CHECK (sensitivity_tier IN (1, 2, 3)),
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (entity_id, record_type, record_id, method)
);

CREATE INDEX IF NOT EXISTS idx_entity_record_links_entity ON entity_record_links(entity_id);
CREATE INDEX IF NOT EXISTS idx_entity_record_links_record ON entity_record_links(record_type, record_id);

CREATE TABLE IF NOT EXISTS graph_edges (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_a_id INTEGER NOT NULL,
    entity_b_id INTEGER NOT NULL,
    edge_kind TEXT NOT NULL CHECK (edge_kind IN ('co_occurrence', 'temporal', 'name_semantic')),
    weight REAL NOT NULL CHECK (weight > 0.0),
    provenance TEXT NOT NULL,
    built_at TEXT NOT NULL DEFAULT (datetime('now')),
    CHECK (entity_a_id < entity_b_id),
    UNIQUE (entity_a_id, entity_b_id, edge_kind)
);

CREATE INDEX IF NOT EXISTS idx_graph_edges_a ON graph_edges(entity_a_id);
CREATE INDEX IF NOT EXISTS idx_graph_edges_b ON graph_edges(entity_b_id);

CREATE TABLE IF NOT EXISTS graph_rebuilds (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    status TEXT NOT NULL DEFAULT 'running' CHECK (status IN ('running', 'completed')),
    scope TEXT NOT NULL DEFAULT 'full',
    builder_version TEXT NOT NULL,
    edge_count INTEGER,
    watermarks TEXT,
    started_at TEXT NOT NULL DEFAULT (datetime('now')),
    completed_at TEXT
);
