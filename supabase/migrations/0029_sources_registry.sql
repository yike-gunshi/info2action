-- 2026-07-05: subscription-config v22.0 — 信源注册表(方案 B 单一真相源)。
--
-- 抓取管线改为从此表读配置;每条 item 挂 source_id 归属(反查 (platform, source_key))。
-- 停用/删除只停增量,存量与展示不变:删除为软删(status='deleted'),重加同一
-- (platform, source_key) 复活原行以保证 item 归属连续。
-- 对应 SQLite 侧: src/db.py SCHEMA 的 sources 表 + items.source_id 列(双写)。

set search_path = remote_poc, extensions, public;

CREATE TABLE IF NOT EXISTS remote_poc.sources (
  id           bigserial PRIMARY KEY,
  platform     text NOT NULL
    CHECK (platform IN ('wechat_mp', 'x_user', 'rss', 'reddit', 'github_repo', 'bilibili_up')),
  source_key   text NOT NULL,
  display_name text,
  status       text NOT NULL DEFAULT 'active'
    CHECK (status IN ('active', 'paused', 'pending', 'broken', 'not_fetched', 'deleted')),
  config_json  text,
  origin       text
    CHECK (origin IS NULL OR origin IN ('seed_import', 'admin_add', 'reconcile_import')),
  validated_at timestamptz,
  created_at   timestamptz NOT NULL DEFAULT now(),
  updated_at   timestamptz NOT NULL DEFAULT now(),
  UNIQUE (platform, source_key)
);

CREATE INDEX IF NOT EXISTS sources_status_idx   ON remote_poc.sources (status);
CREATE INDEX IF NOT EXISTS sources_platform_idx ON remote_poc.sources (platform);

-- item 归属列;查不到的算法源/榜单内容 source_id 为空。
ALTER TABLE remote_poc.items ADD COLUMN IF NOT EXISTS source_id bigint;

CREATE INDEX IF NOT EXISTS items_source_id_idx
  ON remote_poc.items (source_id) WHERE source_id IS NOT NULL;

-- Rollback:
-- DROP INDEX IF EXISTS remote_poc.items_source_id_idx;
-- ALTER TABLE remote_poc.items DROP COLUMN IF EXISTS source_id;
-- DROP INDEX IF EXISTS remote_poc.sources_platform_idx;
-- DROP INDEX IF EXISTS remote_poc.sources_status_idx;
-- DROP TABLE IF EXISTS remote_poc.sources;
