-- BF-0515-mv-pgcron / v18.0 nav-merge compatibility patch:
-- Rebuild mv_items_top_per_platform with AI relevance filter so匿名用户走 MV
-- 快路径返回的卡片与 v18 nav 角标的过滤口径一致。
--
-- 背景:
-- 0010 (BF-0515-mv-pgcron) 创建的 MV 仅过滤 visible=1 AND platform!='manual',
-- 没带 v18 在 query_feed_platforms 加的 AI relevance 过滤。匿名用户走 use_mv 路径
-- 会拿到 ai_category='other' 的长尾噪音, 但 nav 角标按过滤后口径算 → 角标和
-- 卡片数量对不上 (v18 PRD §Spec-2 D3 锁定的口径)。
--
-- AI relevance 口径 (与 src/remote_db.py:_add_ai_relevance_filter 完全一致):
--   (ai_category IS NOT NULL AND ai_category != 'other')
--   OR (ai_categories IS NOT NULL AND ai_categories::text NOT IN ('[]','null','"null"'))
--
-- 必须 DROP + CREATE: PostgreSQL 不支持 ALTER MATERIALIZED VIEW 改 WHERE。
-- DROP 顺带删了 indexes, 下面重建。pg_cron schedule 也要 re-schedule。

set search_path = remote_poc, extensions, public;

-- 1. Drop old MV (含其上 indexes)
DROP MATERIALIZED VIEW IF EXISTS remote_poc.mv_items_top_per_platform CASCADE;

-- 2. Recreate with AI filter
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
      ORDER BY i.fetched_at DESC NULLS LAST,
               i.relevance_score DESC NULLS LAST
    ) AS rn
  FROM remote_poc.items i
  WHERE i.visible = 1
    AND i.platform != 'manual'
    -- v18.0 AI relevance filter (matches _add_ai_relevance_filter in remote_db.py)
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

-- 4. Index for app access pattern
CREATE INDEX IF NOT EXISTS mv_items_top_per_platform_platform_rn_idx
  ON remote_poc.mv_items_top_per_platform (platform, rn);

-- 5. Re-schedule pg_cron (silently no-op if pg_cron unavailable)
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
-- (然后重新 apply 0010_remote_platforms_materialized_view.sql 即可回到旧 MV)
