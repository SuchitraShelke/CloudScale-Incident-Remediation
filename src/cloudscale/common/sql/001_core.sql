-- Idempotent: applied by the orchestrator (as the owner role) on every start.

CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS incidents (
  incident_id TEXT PRIMARY KEY,
  scenario_id TEXT NOT NULL,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Append-only, hash-chained audit log. `seq` is assigned by the writer under an advisory lock,
-- so the chain is gap-free and linear even with several writer services.
CREATE TABLE IF NOT EXISTS audit_log (
  seq         BIGINT PRIMARY KEY,
  event_id    UUID NOT NULL UNIQUE,
  ts          TIMESTAMPTZ NOT NULL,
  incident_id TEXT,
  event_type  TEXT NOT NULL,
  actor_type  TEXT NOT NULL,          -- system | agent | human | policy
  actor_id    TEXT NOT NULL,
  payload     JSONB NOT NULL,
  prev_hash   TEXT NOT NULL,
  hash        TEXT NOT NULL           -- sha256(prev_hash || canonical_json(row without hashes))
);
CREATE INDEX IF NOT EXISTS audit_log_incident ON audit_log (incident_id, seq);

CREATE OR REPLACE FUNCTION audit_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION 'audit_log is append-only (% blocked)', TG_OP;
END $$;

DROP TRIGGER IF EXISTS audit_no_modify ON audit_log;
CREATE TRIGGER audit_no_modify BEFORE UPDATE OR DELETE ON audit_log
  FOR EACH ROW EXECUTE FUNCTION audit_immutable();
DROP TRIGGER IF EXISTS audit_no_truncate ON audit_log;
CREATE TRIGGER audit_no_truncate BEFORE TRUNCATE ON audit_log
  FOR EACH STATEMENT EXECUTE FUNCTION audit_immutable();
