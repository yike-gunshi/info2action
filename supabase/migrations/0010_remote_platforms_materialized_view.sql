-- BF-0515-mv-pgcron: materialized view for /api/feed/platforms hot path
-- Why: /api/feed/platforms cold runs 5 sequential SQLs (counts, source counts,
--   ranked items CTE, category counts via jsonb_array_elements_text, null cat)
--   = 7-8s cold per the BF-0515-11 evidence. With this MV pre-built and refreshed
--   by pg_cron every 10 min, cold reduces to ~50-200ms (1 small SELECT).
--
-- The MV stores top 50 items per platform (sorted by fetched_at DESC, then
-- relevance_score) and exposes a row_number column so the application can
-- LIMIT cheaply.
--
-- pg_cron schedules a CONCURRENTLY refresh every 10 min — safe under reads
-- (requires the unique index below).
--
-- Idempotent. Rollback: see end of file.

set search_path = remote_poc, extensions, public;

-- 1. MV definition.
-- Excludes 3 heavy JSONB columns (detail_json, comments_json, content) which
-- are only used in detail/full-text views — not in /api/feed/platforms list.
-- Including them ballooned MV row size + transfer cost; without them the MV
-- is small enough to read in <500ms cold.
CREATE MATERIALIZED VIEW IF NOT EXISTS remote_poc.mv_items_top_per_platform AS
WITH ranked AS (
  SELECT
    i.id, i.user_id, i.platform, i.source, i.title, i.author_name, i.author_id,
    i.author_avatar, i.url, i.cover_url, i.media_json, i.metrics_json,
    i.tags_json, i.lang, i.description,
    i.ai_summary, i.ai_key_points, i.ai_category, i.ai_keywords,
    i.ai_categories, i.ai_subcategories, i.multi_l1_reason, i.ai_extracted,
    i.content_type, i.visible, i.relevance_score, i.fetched_at, i.published_at,
    i.created_at,
    row_number() OVER (
      PARTITION BY i.platform
      ORDER BY i.fetched_at DESC NULLS LAST,
               i.relevance_score DESC NULLS LAST
    ) AS rn
  FROM remote_poc.items i
  WHERE i.visible = 1
    AND i.platform != 'manual'
)
SELECT * FROM ranked WHERE rn <= 50;

-- 2. Required unique index for REFRESH CONCURRENTLY
CREATE UNIQUE INDEX IF NOT EXISTS mv_items_top_per_platform_id_idx
  ON remote_poc.mv_items_top_per_platform (id);

-- 3. Index for the application's typical access pattern
CREATE INDEX IF NOT EXISTS mv_items_top_per_platform_platform_rn_idx
  ON remote_poc.mv_items_top_per_platform (platform, rn);

-- 4. First refresh (synchronous, no CONCURRENTLY because table is empty on first build)
REFRESH MATERIALIZED VIEW remote_poc.mv_items_top_per_platform;

-- 5. Schedule refresh every 10 minutes via pg_cron
--    (Supabase has pg_cron enabled by default on all tiers.)
--    Use SELECT cron.unschedule first to make this idempotent.
DO $$
BEGIN
  PERFORM cron.unschedule('refresh-mv-items-top-per-platform')
  WHERE EXISTS (
    SELECT 1 FROM cron.job WHERE jobname = 'refresh-mv-items-top-per-platform'
  );
EXCEPTION WHEN OTHERS THEN
  -- pg_cron not available in some local test envs; ignore.
  NULL;
END $$;

DO $$
BEGIN
  PERFORM cron.schedule(
    'refresh-mv-items-top-per-platform',
    '*/10 * * * *',
    $job$REFRESH MATERIALIZED VIEW CONCURRENTLY remote_poc.mv_items_top_per_platform$job$
  );
EXCEPTION WHEN OTHERS THEN
  -- pg_cron not available; manual refresh required.
  RAISE NOTICE 'pg_cron not available, mv must be refreshed manually';
END $$;

-- ── Rollback steps ──────────────────────────────────────────────
-- SELECT cron.unschedule('refresh-mv-items-top-per-platform');
-- DROP MATERIALIZED VIEW IF EXISTS remote_poc.mv_items_top_per_platform CASCADE;
