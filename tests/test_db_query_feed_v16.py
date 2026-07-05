"""W3.T6 — db.py v16.0 改动单测

覆盖 PRD §4.9.5 S11 + §4.9.2 L1 pill 维度:
- _add_display_visibility: source NOT LIKE 'search:%' 参数化排除
- _add_category_filter: ai_categories JSON array 任意元素匹配
- query_feed_by_platform(category=...): L1 维度 pill 切换
- get_category_counts(): 聚合近 7 天 L1 分布

铁律：tempfile DB（不污染主仓库 data/feed.db）；SQL 必须参数化（无 SQL injection 风险）。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import datetime, timedelta

import pytest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "src"))


@pytest.fixture
def tmp_db(monkeypatch):
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    monkeypatch.setattr("db.DB_PATH", tmp.name)
    import db as _db
    _db._item_status_has_user_id = None
    yield tmp.name
    try:
        os.unlink(tmp.name)
    except OSError:
        pass


def _insert_item(conn, *, item_id, platform, source, ai_categories,
                 fetched_at=None, visible=1, stars=100):
    """Helper: insert minimal item row for query tests."""
    fetched_at = fetched_at or datetime.now().isoformat()
    metrics = json.dumps({"stars": stars, "forks": 1, "stars_today": 0})
    cats_json = json.dumps(ai_categories) if ai_categories is not None else None
    conn.execute("""
        INSERT INTO items (
            id, platform, source, title, content, ai_summary, ai_keywords,
            ai_category, ai_categories, ai_subcategories, multi_l1_reason,
            relevance_score, fetched_at, author_name, metrics_json,
            tags_json, visible
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        item_id, platform, source, f"title {item_id}", "content", "summary",
        "k1,k2", (ai_categories[0] if ai_categories else None),
        cats_json, None, None,
        50, fetched_at, "author", metrics, None, visible,
    ))
    conn.commit()


# ============================================================
# Test 1: source LIKE 'search:%' 数据被 _add_display_visibility 排除
# ============================================================
def test_query_feed_by_platform_excludes_search_source(tmp_db):
    import db
    conn = db.get_conn()

    # Insert: 1 normal item + 1 历史 search 数据
    _insert_item(conn, item_id="gh1", platform="github",
                 source="trending:zh", ai_categories=["coding"])
    _insert_item(conn, item_id="gh2", platform="github",
                 source="search:claude", ai_categories=["coding"])

    rows = db.query_feed_by_platform(conn, "github", limit=50)
    assert len(rows) == 1, f"expected 1 normal item, got {len(rows)}"
    assert rows[0]["id"] == "gh1"
    assert rows[0]["source"] == "trending:zh"
    conn.close()


# ============================================================
# Test 2: 同样规则也覆盖 query_feed (推荐页)
# ============================================================
def test_query_feed_also_excludes_search_source(tmp_db):
    import db
    conn = db.get_conn()
    _insert_item(conn, item_id="t1", platform="twitter",
                 source="following", ai_categories=["models"])
    _insert_item(conn, item_id="t2", platform="twitter",
                 source="search:gpt", ai_categories=["models"])

    rows = db.query_feed(conn, platform="twitter", limit=50)
    ids = {r["id"] for r in rows}
    assert "t1" in ids
    assert "t2" not in ids, "search:% data must be hidden across all query paths"
    conn.close()


