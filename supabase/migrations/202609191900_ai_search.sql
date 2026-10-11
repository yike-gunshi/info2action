-- AI search: half-precision item recall index + durable summary jobs.
--
-- Operational notes:
-- - Execute using the pooler's 5432 SESSION port (not the 6543 transaction port).
-- - Run with psql -v ON_ERROR_STOP=1 -f ... and autocommit, NOT -1 / --single-transaction.
--   Section A's CREATE INDEX CONCURRENTLY cannot run inside a transaction block.
--   Section B may run separately in a transaction after the index has completed.
-- - Size (checked 2026-09-19 read-only): items with visible=1 AND embedding IS NOT NULL = 119,993
--   all-time (~480-590 MB HNSW); the 90-day window is ~57k rows (~250 MB). The index is therefore
--   PARTIAL on a FIXED cutoff (published_at >= '2026-06-20 00:00:00+00'::timestamptz).
--   The explicit timezone makes this a constant, avoiding the STABLE date-to-timestamptz
--   conversion that violates the IMMUTABLE requirement for index predicates.
--   The runtime query adds the same
--   literal predicate (env AI_SEARCH_VECTOR_INDEX_CUTOFF) so the planner can match it; the search
--   window (now() - 90 days) only moves forward, so the cutoff never excludes eligible rows.
--   Rotate quarterly: create a new index with a newer cutoff, switch the env, drop the old one.
--   maintenance_work_mem is 128 MB on this instance; the build will spill but completes.
--   Check free disk before building; watch pg_stat_activity in a low-traffic window.
--   Job input_text <=200 KB/row, default <=10 jobs/day.
-- - A failed concurrent build may leave an INVALID index: DROP it CONCURRENTLY
--   before retrying (IF NOT EXISTS does not repair INVALID indexes).
-- - Rollback A: DROP INDEX CONCURRENTLY IF EXISTS remote_poc.remote_poc_items_embedding_halfvec_hnsw_idx;
-- - Rollback B (discards job history): DROP TABLE IF EXISTS remote_poc.ai_search_jobs;

-- A. Session settings are intentionally confined to this 5432 maintenance session.
SET lock_timeout = '5s';
SET statement_timeout = '45min';

CREATE INDEX CONCURRENTLY IF NOT EXISTS remote_poc_items_embedding_halfvec_hnsw_idx
  ON remote_poc.items USING hnsw ((embedding::extensions.halfvec(1536)) extensions.halfvec_cosine_ops)
  WITH (m = 16, ef_construction = 64)
  WHERE visible = 1 AND embedding IS NOT NULL AND published_at >= '2026-06-20 00:00:00+00'::timestamptz;

-- B. Durable jobs. No user identity, foreign keys, cron or item writes.
CREATE TABLE IF NOT EXISTS remote_poc.ai_search_jobs (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  created_at timestamptz NOT NULL DEFAULT now(),
  started_at timestamptz,
  finished_at timestamptz,
  status text NOT NULL DEFAULT 'queued'
    CHECK (status IN ('queued', 'running', 'done', 'failed', 'unavailable')),
  query text NOT NULL,
  query_lexical text NOT NULL,
  ip_hash text NOT NULL,
  window_days int NOT NULL DEFAULT 90,
  lexical_hits int NOT NULL DEFAULT 0,
  vector_hits int NOT NULL DEFAULT 0,
  vector_ok bool NOT NULL DEFAULT false,
  candidate_ids jsonb NOT NULL DEFAULT '[]'::jsonb,
  candidate_meta jsonb NOT NULL DEFAULT '[]'::jsonb,
  model text,
  prompt_version text,
  request_params jsonb,
  usage jsonb,
  elapsed_ms int,
  points_estimate numeric(10,1),
  input_text text,
  result jsonb,
  content_md_final text,
  cited_ids jsonb,
  error text
);

CREATE INDEX IF NOT EXISTS ai_search_jobs_created_at_idx
  ON remote_poc.ai_search_jobs (created_at DESC);
CREATE INDEX IF NOT EXISTS ai_search_jobs_ip_hash_created_at_idx
  ON remote_poc.ai_search_jobs (ip_hash, created_at);

RESET lock_timeout;
RESET statement_timeout;
