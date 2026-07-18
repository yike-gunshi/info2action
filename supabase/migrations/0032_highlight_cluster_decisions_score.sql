-- 2026-07-10: highlights-quality-v25 F-B — cluster 级双因子精选分。
--
-- highlight_cluster_decisions 加两列：
--   highlight_score  numeric  双因子分（LLM 质量 × ln(1+独立源数)，含薄证据收缩），
--                             仅 decision='included' 且有已打分 include 成员时非空
--   score_inputs     jsonb    可解释性输入 {max_q, avg_q, scored_include_count, unique_source_count}
--
-- ⚠️ 部署顺序：本迁移必须先于新代码重启应用（decisions 同步 SQL 会写这两列）。
-- 小表（每 cluster 一行）纯加列，无索引，无 CONCURRENTLY 需求。

set search_path = remote_poc, extensions, public;

ALTER TABLE remote_poc.highlight_cluster_decisions
  ADD COLUMN IF NOT EXISTS highlight_score numeric;

ALTER TABLE remote_poc.highlight_cluster_decisions
  ADD COLUMN IF NOT EXISTS score_inputs jsonb;

-- Rollback:
-- ALTER TABLE remote_poc.highlight_cluster_decisions DROP COLUMN IF EXISTS score_inputs;
-- ALTER TABLE remote_poc.highlight_cluster_decisions DROP COLUMN IF EXISTS highlight_score;
