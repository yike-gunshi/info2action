-- 2026-07-15: highlights-v27.1 T-H2 — per-user cluster 反馈文本说明。
--
-- feedback_note 允许为空；撤销反馈时由应用层与 feedback_kind 一并清空。

set search_path = remote_poc, extensions, public;

ALTER TABLE remote_poc.cluster_status
  ADD COLUMN IF NOT EXISTS feedback_note text;

-- Rollback:
-- ALTER TABLE remote_poc.cluster_status DROP COLUMN IF EXISTS feedback_note;
