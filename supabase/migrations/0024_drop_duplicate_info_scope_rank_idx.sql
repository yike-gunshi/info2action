-- SUPA-PERF-0531-P2-1: drop duplicate info_scope_items rank index.
--
-- Why: info_scope_items_scope_rank_idx has the same key/opclass/collation as
-- the primary-key index on (version_id, scope_key, rank). Keeping both costs
-- about 150 MB of storage and extra write maintenance on read-model refreshes.
--
-- Operational notes:
-- - Run outside an explicit transaction; DROP INDEX CONCURRENTLY cannot run
--   inside BEGIN/COMMIT.
-- - Execute in a low-traffic window and watch pg_stat_activity.
-- - Rollback:
--     CREATE INDEX CONCURRENTLY IF NOT EXISTS info_scope_items_scope_rank_idx
--       ON remote_poc.info_scope_items (version_id, scope_key, rank);

set lock_timeout = '5s';
set statement_timeout = '5min';

DROP INDEX CONCURRENTLY IF EXISTS remote_poc.info_scope_items_scope_rank_idx;
