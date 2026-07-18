-- 2026-07-10: highlights-quality-v25 F-D — per-user cluster 质量反馈。
--
-- cluster_status 加两列（复用既有 per-user per-cluster 状态表，不建新表）：
--   feedback_kind  text       positive | irrelevant | low_quality（NULL=无反馈/已撤销）
--   feedback_at    timestamptz 最近一次反馈时间
-- 本轮只落库积累数据，不参与任何排序/过滤。
-- 对应 SQLite 侧: src/db.py SCHEMA cluster_status + get_conn() 旧库补列。

set search_path = remote_poc, extensions, public;

ALTER TABLE remote_poc.cluster_status
  ADD COLUMN IF NOT EXISTS feedback_kind text;

ALTER TABLE remote_poc.cluster_status
  ADD COLUMN IF NOT EXISTS feedback_at timestamptz;

-- Rollback:
-- ALTER TABLE remote_poc.cluster_status DROP COLUMN IF EXISTS feedback_at;
-- ALTER TABLE remote_poc.cluster_status DROP COLUMN IF EXISTS feedback_kind;
