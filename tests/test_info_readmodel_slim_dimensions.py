"""ENG-0710 读模型降维瘦身(调研文档选项 g,升配已否决后的容量对策)。

砍单:停止物化 section_subcategory(411 scopes/48,360 行)与 group_source
(285 scopes/22,436 行)两个维度——scope 数 -64%(1085→389)、行数 -18.7%、
delta fan-out 5.3→4.3。这两类视图改走 live 查询:
- group_source:query_feed_by_platform 的 live 回落已存在且不受 circuit 门控;
- section_subcategory:query_feed_by_category 需豁免 env 级 live 关闭
  (INFO2ACTION_REMOTE_FEED_LIVE_DISABLED),否则生产变空降级页;
  运行时熔断(live 刚失败后的保护窗)仍必须尊重。

维度目录读取(all/source/category/section_category)与前端 pills 数据源
(taxonomy 配置)均不依赖被砍维度,已调研确认。
"""
import inspect
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
import remote_db  # noqa: E402


# perf-v27 P4(目标架构定稿 §5-5):物化只剩 section_category(信息默认页
# =每模块「全部」pill 首屏);其余全部维度改走 live 现场查。
# ENG-0710 砍 group_source/section_subcategory 是第一刀,本次是收口。
KEPT_DIMENSIONS = ["'section_category'"]
CUT_DIMENSIONS = [
    "'group_source'", "'section_subcategory'",
    "'all'::text AS dimension", "'source'::text AS dimension",
    "'group'::text AS dimension", "'category'::text AS dimension",
]


# ══════════════════ Task-01: 物化 SQL 不再生成被砍维度 ══════════════════


def test_scope_rows_select_drops_cut_dimensions():
    """delta/incremental 共用的 scope 行生成 SQL 不得再爆炸出被砍维度。"""
    sql = remote_db._info_read_model_scope_rows_select("pg_temp.info_read_model_delta")
    for dim in CUT_DIMENSIONS:
        assert dim not in sql, f'{dim} 仍在 scope 生成 SQL 中——每条新内容仍会向该维度复制行'
    for dim in KEPT_DIMENSIONS:
        assert dim in sql, f'保留维度 {dim} 意外消失'
    # 子板块的 LATERAL 展开(一条内容爆 N 个子板块行)必须随维度一起消失
    assert 'ai_subcategories' not in sql


def test_full_rebuild_uses_shared_scope_select_helper():
    """perf-v27 P4: 全量重建不再内联维度 UNION,必须走共享 helper(单一事实源);窗口与封顶必须在场。"""
    src = inspect.getsource(remote_db.refresh_info_read_model)
    assert "_info_read_model_scope_rows_select(" in src
    for dim in CUT_DIMENSIONS:
        assert dim not in src, f'{dim} 仍在全量重建内联 SQL 中'
    assert "info_window_days" in src
    assert "scope_top_n" in src


def test_incremental_rebuild_uses_shared_helper_only():
    """增量重建路径不得内联自己的维度 UNION(必须走共享 helper,否则砍维度会漏)。"""
    src = inspect.getsource(remote_db.refresh_info_read_model_incremental)
    for dim in CUT_DIMENSIONS:
        assert dim not in src, f'{dim} 出现在增量重建源码中(应只经 helper 生成 scope 行)'


# ══════════════════ Task-03: prewarm 不再枚举被砍维度 ══════════════════


def test_prewarm_scope_whitelist_drops_cut_dimensions():
    """prewarm 的 hot_scopes 白名单不再给被砍维度分配预热名额(释放 max_scopes 预算)。"""
    src = inspect.getsource(remote_db.prewarm_info_read_model_pages)
    assert "dimension = 'section_category'" in src
    assert "dimension IN (" not in src


# ══════════════════ Task-02: 子板块 live 豁免(env 关闭豁免/运行时熔断尊重) ══════════════════


from contextlib import contextmanager  # noqa: E402


class _LiveFakeConn:
    def __init__(self):
        self.sqls: list[str] = []

    def execute(self, sql, params=None):
        self.sqls.append(sql)
        return self

    def fetchone(self):
        return {"n": 2}


_LIVE_CONNS: list = []
_TIMEOUTS: list = []


@pytest.fixture
def _live_env(monkeypatch):
    """read model 未命中 + env 级 live 关闭的生产形态;live SQL 层全 mock。"""
    _LIVE_CONNS.clear()
    _TIMEOUTS.clear()
    monkeypatch.setenv(remote_db.REMOTE_FEED_LIVE_DISABLED_ENV, "1")
    monkeypatch.setattr(remote_db, "_REMOTE_FEED_LIVE_CIRCUIT_OPEN_UNTIL", 0.0)
    monkeypatch.setattr(remote_db, "remote_schema", lambda: "remote_poc")
    monkeypatch.setattr(
        remote_db, "_set_short_statement_timeout",
        lambda conn, ms=0: _TIMEOUTS.append(ms),
    )
    monkeypatch.setattr(
        remote_db, "_query_feed_by_category_read_model", lambda **kw: None,
    )
    monkeypatch.setattr(
        remote_db, "_fetch_items",
        lambda *a, **kw: [{"id": "a"}, {"id": "b"}],
    )

    @contextmanager
    def _fake_connect():
        conn = _LiveFakeConn()
        _LIVE_CONNS.append(conn)
        yield conn

    monkeypatch.setattr(remote_db, "connect", _fake_connect)


