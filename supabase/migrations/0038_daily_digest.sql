-- 2026-08-01: daily-digest v31 每日要点快照。
-- entries 自包含展示所需字段；cluster_id 仅作弱引用，不建外键。

set search_path = remote_poc, extensions, public;

CREATE TABLE IF NOT EXISTS remote_poc.daily_digest (
  digest_date date PRIMARY KEY,
  status text NOT NULL
    CHECK (status IN ('rolling', 'final')),
  entries jsonb NOT NULL DEFAULT '[]'::jsonb,
  candidate_ids jsonb,
  source text NOT NULL
    CHECK (source IN ('editor', 'rules_fallback')),
  prompt_version text,
  model text,
  generated_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

-- Rollback:
-- DROP TABLE IF EXISTS remote_poc.daily_digest;