# ============================================================
# Test 3: category 过滤 — ai_categories 数组任意元素匹配
# ============================================================
def test_query_feed_by_platform_filters_by_category(tmp_db):
    import db
    conn = db.get_conn()
    # item gh1 ∈ {products, coding}, gh2 ∈ {tech}, gh3 ∈ {coding}
    _insert_item(conn, item_id="gh1", platform="github",
                 source="trending:zh", ai_categories=["products", "coding"])
    _insert_item(conn, item_id="gh2", platform="github",
                 source="trending:global", ai_categories=["tech"])
    _insert_item(conn, item_id="gh3", platform="github",
                 source="awesome:owner/repo", ai_categories=["coding"])

    coding_rows = db.query_feed_by_platform(conn, "github", limit=50, category="coding")
    coding_ids = {r["id"] for r in coding_rows}
    assert coding_ids == {"gh1", "gh3"}, f"coding filter mismatch: {coding_ids}"

    tech_rows = db.query_feed_by_platform(conn, "github", limit=50, category="tech")
    assert {r["id"] for r in tech_rows} == {"gh2"}

    products_rows = db.query_feed_by_platform(conn, "github", limit=50, category="products")
    assert {r["id"] for r in products_rows} == {"gh1"}, "multi-L1 item must match each L1"
    conn.close()


# ============================================================
# Test 4: category=None → 不加过滤（向后兼容）
# ============================================================
def test_query_feed_by_platform_no_category_returns_all(tmp_db):
    import db
    conn = db.get_conn()
    _insert_item(conn, item_id="r1", platform="github",
                 source="trending:zh", ai_categories=["coding"])
    _insert_item(conn, item_id="r2", platform="github",
                 source="trending:zh", ai_categories=["models"])

    rows = db.query_feed_by_platform(conn, "github", limit=50)  # no category
    assert len(rows) == 2
    rows_none = db.query_feed_by_platform(conn, "github", limit=50, category=None)
    assert len(rows_none) == 2
    rows_empty = db.query_feed_by_platform(conn, "github", limit=50, category="")
    assert len(rows_empty) == 2
    conn.close()


# ============================================================
# Test 5: get_category_counts 聚合近 7 天 L1 分布，按数量降序
# ============================================================
def test_get_category_counts_aggregates_by_l1(tmp_db):
    import db
    conn = db.get_conn()
    # 3 个 coding，1 个 tech，1 个 products + coding（multi-L1）
    _insert_item(conn, item_id="c1", platform="github", source="trending:zh",
                 ai_categories=["coding"])
    _insert_item(conn, item_id="c2", platform="github", source="trending:zh",
                 ai_categories=["coding"])
    _insert_item(conn, item_id="c3", platform="github", source="trending:zh",
                 ai_categories=["coding"])
    _insert_item(conn, item_id="t1", platform="github", source="trending:zh",
                 ai_categories=["tech"])
    _insert_item(conn, item_id="p1", platform="github", source="trending:zh",
                 ai_categories=["products", "coding"])

    counts = db.get_category_counts(conn, "github", days=7)
    # coding=4(c1+c2+c3+p1), tech=1, products=1
    assert counts == {"coding": 4, "tech": 1, "products": 1}, f"got {counts}"
    # ordered by cnt DESC：dict 保持插入顺序，验证 coding 在前
    keys = list(counts.keys())
    assert keys[0] == "coding"
    conn.close()


# ============================================================
# Test 6: get_category_counts 排除 search:% 数据 + 过期数据
# ============================================================
def test_get_category_counts_excludes_search_and_old(tmp_db):
    import db
    conn = db.get_conn()
    old = (datetime.now() - timedelta(days=10)).isoformat()
    _insert_item(conn, item_id="recent", platform="github", source="trending:zh",
                 ai_categories=["models"])
    _insert_item(conn, item_id="old", platform="github", source="trending:zh",
                 ai_categories=["models"], fetched_at=old)
    _insert_item(conn, item_id="search", platform="github",
                 source="search:claude", ai_categories=["models"])

    counts = db.get_category_counts(conn, "github", days=7)
    assert counts == {"models": 1}, f"only 'recent' should count: {counts}"
    conn.close()


