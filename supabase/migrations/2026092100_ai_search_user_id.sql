-- AI search: optional user ownership for personal search history.
--
-- Operational notes:
-- - Execute using the pooler's 5432 SESSION port (not the 6543 transaction port).
-- - Run with psql -v ON_ERROR_STOP=1 -f ... and autocommit, NOT -1 / --single-transaction.
--   CREATE INDEX CONCURRENTLY cannot run inside a transaction block.
-- - ADD COLUMN has no default: this is a metadata-only operation, with no table rewrite.
--   Historical jobs are intentionally NOT backfilled; anonymous jobs also keep user_id NULL.
-- - The index is PARTIAL (WHERE user_id IS NOT NULL): historical jobs without an owner
--   do not occupy the index.
-- - A failed concurrent build may leave an INVALID index. Before retrying, run:
--   DROP INDEX CONCURRENTLY IF EXISTS remote_poc.ai_search_jobs_user_id_created_at_idx;
--   IF NOT EXISTS does not repair INVALID indexes.
-- - Rollback (autocommit; discards user ownership, keeps jobs):
--   DROP INDEX CONCURRENTLY IF EXISTS remote_poc.ai_search_jobs_user_id_created_at_idx;
--   ALTER TABLE remote_poc.ai_search_jobs DROP COLUMN IF EXISTS user_id;

ALTER TABLE remote_poc.ai_search_jobs ADD COLUMN IF NOT EXISTS user_id text;

CREATE INDEX CONCURRENTLY IF NOT EXISTS ai_search_jobs_user_id_created_at_idx
  ON remote_poc.ai_search_jobs (user_id, created_at DESC)
  WHERE user_id IS NOT NULL;
