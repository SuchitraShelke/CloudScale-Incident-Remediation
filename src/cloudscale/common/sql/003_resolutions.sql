-- How humans fixed escalated incidents, and the runbook entries proposed from verified fixes.
-- Proposals are never added to runbooks.yaml automatically: a senior reviews them (autonomy only grows by review).

CREATE TABLE IF NOT EXISTS manual_resolutions (
  incident_id  TEXT PRIMARY KEY,
  recorded_by  TEXT NOT NULL,
  how_fixed    TEXT NOT NULL,
  category     TEXT NOT NULL,
  verified     BOOLEAN NOT NULL,
  metrics      JSONB NOT NULL,
  ts           TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS runbook_proposals (
  seq             BIGSERIAL PRIMARY KEY,
  proposal        JSONB NOT NULL,       -- {id, root_cause_category, signatures, corroborate, how_fixed, ...}
  source_incident TEXT NOT NULL,
  status          TEXT NOT NULL DEFAULT 'proposed',
  ts              TIMESTAMPTZ NOT NULL DEFAULT now()
);
