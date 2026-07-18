-- 2026-07-15: highlights-curation-v27 W1/W3 — cluster 推荐理由。
--
-- why_read 由总结管线逐步回填；NULL 表示尚未完成加工。
-- 本迁移只增加 nullable text 列，不触碰现有 cluster 数据。

set search_path = remote_poc, extensions, public;

ALTER TABLE remote_poc.clusters
  ADD COLUMN IF NOT EXISTS why_read text;

-- Rollback:
-- ALTER TABLE remote_poc.clusters DROP COLUMN IF EXISTS why_read;
