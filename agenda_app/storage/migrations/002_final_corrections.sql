ALTER TABLE runs ADD COLUMN created_seq INTEGER;
ALTER TABLE runs ADD COLUMN deadline_at TEXT;
ALTER TABLE model_inventory ADD COLUMN endpoint_url TEXT;

UPDATE runs
SET created_seq = (
  SELECT COUNT(*) FROM runs newer
  WHERE newer.requested_at < runs.requested_at
     OR (newer.requested_at = runs.requested_at AND newer.id <= runs.id)
);

UPDATE model_inventory
SET endpoint_url = (SELECT json_extract(values_json, '$.ollama_base_url') FROM settings WHERE settings.id = 1)
WHERE endpoint_url IS NULL;

CREATE UNIQUE INDEX IF NOT EXISTS runs_created_seq_unique ON runs(created_seq);
CREATE INDEX IF NOT EXISTS runs_created_order ON runs(created_seq DESC, id DESC);

CREATE TABLE import_rows_final (
  id TEXT PRIMARY KEY, import_id TEXT NOT NULL REFERENCES imports(id) ON DELETE RESTRICT, file_kind TEXT NOT NULL,
  file_hash TEXT NOT NULL, row_no INTEGER NOT NULL, row_key TEXT NOT NULL, payload_json TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('imported','duplicate','unresolved','invalid')), entity_type TEXT, entity_id TEXT, reason TEXT,
  UNIQUE(import_id, file_kind, row_key)
);
INSERT INTO import_rows_final(id,import_id,file_kind,file_hash,row_no,row_key,payload_json,status,entity_type,entity_id,reason)
SELECT id,import_id,file_kind,file_hash,row_no,row_key,payload_json,status,entity_type,entity_id,reason FROM import_rows;
DROP TABLE import_rows;
ALTER TABLE import_rows_final RENAME TO import_rows;
