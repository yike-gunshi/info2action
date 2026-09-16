# Migration 维护约定

## 历史编号碰撞

以下同号 migration 是并行分支各自取“下一个号”造成的历史事实。根据 Git 提交时间推断，实际执行顺序如下：

- `0015_action_detail_read_models.sql`：行动详情读模型，提交于 2026-05-23 09:56，先执行；`0015_info_read_model_compound_scopes.sql`：信息读模型复合 scope，提交于 2026-05-23 16:35，后执行。两者内容域不相交，实际未互相冲突。
- `0029_action_generation_daily_quota.sql`：行动生成每日配额，提交于 2026-07-04 20:00，先执行；`0029_sources_registry.sql`：来源注册表，提交于 2026-07-05 08:31，后执行。
- `0030_actions_steps_column.sql`：行动步骤字段，提交于 2026-07-04 20:45，先执行；`0030_sources_failure_tracking.sql`：来源失败追踪，提交于 2026-07-06 01:20，后执行。

这些文件已在生产执行过，迁移工具按文件名记账，永远不要重命名、修改或删除。

## 新 migration 命名约定

后续 migration 一律使用 `YYYYMMDDHHMM_描述.sql` 时间戳前缀，避免并行分支发生编号碰撞。该前缀排序在所有历史 `00xx` migration 之后，符合 Supabase CLI 的记账顺序。

## pg_cron 单一出处规则

cron 定义只允许出现在专门的 cron migration 中，禁止在功能 migration 中顺手 unschedule 或 reschedule。

当前事实：MV `remote_poc.mv_items_top_per_platform` 及其 refresh cron job 已于 perf-hotcold-v27（2026-07-12）在生产删除（见 `docs/ENGINEERING-CHANGELOG.md` 与 `src/remote_db/stats_misc.py` 的 docstring），当前生产不应存在任何针对它的 refresh job。0027 注释中的"恢复 SQL"是过时状态，不要照抄。

将来若需新增 cron，必须先核对生产 `cron.job` 表的实存状态，再写专门的 cron migration；禁止用 `EXCEPTION WHEN OTHERS` 吞掉 schedule 失败。