# ============================================================
# Test 8 (BF-0512-2+4): source 维度 pill 放行 NULL ai_categories
# ============================================================
def test_query_feed_by_platform_source_pill_includes_null_ai_categories(tmp_db):
    """B 站 / Twitter / 公众号 等 source 维度 pill 切换时，必须放行 ai_categories=NULL
    的历史 item。否则 v16.0 W4.T11 拆分 source pill 后，B 站 watch_later 1024 条
    + GitHub 33% NULL 历史数据全部消失（用户人工验收 BF-0512-2 报）。"""
    import db
    conn = db.get_conn()

    # 模拟历史 NULL ai_categories item (v4.0 之前 enrich 没跑全)
    _insert_item(conn, item_id="bili_old1", platform="bilibili",
                 source="watch_later", ai_categories=None)
    _insert_item(conn, item_id="bili_old2", platform="bilibili",
                 source="hot", ai_categories=None)
    # 新 item 有 ai_categories
    _insert_item(conn, item_id="bili_new", platform="bilibili",
                 source="hot", ai_categories=["tutorials"])

    # source 维度切换（无 category）→ 必须包含 NULL item
    rows_watch = db.query_feed_by_platform(conn, "bilibili", limit=50, source="watch_later")
    assert {r["id"] for r in rows_watch} == {"bili_old1"}, \
        f"watch_later 应返回 NULL item, got: {[r['id'] for r in rows_watch]}"

    rows_hot = db.query_feed_by_platform(conn, "bilibili", limit=50, source="hot")
    assert {r["id"] for r in rows_hot} == {"bili_old2", "bili_new"}, \
        f"hot 应返回 NULL + 有 cat 两条, got: {[r['id'] for r in rows_hot]}"

    # 无 source 无 category（「全部」pill）→ 全部
    rows_all = db.query_feed_by_platform(conn, "bilibili", limit=50)
    assert {r["id"] for r in rows_all} == {"bili_old1", "bili_old2", "bili_new"}
    conn.close()


# ============================================================
# Test 9 (BF-0512-4): L1 category 维度仍 require ai_categories
# ============================================================
def test_query_feed_by_platform_l1_pill_excludes_null(tmp_db):
    """L1 维度 pill 切换（category 传入）时，NULL ai_categories item 不应展示
    （NULL item 不归属任何 L1）。"""
    import db
    conn = db.get_conn()

    _insert_item(conn, item_id="gh_null", platform="github",
                 source="trending:zh", ai_categories=None)
    _insert_item(conn, item_id="gh_coding", platform="github",
                 source="trending:zh", ai_categories=["coding"])
    _insert_item(conn, item_id="gh_models", platform="github",
                 source="trending:zh", ai_categories=["models"])

    # category=coding → 只 coding，不含 NULL
    rows = db.query_feed_by_platform(conn, "github", limit=50, category="coding")
    assert {r["id"] for r in rows} == {"gh_coding"}

    # category=None（无 L1 切换）→ 含 NULL item
    rows_all = db.query_feed_by_platform(conn, "github", limit=50)
    assert {r["id"] for r in rows_all} == {"gh_null", "gh_coding", "gh_models"}, \
        "无 category 时必须放行 NULL item"
    conn.close()


# ============================================================
# Test 10 (BF-0512-4): API 返回字段含 ai_categories
# ============================================================
def test_query_feed_by_platform_returns_ai_categories_field(tmp_db):
    """API 响应必须含 ai_categories 字段（不只 ai_category 单字符串），
    供前端 client-side 显示 / 二次过滤。BF-0512-4 用户报「item 没分组」
    根因之一是 cols 缺 ai_categories 字段。"""
    import db
    conn = db.get_conn()
    _insert_item(conn, item_id="gh_multi", platform="github",
                 source="trending:zh", ai_categories=["coding", "tools"])

    rows = db.query_feed_by_platform(conn, "github", limit=50)
    assert len(rows) == 1
    row = rows[0]
    assert "ai_categories" in row, f"row 缺 ai_categories 字段: {row.keys()}"
    # _normalize_item_category 会把 ai_categories JSON string 解析为 list
    assert row["ai_categories"] == ["coding", "tools"], f"got {row['ai_categories']}"
    conn.close()


