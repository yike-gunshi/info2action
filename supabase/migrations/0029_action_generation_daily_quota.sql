-- v21.0 action-revival: 行动点生成每日配额表(remote 镜像 db.py 的 user_daily_generation)。
--
-- Why: 生成入口从 admin-only 放开到登录用户后,非 admin 每日限 5 次(item + cluster 合计,
-- 发起即计)。计数必须落 remote(生产走 Supabase),且与 actions 行数解耦——删除行动点不释放配额。
--
-- 使用方:src/remote_db.py 的 get_generation_usage_today_remote /
-- try_consume_generation_quota_remote(后者用 upsert + WHERE 守卫做原子"未超限才 +1")。
--
-- 与 asr_usage(0.x)同构:按 (user_id, day_cst) 分片,day_cst 为北京时间自然日。

CREATE TABLE IF NOT EXISTS remote_poc.user_daily_generation (
  user_id     TEXT NOT NULL,
  day_cst     TEXT NOT NULL,
  count       INTEGER NOT NULL DEFAULT 0,
  updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (user_id, day_cst)
);

CREATE INDEX IF NOT EXISTS user_daily_generation_day_idx
  ON remote_poc.user_daily_generation (day_cst);
