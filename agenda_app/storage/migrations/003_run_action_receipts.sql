ALTER TABLE action_requests ADD COLUMN response_state TEXT NOT NULL DEFAULT 'canonical'
  CHECK(response_state IN ('pending','canonical'));

CREATE INDEX IF NOT EXISTS action_requests_pending
  ON action_requests(scope, idempotency_key, response_state);
