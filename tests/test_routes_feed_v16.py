"""W3.T7 — routes/feed.py v16.0 改动单测

覆盖 PRD §4.9.2 Section pill 矩阵 + §4.9.5 S2/S5/S12 + decision-anchor #17/#18/#19:
- GET /api/feed/platforms 返回 category_counts (per-platform L1 分布,按数量降序)
- search:% 数据不进 source_counts / category_counts (W3.T6 显式过滤)
- GET /api/feed/platforms/more?category=xxx 走 L1 数组过滤
- GET /api/feed/platforms/more 不传 category 时返回全部 (向后兼容)

铁律：tempfile DB（不污染主仓库 data/feed.db；feedback_qa_fixture_isolation_not_production_db）
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "src"))


@pytest.fixture
def app_with_tmp_db(monkeypatch):
    """Spin up FastAPI app pointed at a tempfile DB; legacy-trusted (no auth required).

    legacy_authenticated 是 routes/feed.py 判断 public_only 的反向标志位；
    不模拟登录，借由 monkeypatching middleware 把每个请求标为 legacy 信任，
    保证 manual 平台数据可见、避免 anonymous gate 干扰本测试。
    """
    monkeypatch.setenv("JWT_SECRET", "v16-test-secret-needs-to-be-32-chars-long!!")
    monkeypatch.setenv("RATELIMIT_ENABLED", "false")

    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()

    import db as _db
    monkeypatch.setattr(_db, "DB_PATH", tmp.name)
    _db._item_status_has_user_id = None

    import app as app_mod
    app_mod.app.state.limiter.enabled = False

    # Reset the v16.0 cache between tests (5min TTL would otherwise cross-pollinate)
    import routes.feed as feed_route
    feed_route._CATEGORY_COUNTS_CACHE.clear()

    # 跳过 anonymous public_only gate（避免 manual 平台数据被隐藏 + manual 测试外干扰）；
    # 测试聚焦 v16.0 数据形态而非鉴权回归（鉴权另由 test_owner_isolation.py 覆盖）。
    monkeypatch.setattr(feed_route, "_is_anonymous_public_request", lambda req: False)
    monkeypatch.setattr(feed_route, "_manual_owner_user_id", lambda req: None)

    yield app_mod.app

    try:
        os.unlink(tmp.name)
    except OSError:
        pass


def _insert_item(conn, *, item_id, platform, source, ai_categories,
                 fetched_at=None, visible=1):
    fetched_at = fetched_at or datetime.now().isoformat()
    cats_json = json.dumps(ai_categories) if ai_categories is not None else None
    metrics = json.dumps({"stars": 100})
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
# Test 1: /api/feed/platforms 返回 category_counts，按数量降序
# ============================================================
def test_platforms_returns_category_counts_for_l1_sections(app_with_tmp_db):
    import db
    conn = db.get_conn()
    # GitHub: coding=3, products=1, tech=1
    _insert_item(conn, item_id="g1", platform="github",
                 source="trending:zh", ai_categories=["coding"])
    _insert_item(conn, item_id="g2", platform="github",
                 source="trending:zh", ai_categories=["coding"])
    _insert_item(conn, item_id="g3", platform="github",
                 source="awesome:o/r", ai_categories=["coding", "products"])
    _insert_item(conn, item_id="g4", platform="github",
                 source="trending:global", ai_categories=["tech"])
    conn.close()

    client = TestClient(app_with_tmp_db)
    resp = client.get("/api/feed/platforms")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "category_counts" in body, f"missing category_counts in response keys: {list(body.keys())}"
    gh_counts = body["category_counts"].get("github")
    assert gh_counts is not None, f"github absent from category_counts: {body['category_counts']}"
    assert gh_counts == {"coding": 3, "tech": 1, "products": 1}, gh_counts
    # dict 保持插入顺序 → 第一个 key 是 coding（cnt DESC）
    assert list(gh_counts.keys())[0] == "coding"


# ============================================================
# Test 2: search:% 数据不进 source_counts / category_counts
# ============================================================
def test_platforms_excludes_search_source(app_with_tmp_db):
    import db
    conn = db.get_conn()
    _insert_item(conn, item_id="ok", platform="github",
                 source="trending:zh", ai_categories=["coding"])
    _insert_item(conn, item_id="searched", platform="github",
                 source="search:claude", ai_categories=["models"])
    conn.close()

    client = TestClient(app_with_tmp_db)
    resp = client.get("/api/feed/platforms")
    assert resp.status_code == 200, resp.text
    body = resp.json()

    # source_counts 不含 search:% (W3.T6 _add_display_visibility 过滤)
    gh_sources = body["source_counts"].get("github", {})
    assert "search:claude" not in gh_sources, f"search:% leaked into source_counts: {gh_sources}"
    assert gh_sources.get("trending:zh") == 1

    # category_counts 同样不包含被过滤掉的 models
    gh_cats = body["category_counts"].get("github", {})
    assert "models" not in gh_cats, f"search:% leaked into category_counts: {gh_cats}"
    assert gh_cats == {"coding": 1}


# ============================================================
# Test 3: /api/feed/platforms/more?category=xxx 走 L1 过滤
# ============================================================
def test_platform_list_accepts_category_filter(app_with_tmp_db):
    import db
    conn = db.get_conn()
    _insert_item(conn, item_id="c1", platform="github",
                 source="trending:zh", ai_categories=["coding"])
    _insert_item(conn, item_id="c2", platform="github",
                 source="trending:zh", ai_categories=["coding", "products"])
    _insert_item(conn, item_id="t1", platform="github",
                 source="trending:zh", ai_categories=["tech"])
    conn.close()

    client = TestClient(app_with_tmp_db)
    resp = client.get("/api/feed/platforms/more?platform=github&category=coding")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["platform"] == "github"
    assert body["category"] == "coding"
    ids = {it["id"] for it in body["items"]}
    assert ids == {"c1", "c2"}, f"coding filter mismatch: {ids}"

    # tech filter 只应留 t1
    resp2 = client.get("/api/feed/platforms/more?platform=github&category=tech")
    assert resp2.status_code == 200
    ids2 = {it["id"] for it in resp2.json()["items"]}
    assert ids2 == {"t1"}, f"tech filter mismatch: {ids2}"


# ============================================================
# Test 4: /api/feed/platforms/more 不传 category 返回全部
# ============================================================
def test_platform_list_no_category_returns_all(app_with_tmp_db):
    import db
    conn = db.get_conn()
    _insert_item(conn, item_id="r1", platform="github",
                 source="trending:zh", ai_categories=["coding"])
    _insert_item(conn, item_id="r2", platform="github",
                 source="trending:zh", ai_categories=["models"])
    _insert_item(conn, item_id="r3", platform="github",
                 source="trending:zh", ai_categories=["tech"])
    conn.close()

    client = TestClient(app_with_tmp_db)
    resp = client.get("/api/feed/platforms/more?platform=github")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["category"] is None
    ids = {it["id"] for it in body["items"]}
    assert ids == {"r1", "r2", "r3"}, f"expected all 3 items: {ids}"
