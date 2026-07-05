-- SUPA-PERF-0603-P1-3: shrink info_card_items search-index write surface.
--
-- Why: info_card_items delta upserts now write only ~200 rows per fetch, but
-- the search_text trigram GIN index covers old complete read-model versions
-- too. When GIN pending-list cleanup lands on a foreground upsert, that small
-- delta can take 30-50s.
--
-- Safety:
-- - protect every version referenced by info_read_model_state
-- - keep the latest complete version
-- - remove stale complete versions and old transient building/error versions
-- - rebuild the existing search index concurrently; do not drop search
--   capability or change API behavior
--
-- Operational notes:
-- - Run outside an explicit transaction; REINDEX INDEX CONCURRENTLY cannot run
--   inside BEGIN/COMMIT.
-- - Execute in a low-traffic window and watch pg_stat_activity.

SET lock_timeout = '5s';
SET statement_timeout = '15min';

WITH protected_versions AS (
  SELECT active_version_id AS version_id
    FROM remote_poc.info_read_model_state
   WHERE active_version_id IS NOT NULL
  UNION
  SELECT version_id
    FROM (
      SELECT version_id
        FROM remote_poc.info_read_model_versions
       WHERE status = 'complete'
       ORDER BY completed_at DESC NULLS LAST,
                generated_at DESC NULLS LAST
       LIMIT 1
    ) recent_complete
)
DELETE FROM remote_poc.info_read_model_versions v
 WHERE NOT EXISTS (
       SELECT 1
         FROM protected_versions p
        WHERE p.version_id = v.version_id
   )
   AND (
        v.status = 'complete'
     OR v.generated_at < now() - interval '6 hours'
   );

REINDEX INDEX CONCURRENTLY remote_poc.info_card_items_search_trgm_idx;

ANALYZE remote_poc.info_card_items;

-- Rollback:
-- No automatic data rollback. A previous read model version can be rebuilt
-- with the normal info read-model refresh if needed.
