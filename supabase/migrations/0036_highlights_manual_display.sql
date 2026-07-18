-- 2026-07-17: highlights-funnel-panorama-v29 簇级展示 override。
--
-- manual_display    force_show | force_hide（NULL=恢复自动判定）
-- manual_display_at 最近一次 override 时间，撤销时清空。
-- 小表纯加列，无索引，无 CONCURRENTLY 需求。

set search_path = remote_poc, extensions, public;

ALTER TABLE remote_poc.highlight_cluster_decisions
  ADD COLUMN IF NOT EXISTS manual_display text
  CHECK (manual_display IS NULL OR manual_display IN ('force_show', 'force_hide'));

ALTER TABLE remote_poc.highlight_cluster_decisions
  ADD COLUMN IF NOT EXISTS manual_display_at timestamptz;

-- Rollback:
-- ALTER TABLE remote_poc.highlight_cluster_decisions DROP COLUMN IF EXISTS manual_display_at;
-- ALTER TABLE remote_poc.highlight_cluster_decisions DROP COLUMN IF EXISTS manual_display;
