-- BF-FOLLOWUP-0517-B1: rebuild mv_items_top_per_platform with
-- COALESCE(published_at, fetched_at) DESC as primary sort.
--
-- Why:
-- Old sort `ORDER BY fetched_at DESC, relevance_score DESC` lets a freshly
-- fetched batch of OLD items (e.g. twitter bookmarks scraped today but
-- originally posted in April) outrank items that were actually published
-- recently. User-facing channel sections then show 2026-04 cards even when
-- the database has plenty of 2026-05-17 content. See
-- docs/bugfix/reviews/BF-0517-5.md §2.3 for the empirical evidence.
--
-- New sort:
--   1. COALESCE(published_at, fetched_at) DESC  — recent posts first
--   2. fetched_at DESC                          — tie-breaker (same publish
--                                                  date → newer fetch first)
--   3. relevance_score DESC                     — final tie-breaker
--
-- NULL handling: published_at is NULL for github (no per-repo publish ts)
-- and a handful of legacy RSS rows. COALESCE makes those rows fall back to
-- fetched_at — same behavior as the old sort for them.
--
-- PostgreSQL doesn't allow ALTER MATERIALIZED VIEW … ORDER BY; must DROP
-- + CREATE. The CASCADE drops the two existing indexes; we recreate them.
-- During the DROP→CREATE window (~5–10s) the use_mv fast path will raise
-- RemoteDBError and callers fall back to live query, so readers stay served.

set search_path = remote_poc, extensions, public;

-- 1. Drop old MV (cascades to indexes)
DROP MATERIALIZED VIEW IF EXISTS remote_poc.mv_items_top_per_platform CASCADE;

-- 2. Recreate with new sort
CREATE MATERIALIZED VIEW remote_poc.mv_items_top_per_platform AS
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
      ORDER BY
        COALESCE(i.published_at, i.fetched_at) DESC NULLS LAST,
        i.fetched_at DESC NULLS LAST,
        i.relevance_score DESC NULLS LAST
    ) AS rn
  FROM remote_poc.items i
  WHERE i.visible = 1
    AND i.platform != 'manual'
    AND (
      (i.ai_category IS NOT NULL AND i.ai_category != 'other')
      OR (i.ai_categories IS NOT NULL
          AND i.ai_categories::text NOT IN ('[]', 'null', '"null"'))
    )
)
SELECT * FROM ranked WHERE rn <= 50;

-- 3. Required unique index for REFRESH CONCURRENTLY
CREATE UNIQUE INDEX IF NOT EXISTS mv_items_top_per_platform_id_idx
  ON remote_poc.mv_items_top_per_platform (id);

-- 4. Access pattern index
CREATE INDEX IF NOT EXISTS mv_items_top_per_platform_platform_rn_idx
  ON remote_poc.mv_items_top_per_platform (platform, rn);

-- 5. Re-schedule pg_cron (silently no-op if pg_cron not enabled — same as 0011)
DO $$
BEGIN
  PERFORM cron.unschedule('refresh-mv-items-top-per-platform')
  WHERE EXISTS (
    SELECT 1 FROM cron.job WHERE jobname = 'refresh-mv-items-top-per-platform'
  );
EXCEPTION WHEN OTHERS THEN
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
  RAISE NOTICE 'pg_cron not available; rely on application-side prewarm';
END $$;

-- ── Rollback ──────────────────────────────────────
-- DROP MATERIALIZED VIEW IF EXISTS remote_poc.mv_items_top_per_platform CASCADE;
-- Then re-apply 0011_remote_platforms_mv_ai_filter.sql to restore prior sort.
