-- Reduce Supabase I/O spikes during fetch, enrichment, and publish.
--
-- Operational notes:
-- - Run outside an explicit transaction; CREATE INDEX CONCURRENTLY cannot run
--   inside a transaction block.
-- - Rollback:
--     SELECT cron.schedule(
--       'refresh-mv-items-top-per-platform',
--       '*/10 * * * *',
--       $$REFRESH MATERIALIZED VIEW CONCURRENTLY remote_poc.mv_items_top_per_platform$$
--     );
--     DROP INDEX CONCURRENTLY IF EXISTS remote_poc.remote_poc_fetch_run_items_run_inserted_item_idx;
--     DROP INDEX CONCURRENTLY IF EXISTS remote_poc.remote_poc_clusters_touched_publish_pending_idx;

set search_path = remote_poc, extensions, public;

CREATE INDEX CONCURRENTLY IF NOT EXISTS remote_poc_fetch_run_items_run_inserted_item_idx
  ON remote_poc.fetch_run_items(run_id, was_inserted, item_id);

DO $$
BEGIN
  IF EXISTS (
    SELECT 1 FROM cron.job WHERE jobname = 'refresh-mv-items-top-per-platform'
  ) THEN
    PERFORM cron.unschedule('refresh-mv-items-top-per-platform');
  END IF;
END $$;

CREATE INDEX CONCURRENTLY IF NOT EXISTS remote_poc_clusters_touched_publish_pending_idx
  ON remote_poc.clusters(last_touched_run_id, id)
  WHERE last_touched_run_id IS NOT NULL
    AND (
      ai_title_draft IS NOT NULL
      OR ai_summary_draft IS NOT NULL
      OR ai_key_points_draft IS NOT NULL
      OR pending_is_visible_in_feed IS NOT NULL
      OR pending_summary_warnings_json IS NOT NULL
    );