def test_subcategory_falls_back_to_live_despite_env_live_disabled(_live_env):
    """核心断言:子板块视图不再物化后,env 级 LIVE_DISABLED 不得把它打成空降级页。"""
    result = remote_db.query_feed_by_category(
        category="slim-test-products", subcategory="chatbot", limit=10,
    )
    assert not result.get("degraded"), (
        f'子板块请求在 LIVE_DISABLED=1 下返回降级空页 = 砍维度后生产子板块全空,实际 {result}'
    )
    assert len(result["items"]) == 2 and result["total"] == 2
    # 生产实测:EXISTS+jsonb_array_elements_text 形态 11.75s(全表 SRF),
    # @> 包含形态走既有 GIN(jsonb_path_ops)索引 0.8-4s——live 必须用 @>。
    live_sqls = "\n".join(sql for conn in _LIVE_CONNS for sql in conn.sqls)
    assert "jsonb_array_elements_text(i.ai_subcategories)" not in live_sqls, (
        'live 计数仍用 SRF 展开形态,1GB 实例上必超 2.5s 预算'
    )
    assert "i.ai_subcategories @> " in live_sqls
    # 热门子板块冷缓存 @> count ~4s > 常规 live 预算 2.5s:子板块 live 必须有独立更宽预算
    assert _TIMEOUTS and max(_TIMEOUTS) >= 10000, (
        f'子板块 live 未获得 ≥10s 独立预算,实际 SET 过的超时: {_TIMEOUTS}'
    )


def test_subcategory_still_respects_runtime_breaker(_live_env, monkeypatch):
    """边界:live 刚失败后的运行时熔断窗内,子板块请求仍必须降级保护库。"""
    monkeypatch.setattr(
        remote_db, "_REMOTE_FEED_LIVE_CIRCUIT_OPEN_UNTIL",
        __import__("time").monotonic() + 60,
    )
    result = remote_db.query_feed_by_category(
        category="slim-test-products2", subcategory="chatbot", limit=10,
    )
    assert result.get("degraded") is True and result["items"] == []


def test_plain_category_keeps_env_circuit_behavior(_live_env):
    """回归:非子板块的 category 请求仍受 env 级 live 关闭保护(行为不变)。"""
    result = remote_db.query_feed_by_category(category="slim-test-products3", limit=10)
    assert result.get("degraded") is True and result["items"] == []


# ══════════ review-0710: 搜索×被砍维度组合不得因"搜索读模型不可用"降级 ══════════


def test_search_plus_subcategory_falls_through_to_live(_live_env, monkeypatch):
    """全局搜索激活时点子板块 pill(FeedSection.tsx:115 组合请求):
    子板块搜索 scope 已不物化,搜索读模型返回 None 后必须放行到 live,
    不得命中 info_search_read_model_unavailable 降级短路。"""
    monkeypatch.setattr(
        remote_db, "_query_feed_by_category_search_read_model", lambda **kw: None,
    )
    monkeypatch.setattr(
        remote_db, "_can_use_info_search_read_model", lambda **kw: True,
    )
    result = remote_db.query_feed_by_category(
        category="slim-test-products4", subcategory="chatbot", search="agent", limit=10,
    )
    assert not result.get("degraded"), (
        f'搜索+子板块组合被降级短路拦下(应放行到 live),实际 {result}'
    )
    assert len(result["items"]) == 2


def test_search_plus_group_source_falls_through_to_live(_live_env, monkeypatch):
    """语鲸分组×源视图内搜索:group_source scope 已不物化,同样放行到 live。"""
    monkeypatch.setattr(
        remote_db, "_query_feed_by_platform_search_read_model", lambda **kw: None,
    )
    monkeypatch.setattr(
        remote_db, "_query_feed_by_platform_read_model", lambda **kw: None,
    )
    monkeypatch.setattr(
        remote_db, "_can_use_info_search_read_model", lambda **kw: True,
    )
    result = remote_db.query_feed_by_platform(
        platform="lingowhale", group="AI-机构", source="虎嗅-前沿科技-网站",
        search="agent", limit=10,
    )
    assert not result.get("degraded"), (
        f'搜索+分组×源组合被降级短路拦下(应放行到 live),实际 {result}'
    )
