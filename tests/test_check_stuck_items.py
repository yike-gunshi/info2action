"""BF-0804-1: 聚类补救 SLA 告警的查询守卫。

告警要么长红要么永不响,都等于没有。这里钉住窗口的两端:必须只数"够老
(超 SLA)但还救得回来(仍在回溯窗口内)"的那段,并且只数已打分非 drop 的
条目——drop 的本来就不该进簇,算进去会把告警噪音顶满。
"""
import importlib.util
import os
import sys

_OPS = os.path.join(os.path.dirname(__file__), "..", "ops", "check_stuck_items.py")
_spec = importlib.util.spec_from_file_location("check_stuck_items", _OPS)
check_stuck_items = importlib.util.module_from_spec(_spec)
sys.modules["check_stuck_items"] = check_stuck_items
_spec.loader.exec_module(check_stuck_items)


def test_sql_bounds_both_ends_of_the_window():
    sql = check_stuck_items.build_sql("remote_poc")
    # 下界:够老才算违约
    assert "fetched_at <= now() - (%(sla_hours)s * interval '1 hour')" in sql
    # 上界:早于回溯窗口的已经救不回来,计入会让告警长红
    assert "fetched_at >= now() - (%(lookback_hours)s * interval '1 hour')" in sql


def test_sql_only_counts_scored_non_drop_items_awaiting_a_cluster():
    sql = check_stuck_items.build_sql("remote_poc")
    assert "cluster_id IS NULL" in sql
    assert "highlight_verdict IS NOT NULL" in sql
    assert "highlight_verdict <> 'drop'" in sql
    assert "ai_summary IS NOT NULL AND ai_summary <> ''" in sql


def test_sql_uses_the_given_schema():
    assert "FROM remote_poc.items" in check_stuck_items.build_sql("remote_poc")


def test_sla_defaults_match_the_6_hour_promise():
    assert check_stuck_items.SLA_HOURS == 6
    assert check_stuck_items.LOOKBACK_HOURS == 72


def test_summary_sql_excludes_clusters_hidden_by_the_qualify_gate():
    sql = check_stuck_items.build_summary_sql("remote_poc")
    assert "c.pending_is_visible_in_feed != 0" in sql
    assert "c.ai_title_draft IS NULL" in sql
    assert "c.ai_summary_draft IS NULL" in sql
    assert "c.ai_key_points_draft IS NULL" in sql


def test_summary_sql_only_counts_unpublished_active_clusters():
    sql = check_stuck_items.build_summary_sql("remote_poc")
    assert "COALESCE(c.published_run_id, -1) != c.last_touched_run_id" in sql
    assert "c.archived IS NOT TRUE" in sql
    assert "c.merged_into IS NULL" in sql


def test_summary_sql_bounds_both_ends_of_the_item_window():
    sql = check_stuck_items.build_summary_sql("remote_poc")
    assert "i.fetched_at <= now() - (%(sla_hours)s * interval '1 hour')" in sql
    assert "i.fetched_at >= now() - (%(lookback_hours)s * interval '1 hour')" in sql


def test_summary_sql_uses_the_given_schema():
    sql = check_stuck_items.build_summary_sql("remote_poc")
    assert "FROM remote_poc.clusters c" in sql
    assert "FROM remote_poc.cluster_items ci" in sql
    assert "JOIN remote_poc.items i" in sql


def test_breach_when_either_backlog_exceeds_threshold():
    assert check_stuck_items.is_breach(51, 0, 50)
    assert check_stuck_items.is_breach(0, 51, 50)
    assert not check_stuck_items.is_breach(50, 50, 50)
