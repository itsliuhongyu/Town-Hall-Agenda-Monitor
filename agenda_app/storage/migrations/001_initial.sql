PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS schema_migrations (
  version INTEGER PRIMARY KEY, name TEXT NOT NULL, checksum TEXT NOT NULL, applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sources (
  id TEXT PRIMARY KEY, identity_key TEXT NOT NULL UNIQUE, platform TEXT NOT NULL, name TEXT NOT NULL,
  collection_url TEXT NOT NULL, timezone TEXT NOT NULL, timezone_origin TEXT NOT NULL,
  enabled INTEGER NOT NULL CHECK(enabled IN (0,1)), config_json TEXT NOT NULL DEFAULT '{}',
  revision INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS meetings (
  id TEXT PRIMARY KEY, source_id TEXT NOT NULL REFERENCES sources(id) ON DELETE RESTRICT,
  identity_key TEXT NOT NULL, identity_kind TEXT NOT NULL CHECK(identity_kind IN ('native','fallback','legacy')),
  native_key TEXT, title TEXT NOT NULL, local_date TEXT, local_datetime TEXT, starts_at_utc TEXT,
  timezone TEXT NOT NULL, raw_date TEXT NOT NULL DEFAULT '',
  time_quality TEXT NOT NULL CHECK(time_quality IN ('date_only','exact','ambiguous','unknown')),
  status TEXT NOT NULL CHECK(status IN ('scheduled','unknown')), created_at TEXT NOT NULL, last_seen_at TEXT NOT NULL,
  UNIQUE(source_id, identity_key)
);
CREATE TABLE IF NOT EXISTS documents (
  id TEXT PRIMARY KEY, meeting_id TEXT NOT NULL REFERENCES meetings(id) ON DELETE RESTRICT,
  identity_key TEXT NOT NULL, native_key TEXT, kind TEXT NOT NULL CHECK(kind IN ('agenda','packet','attachment','legacy')),
  original_url TEXT, source_url TEXT, display_filename TEXT NOT NULL, created_at TEXT NOT NULL, last_seen_at TEXT NOT NULL,
  UNIQUE(meeting_id, identity_key)
);
CREATE TABLE IF NOT EXISTS document_versions (
  id TEXT PRIMARY KEY, document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE RESTRICT,
  version_no INTEGER NOT NULL CHECK(version_no > 0), sha256 TEXT, blob_relpath TEXT, byte_size INTEGER CHECK(byte_size IS NULL OR byte_size >= 0),
  media_type TEXT, fetched_url TEXT, fetched_at TEXT, original_state TEXT NOT NULL CHECK(original_state IN ('available','missing','legacy_unknown')),
  retained_text TEXT, reader_version TEXT, text_sha256 TEXT,
  read_status TEXT NOT NULL CHECK(read_status IN ('pending','readable','empty','needs_ocr','failed','unsupported')),
  read_error_json TEXT, created_at TEXT NOT NULL, UNIQUE(document_id, version_no), UNIQUE(document_id, sha256)
);
CREATE TABLE IF NOT EXISTS analyses (
  id TEXT PRIMARY KEY, document_version_id TEXT NOT NULL REFERENCES document_versions(id) ON DELETE RESTRICT,
  extraction_key TEXT NOT NULL, model_name TEXT, model_digest TEXT, reader_version TEXT NOT NULL,
  extractor_version TEXT NOT NULL, prompt_hash TEXT NOT NULL, parameters_json TEXT NOT NULL,
  settings_snapshot_json TEXT NOT NULL, state TEXT NOT NULL CHECK(state IN ('success','failed','empty','legacy_import')),
  cache_hit INTEGER NOT NULL CHECK(cache_hit IN (0,1)), started_at TEXT NOT NULL, finished_at TEXT,
  error_json TEXT, UNIQUE(document_version_id, extraction_key)
);
CREATE TABLE IF NOT EXISTS items (
  id TEXT PRIMARY KEY, document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE RESTRICT,
  identity_key TEXT NOT NULL, identity_kind TEXT NOT NULL CHECK(identity_kind IN ('native','exact_anchor','analysis_scoped','legacy')),
  created_at TEXT NOT NULL, UNIQUE(document_id, identity_key)
);
CREATE TABLE IF NOT EXISTS item_versions (
  id TEXT PRIMARY KEY, item_id TEXT NOT NULL REFERENCES items(id) ON DELETE RESTRICT,
  document_version_id TEXT NOT NULL REFERENCES document_versions(id) ON DELETE RESTRICT,
  evidence_hash TEXT NOT NULL, original_text TEXT NOT NULL, anchor_json TEXT NOT NULL,
  anchor_status TEXT NOT NULL CHECK(anchor_status IN ('exact','ambiguous','unverified','legacy')),
  created_at TEXT NOT NULL, UNIQUE(item_id, document_version_id, evidence_hash)
);
CREATE TABLE IF NOT EXISTS analysis_items (
  analysis_id TEXT NOT NULL REFERENCES analyses(id) ON DELETE RESTRICT,
  item_version_id TEXT NOT NULL REFERENCES item_versions(id) ON DELETE RESTRICT,
  ordinal INTEGER NOT NULL CHECK(ordinal >= 0), title TEXT NOT NULL, model_priority TEXT CHECK(model_priority IS NULL OR model_priority IN ('low','medium','high')),
  model_reason TEXT, extraction_origin TEXT NOT NULL CHECK(extraction_origin IN ('llm','rules','legacy')),
  suggested_priority TEXT NOT NULL CHECK(suggested_priority IN ('low','medium','high')),
  suggestion_source TEXT NOT NULL CHECK(suggestion_source IN ('model','builtin','legacy')), created_at TEXT NOT NULL,
  PRIMARY KEY(analysis_id, item_version_id), UNIQUE(analysis_id, ordinal)
);
CREATE TABLE IF NOT EXISTS policy_versions (
  id TEXT PRIMARY KEY, version_no INTEGER NOT NULL UNIQUE, rules_json TEXT NOT NULL, removals_json TEXT NOT NULL,
  strategy TEXT NOT NULL CHECK(strategy IN ('model_first','rules_override')), thresholds_json TEXT NOT NULL,
  change_reason TEXT NOT NULL, previous_id TEXT REFERENCES policy_versions(id) ON DELETE RESTRICT,
  undo_of TEXT REFERENCES policy_versions(id) ON DELETE RESTRICT, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS action_requests (
  id TEXT PRIMARY KEY, scope TEXT NOT NULL, idempotency_key TEXT NOT NULL, body_hash TEXT NOT NULL,
  result_json TEXT NOT NULL, http_status INTEGER NOT NULL, created_at TEXT NOT NULL, UNIQUE(scope, idempotency_key)
);
CREATE TABLE IF NOT EXISTS review_state (
  item_version_id TEXT PRIMARY KEY REFERENCES item_versions(id) ON DELETE RESTRICT,
  state TEXT NOT NULL CHECK(state IN ('unreviewed','confirmed','needs_review')),
  human_priority TEXT CHECK(human_priority IS NULL OR human_priority IN ('low','medium','high')),
  note TEXT NOT NULL DEFAULT '', row_version INTEGER NOT NULL DEFAULT 1 CHECK(row_version > 0),
  last_event_id TEXT, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS review_events (
  id TEXT PRIMARY KEY, item_version_id TEXT NOT NULL REFERENCES item_versions(id) ON DELETE RESTRICT,
  item_revision INTEGER NOT NULL, action TEXT NOT NULL CHECK(action IN ('set_priority','confirm','save_note','undo','import')),
  before_json TEXT NOT NULL, after_json TEXT NOT NULL, undo_of TEXT REFERENCES review_events(id) ON DELETE RESTRICT,
  request_id TEXT REFERENCES action_requests(id) ON DELETE RESTRICT, actor TEXT NOT NULL,
  created_at TEXT NOT NULL, UNIQUE(item_version_id, item_revision), UNIQUE(undo_of)
);
CREATE TABLE IF NOT EXISTS settings (
  id INTEGER PRIMARY KEY CHECK(id=1), revision INTEGER NOT NULL CHECK(revision > 0), values_json TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS model_inventory (
  id INTEGER PRIMARY KEY CHECK(id=1), state TEXT NOT NULL CHECK(state IN ('unknown','online','offline')),
  models_json TEXT NOT NULL DEFAULT '[]', observed_at TEXT, last_attempt_at TEXT, error_json TEXT
);
CREATE TABLE IF NOT EXISTS runs (
  id TEXT PRIMARY KEY, parent_run_id TEXT REFERENCES runs(id) ON DELETE RESTRICT,
  kind TEXT NOT NULL CHECK(kind IN ('full','retry','analyze_only')),
  status TEXT NOT NULL CHECK(status IN ('running','success','no_results','partial','failed','interrupted')),
  phase TEXT NOT NULL CHECK(phase IN ('discovery','download','read','analyze','publish','done')),
  reason_code TEXT, requested_at TEXT NOT NULL, started_at TEXT NOT NULL, finished_at TEXT,
  heartbeat_at TEXT NOT NULL, owner_token TEXT NOT NULL, worker_pid INTEGER, settings_snapshot_json TEXT NOT NULL,
  policy_version_id TEXT NOT NULL REFERENCES policy_versions(id) ON DELETE RESTRICT, source_snapshot_json TEXT NOT NULL,
  total_sources INTEGER NOT NULL CHECK(total_sources >= 0), error_json TEXT,
  publication_state TEXT NOT NULL CHECK(publication_state IN ('none','pending','published','preserved','failed')),
  revision INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS run_sources (
  id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE RESTRICT, source_id TEXT NOT NULL REFERENCES sources(id) ON DELETE RESTRICT,
  source_snapshot_json TEXT NOT NULL, window_start TEXT NOT NULL, window_end TEXT NOT NULL, timezone TEXT NOT NULL,
  state TEXT NOT NULL CHECK(state IN ('pending','discovering','downloading','analyzing','success','no_results','partial','failed','interrupted')),
  discovery_state TEXT NOT NULL CHECK(discovery_state IN ('pending','success','empty','failed')),
  inherited_from_id TEXT REFERENCES run_sources(id) ON DELETE RESTRICT, started_at TEXT, finished_at TEXT, observed_at TEXT,
  found_count INTEGER NOT NULL DEFAULT 0 CHECK(found_count >= 0), downloaded_count INTEGER NOT NULL DEFAULT 0 CHECK(downloaded_count >= 0),
  analyzed_count INTEGER NOT NULL DEFAULT 0 CHECK(analyzed_count >= 0), error_json TEXT, UNIQUE(run_id, source_id)
);
CREATE TABLE IF NOT EXISTS run_documents (
  id TEXT PRIMARY KEY, run_source_id TEXT NOT NULL REFERENCES run_sources(id) ON DELETE RESTRICT,
  document_id TEXT REFERENCES documents(id) ON DELETE RESTRICT, document_version_id TEXT REFERENCES document_versions(id) ON DELETE RESTRICT,
  analysis_id TEXT REFERENCES analyses(id) ON DELETE RESTRICT, locator_key TEXT NOT NULL, original_url TEXT NOT NULL DEFAULT '',
  download_state TEXT NOT NULL CHECK(download_state IN ('pending','running','downloaded','reused','failed','not_needed')),
  read_state TEXT NOT NULL CHECK(read_state IN ('pending','readable','empty','needs_ocr','failed','unsupported')),
  analyze_state TEXT NOT NULL CHECK(analyze_state IN ('pending','running','cached','success','empty','failed','not_needed')),
  attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts >= 0), attempts_json TEXT NOT NULL DEFAULT '[]', inherited_from_id TEXT REFERENCES run_documents(id) ON DELETE RESTRICT,
  bytes_received INTEGER NOT NULL DEFAULT 0 CHECK(bytes_received >= 0), started_at TEXT, finished_at TEXT, error_json TEXT,
  UNIQUE(run_source_id, locator_key)
);
CREATE TABLE IF NOT EXISTS run_items (
  run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE RESTRICT, item_version_id TEXT NOT NULL REFERENCES item_versions(id) ON DELETE RESTRICT,
  analysis_id TEXT NOT NULL, policy_version_id TEXT NOT NULL REFERENCES policy_versions(id) ON DELETE RESTRICT,
  policy_priority TEXT NOT NULL CHECK(policy_priority IN ('low','medium','high')), proposed_priority TEXT NOT NULL CHECK(proposed_priority IN ('low','medium','high')),
  decision_source TEXT NOT NULL CHECK(decision_source IN ('model','policy','builtin','legacy')), matched_rules_json TEXT NOT NULL DEFAULT '[]',
  included INTEGER NOT NULL CHECK(included IN (0,1)), exclusion_reason TEXT, source_run_id TEXT REFERENCES runs(id) ON DELETE RESTRICT,
  PRIMARY KEY(run_id, item_version_id), FOREIGN KEY(analysis_id, item_version_id) REFERENCES analysis_items(analysis_id, item_version_id) ON DELETE RESTRICT
);
CREATE TABLE IF NOT EXISTS app_state (
  id INTEGER PRIMARY KEY CHECK(id=1), current_policy_id TEXT NOT NULL REFERENCES policy_versions(id) ON DELETE RESTRICT,
  published_run_id TEXT REFERENCES runs(id) ON DELETE RESTRICT, last_full_success_run_id TEXT REFERENCES runs(id) ON DELETE RESTRICT,
  last_complete_run_id TEXT REFERENCES runs(id) ON DELETE RESTRICT, latest_export_id TEXT, data_revision INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS jobs (
  id TEXT PRIMARY KEY, kind TEXT NOT NULL CHECK(kind IN ('model_refresh','connection_test','export','import')),
  state TEXT NOT NULL CHECK(state IN ('running','success','failed','interrupted')), request_id TEXT REFERENCES action_requests(id) ON DELETE RESTRICT,
  owner_token TEXT NOT NULL, input_json TEXT NOT NULL, result_json TEXT, error_json TEXT, started_at TEXT NOT NULL, finished_at TEXT
);
CREATE TABLE IF NOT EXISTS imports (
  id TEXT PRIMARY KEY, bundle_hash TEXT NOT NULL UNIQUE, source_root TEXT NOT NULL,
  state TEXT NOT NULL CHECK(state IN ('running','success','partial','failed','interrupted')),
  started_at TEXT NOT NULL, finished_at TEXT, summary_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS import_rows (
  id TEXT PRIMARY KEY, import_id TEXT NOT NULL REFERENCES imports(id) ON DELETE RESTRICT, file_kind TEXT NOT NULL,
  file_hash TEXT NOT NULL, row_no INTEGER NOT NULL, row_key TEXT NOT NULL, payload_json TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('imported','duplicate','unresolved','invalid')), entity_type TEXT, entity_id TEXT, reason TEXT,
  UNIQUE(file_kind, row_key)
);
CREATE TABLE IF NOT EXISTS exports (
  id TEXT PRIMARY KEY, run_id TEXT REFERENCES runs(id) ON DELETE RESTRICT, query_json TEXT NOT NULL,
  data_revision INTEGER NOT NULL, review_snapshot_at TEXT NOT NULL,
  state TEXT NOT NULL CHECK(state IN ('preparing','ready','failed')), relpath TEXT, manifest_json TEXT,
  created_at TEXT NOT NULL, error_json TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS one_active_run ON runs((1)) WHERE status='running';
CREATE INDEX IF NOT EXISTS meetings_source_date ON meetings(source_id, local_date);
CREATE INDEX IF NOT EXISTS versions_document_created ON document_versions(document_id, created_at);
CREATE INDEX IF NOT EXISTS versions_hash ON document_versions(sha256);
CREATE INDEX IF NOT EXISTS items_document ON items(document_id);
CREATE INDEX IF NOT EXISTS item_versions_document ON item_versions(document_version_id);
CREATE INDEX IF NOT EXISTS reviews_state ON review_state(state, updated_at);
CREATE INDEX IF NOT EXISTS review_events_item_time ON review_events(item_version_id, created_at);
CREATE INDEX IF NOT EXISTS runs_started ON runs(started_at DESC);
CREATE INDEX IF NOT EXISTS run_sources_run_state ON run_sources(run_id, state);
CREATE INDEX IF NOT EXISTS run_documents_source_state ON run_documents(run_source_id, download_state, analyze_state);
CREATE INDEX IF NOT EXISTS run_items_run_priority ON run_items(run_id, proposed_priority, item_version_id);
CREATE INDEX IF NOT EXISTS exports_run ON exports(run_id, created_at);
