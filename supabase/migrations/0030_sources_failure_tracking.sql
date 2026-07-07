-- 2026-07-06: subscription-config v22.0 — per-source failure tracking.
--
-- 为 sources 注册表补连续失败计数、最近成功时间和最近错误信息。
-- 对应 SQLite 侧: src/db.py SCHEMA 的 sources 表 + get_conn() 旧库补列。

set search_path = remote_poc, extensions, public;

ALTER TABLE remote_poc.sources
  ADD COLUMN IF NOT EXISTS consecutive_failures integer NOT NULL DEFAULT 0;

ALTER TABLE remote_poc.sources
  ADD COLUMN IF NOT EXISTS last_success_at text;

ALTER TABLE remote_poc.sources
  ADD COLUMN IF NOT EXISTS last_error text;

-- Rollback:
-- ALTER TABLE remote_poc.sources DROP COLUMN IF EXISTS last_error;
-- ALTER TABLE remote_poc.sources DROP COLUMN IF EXISTS last_success_at;
-- ALTER TABLE remote_poc.sources DROP COLUMN IF EXISTS consecutive_failures;
