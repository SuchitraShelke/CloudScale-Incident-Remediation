-- Semantic cache (pgvector) + token ledger. Idempotent.

CREATE TABLE IF NOT EXISTS semantic_cache (
  id                BIGSERIAL PRIMARY KEY,
  cloud             TEXT NOT NULL,
  namespace         TEXT NOT NULL,
  service           TEXT NOT NULL,
  key_text          TEXT NOT NULL,
  embedding         vector(384) NOT NULL,          -- BAAI/bge-small-en-v1.5
  triage            JSONB NOT NULL,
  plan_template     JSONB NOT NULL,                -- {namespace}/{service}/{revision} placeholders
  original_cost_usd NUMERIC(12,6) NOT NULL,
  source_incident   TEXT NOT NULL,
  hit_count         INT NOT NULL DEFAULT 0,
  created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  expires_at        TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS semantic_cache_scope ON semantic_cache (cloud, namespace, service);
CREATE INDEX IF NOT EXISTS semantic_cache_hnsw ON semantic_cache USING hnsw (embedding vector_cosine_ops);

CREATE TABLE IF NOT EXISTS token_usage (
  id                BIGSERIAL PRIMARY KEY,
  ts                TIMESTAMPTZ NOT NULL DEFAULT now(),
  incident_id       TEXT NOT NULL,
  agent             TEXT NOT NULL,
  model             TEXT NOT NULL,
  input_tokens      INT NOT NULL,
  output_tokens     INT NOT NULL,
  cache_read_tokens INT NOT NULL DEFAULT 0,
  cost_usd          NUMERIC(12,6) NOT NULL,           -- 0 when served from the semantic cache
  avoided_cost_usd  NUMERIC(12,6) NOT NULL DEFAULT 0, -- original cost of the cached call on a hit
  cache_tier        TEXT,                             -- NULL | semantic
  failover          TEXT
);
CREATE INDEX IF NOT EXISTS token_usage_incident ON token_usage (incident_id);
CREATE INDEX IF NOT EXISTS token_usage_ts ON token_usage (ts);

-- Order incidents by insertion, not by created_at: the dev VM's clock was corrected by about an hour,
-- which put earlier incidents "after" later ones.
ALTER TABLE incidents ADD COLUMN IF NOT EXISTS seq BIGSERIAL;