# ============================================================
# BF-0512-6: 「未分类」L1 pill — UNCATEGORIZED_SENTINEL 分支测试
# ============================================================
def test_query_feed_by_platform_uncategorized_returns_null_ai_categories(tmp_db):
    """category='__uncategorized__' 应返回 ai_categories=NULL 的 item，
    解决 BF-0512-6「全部 pill 数字 vs L1 pill 加和不对账」问题。"""
    import db
    conn = db.get_conn()

    _insert_item(conn, item_id="gh_null1", platform="github",
                 source="trending:zh", ai_categories=None)
    _insert_item(conn, item_id="gh_null2", platform="github",
                 source="trending:zh", ai_categories=None)
    _insert_item(conn, item_id="gh_coding", platform="github",
                 source="trending:zh", ai_categories=["coding"])

    rows = db.query_feed_by_platform(
        conn, "github", limit=50, category=db.UNCATEGORIZED_SENTINEL
    )
    assert {r["id"] for r in rows} == {"gh_null1", "gh_null2"}, \
        f"未分类 pill 应只返 NULL item, got {[r['id'] for r in rows]}"

    # 对照: category='coding' 仍只返 coding，不含 NULL
    rows_coding = db.query_feed_by_platform(conn, "github", limit=50, category="coding")
    assert {r["id"] for r in rows_coding} == {"gh_coding"}
    conn.close()


def test_get_category_counts_includes_uncategorized(tmp_db):
    """get_category_counts 应在 NULL ai_categories item 数 > 0 时返回
    UNCATEGORIZED_SENTINEL 键，让前端「未分类」pill 能显示数字。"""
    import db
    conn = db.get_conn()
    _insert_item(conn, item_id="g1", platform="github", source="trending:zh",
                 ai_categories=["coding"])
    _insert_item(conn, item_id="g2", platform="github", source="trending:zh",
                 ai_categories=["coding"])
    _insert_item(conn, item_id="g_null1", platform="github", source="trending:zh",
                 ai_categories=None)
    _insert_item(conn, item_id="g_null2", platform="github", source="trending:zh",
                 ai_categories=None)
    _insert_item(conn, item_id="g_null3", platform="github", source="trending:zh",
                 ai_categories=None)
    counts = db.get_category_counts(conn, "github", days=7)
    assert counts.get("coding") == 2, f"coding 应=2, got {counts}"
    assert counts.get(db.UNCATEGORIZED_SENTINEL) == 3, \
        f"未分类应=3, got {counts}"
    conn.close()


def test_get_category_counts_omits_uncategorized_when_zero(tmp_db):
    """无 NULL ai_categories item 时，counts 不应含 UNCATEGORIZED_SENTINEL 键
    （前端不渲染空的「未分类」pill）。"""
    import db
    conn = db.get_conn()
    _insert_item(conn, item_id="r1", platform="reddit", source="ClaudeAI",
                 ai_categories=["models"])
    _insert_item(conn, item_id="r2", platform="reddit", source="ClaudeAI",
                 ai_categories=["models"])
    counts = db.get_category_counts(conn, "reddit", days=7)
    assert counts == {"models": 2}
    assert db.UNCATEGORIZED_SENTINEL not in counts
    conn.close()


# ============================================================
# Test 7: SQL injection 防御 — search:% 用参数化
# ============================================================
def test_search_filter_uses_parameterized_query(tmp_db):
    """构造一个含 SQL meta 字符的 source 不会被注入式破坏 query."""
    import db
    conn = db.get_conn()
    # source 含单引号、注释符等
    _insert_item(conn, item_id="evil", platform="github",
                 source="trending:zh' OR 1=1 --",
                 ai_categories=["coding"])
    _insert_item(conn, item_id="ok", platform="github",
                 source="trending:zh", ai_categories=["coding"])

    rows = db.query_feed_by_platform(conn, "github", limit=50)
    # 两条都不是 search: 开头 → 都返回；查询不会因 evil source 解析失败
    ids = {r["id"] for r in rows}
    assert ids == {"evil", "ok"}, f"got {ids}"
    conn.close()
