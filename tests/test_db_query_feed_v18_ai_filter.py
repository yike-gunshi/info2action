"""v18.0 nav-merge: query_feed_platforms AI 过滤测试

PRD §Spec-2 锁定：信息 tab 复用 query_feed_platforms，必须强制 AI 过滤
口径：(ai_category IS NOT NULL AND ai_category != 'other') OR
     (ai_categories IS NOT NULL AND ai_categories NOT IN ('[]','null','"null"'))

铁律：tempfile DB；只断言 AI 过滤行为，不断言其他副作用。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import datetime

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


def _insert(conn, *, item_id, platform, ai_category, ai_categories,
            source="following", visible=1):
    fetched_at = datetime.now().isoformat()
    metrics = json.dumps({"stars": 100})
    if ai_categories is None:
        cats_json = None
    elif isinstance(ai_categories, str):
        cats_json = ai_categories  # 直传 raw json 字符串（测 NULL 字面量等边界）
    else:
        cats_json = json.dumps(ai_categories)
    conn.execute("""
        INSERT INTO items (
            id, platform, source, title, content, ai_summary, ai_keywords,
            ai_category, ai_categories, ai_subcategories, multi_l1_reason,
            relevance_score, fetched_at, author_name, metrics_json,
            tags_json, visible
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        item_id, platform, source, f"t{item_id}", "c", "s", "k",
        ai_category, cats_json, None, None,
        50, fetched_at, "author", metrics, None, visible,
    ))
    conn.commit()


# ============================================================
# Spec-2.1 留存断言：AI 过滤口径正确性
# ============================================================
def test_query_feed_platforms_includes_items_with_ai_category(tmp_db):
    """ai_category 非 NULL 且非 'other' 的 item SHALL 被保留"""
    import db
    conn = db.get_conn()
    _insert(conn, item_id="a1", platform="twitter",
            ai_category="models", ai_categories=None)
    sections, counts, _ = db.query_feed_platforms(conn)
    assert counts.get("twitter") == 1, counts
    assert sections.get("twitter") and sections["twitter"][0]["id"] == "a1"
    conn.close()


def test_query_feed_platforms_includes_items_with_ai_categories_array(tmp_db):
    """ai_categories 非空 JSON array 的 item SHALL 被保留（即使 ai_category=NULL）"""
    import db
    conn = db.get_conn()
    _insert(conn, item_id="b1", platform="reddit",
            ai_category=None, ai_categories=["coding"])
    sections, counts, _ = db.query_feed_platforms(conn)
    assert counts.get("reddit") == 1, counts
    conn.close()


def test_query_feed_platforms_excludes_items_with_other_category(tmp_db):
    """ai_category='other' 且 ai_categories=NULL 的 item SHALL 被过滤掉"""
    import db
    conn = db.get_conn()
    _insert(conn, item_id="c1", platform="bilibili",
            ai_category="other", ai_categories=None)
    _insert(conn, item_id="c2", platform="bilibili",
            ai_category="models", ai_categories=None)
    sections, counts, _ = db.query_feed_platforms(conn)
    assert counts.get("bilibili") == 1, counts
    assert sections["bilibili"][0]["id"] == "c2"
    conn.close()


def test_query_feed_platforms_excludes_completely_empty(tmp_db):
    """ai_category=NULL 且 ai_categories=NULL 的 item SHALL 被过滤掉"""
    import db
    conn = db.get_conn()
    _insert(conn, item_id="d1", platform="bilibili",
            ai_category=None, ai_categories=None)
    sections, counts, _ = db.query_feed_platforms(conn)
    assert counts.get("bilibili", 0) == 0, counts
    conn.close()


def test_query_feed_platforms_excludes_empty_array_literal(tmp_db):
    """ai_categories='[]' 字面空数组 SHALL 不算"""
    import db
    conn = db.get_conn()
    _insert(conn, item_id="e1", platform="hackernews",
            ai_category=None, ai_categories="[]")
    sections, counts, _ = db.query_feed_platforms(conn)
    assert counts.get("hackernews", 0) == 0, counts
    conn.close()


def test_query_feed_platforms_excludes_null_string_literal(tmp_db):
    """ai_categories='null' / '"null"' 字面 NULL SHALL 不算"""
    import db
    conn = db.get_conn()
    _insert(conn, item_id="f1", platform="rss",
            ai_category=None, ai_categories="null")
    _insert(conn, item_id="f2", platform="rss",
            ai_category=None, ai_categories='"null"')
    sections, counts, _ = db.query_feed_platforms(conn)
    assert counts.get("rss", 0) == 0, counts
    conn.close()


def test_query_feed_platforms_other_category_with_nonempty_array_kept(tmp_db):
    """ai_category='other' 但 ai_categories 非空 → 仍 SHALL 保留（OR 关系）"""
    import db
    conn = db.get_conn()
    _insert(conn, item_id="g1", platform="github",
            ai_category="other", ai_categories=["coding"])
    sections, counts, _ = db.query_feed_platforms(conn)
    assert counts.get("github") == 1, counts
    conn.close()


def test_query_feed_platforms_mixed_realistic_distribution(tmp_db):
    """混合场景：5 条数据，3 个保留 / 2 个过滤"""
    import db
    conn = db.get_conn()
    _insert(conn, item_id="m1", platform="twitter",
            ai_category="models", ai_categories=None)  # 留
    _insert(conn, item_id="m2", platform="twitter",
            ai_category="other", ai_categories=None)   # 过
    _insert(conn, item_id="m3", platform="twitter",
            ai_category=None, ai_categories=["agents"])  # 留
    _insert(conn, item_id="m4", platform="twitter",
            ai_category=None, ai_categories=None)   # 过
    _insert(conn, item_id="m5", platform="twitter",
            ai_category=None, ai_categories="[]")  # 过
    sections, counts, _ = db.query_feed_platforms(conn)
    assert counts.get("twitter") == 2, counts
    ids = {x["id"] for x in sections["twitter"]}
    assert ids == {"m1", "m3"}
    conn.close()
