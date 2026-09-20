CREATE TABLE source_initializations (
  id TEXT PRIMARY KEY,
  source_root TEXT NOT NULL,
  source_path TEXT NOT NULL,
  source_hash TEXT NOT NULL,
  state TEXT NOT NULL CHECK(state IN ('running','success','partial','failed')),
  started_at TEXT NOT NULL,
  finished_at TEXT,
  summary_json TEXT NOT NULL DEFAULT '{}',
  error_json TEXT
);

CREATE TABLE source_initialization_rows (
  id TEXT PRIMARY KEY,
  initialization_id TEXT NOT NULL REFERENCES source_initializations(id) ON DELETE RESTRICT,
  row_no INTEGER NOT NULL,
  row_key TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('imported','duplicate','unresolved','invalid')),
  source_id TEXT REFERENCES sources(id) ON DELETE RESTRICT,
  reason TEXT,
  UNIQUE(initialization_id, row_key)
);

CREATE INDEX source_initializations_started ON source_initializations(started_at DESC, id DESC);
CREATE INDEX source_initialization_rows_status ON source_initialization_rows(initialization_id, status, row_no);
