"""Tests for db.py — core database operations."""
import json
import os
import sqlite3
import sys
import tempfile

import pytest

# Insert project root so we can import db
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
import db as db_mod


@pytest.fixture()
def tmp_db(monkeypatch, tmp_path):
    """Provide a fresh SQLite DB in a temp directory."""
    db_path = str(tmp_path / 'test_feed.db')
    monkeypatch.setattr(db_mod, 'DB_PATH', db_path)
    conn = db_mod.get_conn()
    yield conn
    conn.close()


def _make_item(**overrides):
    base = dict(
        id='test-1', platform='twitter', source='following',
        title='Test title', content='Test content',
        author_name='alice', author_id='a1', author_avatar='',
        url='https://x.com/alice/status/1', cover_url=None,
        media_json=None, metrics_json='{"likes":10}',
        tags_json=None, lang='en', detail_json=None,
        comments_json=None, ai_summary=None,
        relevance_score=5.0, fetched_at='2026-03-18T00:00:00',
        published_at='2026-03-18T00:00:00',
    )
    base.update(overrides)
    return base


# ── upsert & query ──

class TestUpsertAndQuery:
    def test_insert_and_query(self, tmp_db):
        db_mod.batch_upsert(tmp_db, [_make_item()])
        items = db_mod.query_feed(tmp_db, limit=10)
        assert len(items) == 1
        assert items[0]['title'] == 'Test title'

    def test_upsert_updates_metrics(self, tmp_db):
        db_mod.batch_upsert(tmp_db, [_make_item(metrics_json='{"likes":10}')])
        db_mod.batch_upsert(tmp_db, [_make_item(metrics_json='{"likes":99}')])
        items = db_mod.query_feed(tmp_db)
        assert json.loads(items[0]['metrics_json'])['likes'] == 99

    def test_upsert_updates_source(self, tmp_db):
        db_mod.batch_upsert(tmp_db, [_make_item(source='following')])
        db_mod.batch_upsert(tmp_db, [_make_item(source='for_you')])
        items = db_mod.query_feed(tmp_db)
        assert items[0]['source'] == 'for_you'

    def test_upsert_does_not_blank_author(self, tmp_db):
        db_mod.batch_upsert(tmp_db, [_make_item(author_name='alice')])
        db_mod.batch_upsert(tmp_db, [_make_item(author_name='')])
        items = db_mod.query_feed(tmp_db)
        assert items[0]['author_name'] == 'alice'

    def test_query_filter_platform(self, tmp_db):
        db_mod.batch_upsert(tmp_db, [
            _make_item(id='tw-1', platform='twitter'),
            _make_item(id='xhs-1', platform='xiaohongshu'),
        ])
        tw = db_mod.query_feed(tmp_db, platform='twitter')
        assert len(tw) == 1
        assert tw[0]['platform'] == 'twitter'

    def test_query_pagination(self, tmp_db):
        db_mod.batch_upsert(tmp_db, [_make_item(id=f'item-{i}') for i in range(5)])
        page = db_mod.query_feed(tmp_db, limit=2, offset=2)
        assert len(page) == 2

    def test_github_display_requires_min_stars(self, tmp_db):
        db_mod.batch_upsert(tmp_db, [
            _make_item(
                id='gh-49', platform='github', source='trending',
                title='Repo 49', metrics_json='{"stars":49}',
            ),
            _make_item(
                id='gh-50', platform='github', source='trending',
                title='Repo 50', metrics_json='{"stars":50}',
            ),
            _make_item(
                id='gh-missing', platform='github', source='trending',
                title='Repo missing', metrics_json='{}',
            ),
            _make_item(id='tw-1', platform='twitter', title='Twitter item'),
        ])
        tmp_db.execute(
            """UPDATE items
               SET ai_category='ai_tools', ai_categories='["ai_tools"]', visible=1
               WHERE id IN ('gh-49', 'gh-50', 'gh-missing', 'tw-1')"""
        )
        tmp_db.commit()

        feed_ids = {item['id'] for item in db_mod.query_feed(tmp_db)}
        assert 'gh-49' not in feed_ids
        assert 'gh-missing' not in feed_ids
        assert {'gh-50', 'tw-1'} <= feed_ids

        github_ids = [item['id'] for item in db_mod.query_feed(tmp_db, platform='github')]
        assert github_ids == ['gh-50']

        sections, cat_counts = db_mod.query_feed_sections(tmp_db, per_category=None)
        assert cat_counts['ai_tools'] == 2
        assert {item['id'] for item in sections['ai_tools']} == {'gh-50', 'tw-1'}

        platform_sections, platform_counts, source_counts = db_mod.query_feed_platforms(
            tmp_db, per_platform=None,
        )
        assert platform_counts['github'] == 1
        assert source_counts['github']['trending'] == 1
        assert [item['id'] for item in platform_sections['github']] == ['gh-50']


# ── status ──

class TestStatus:
    def test_set_clicked(self, tmp_db):
        db_mod.batch_upsert(tmp_db, [_make_item()])
        db_mod.set_status(tmp_db, 'test-1', 'clicked')
        items = db_mod.query_feed(tmp_db)
        assert items[0]['clicked_at'] is not None

    def test_set_starred(self, tmp_db):
        db_mod.batch_upsert(tmp_db, [_make_item()])
        db_mod.set_status(tmp_db, 'test-1', 'starred')
        items = db_mod.query_feed(tmp_db)
        assert items[0]['starred_at'] is not None

    def test_invalid_action_raises(self, tmp_db):
        with pytest.raises(ValueError, match="Invalid action"):
            db_mod.set_status(tmp_db, 'test-1', 'deleted')

    def test_query_unread(self, tmp_db):
        db_mod.batch_upsert(tmp_db, [
            _make_item(id='read-1'),
            _make_item(id='unread-1'),
        ])
        db_mod.set_status(tmp_db, 'read-1', 'clicked')
        unread = db_mod.query_feed(tmp_db, unread=True)
        assert len(unread) == 1
        assert unread[0]['id'] == 'unread-1'


# ── BF-0420-22: composite-PK item_status + anonymous caller ──

class TestStatusCompositePk:
    """Regression for BF-0420-22: set_status() used to emit
    `ON CONFLICT(item_id)` against the composite-PK `item_status` schema when
    `user_id=None`, crashing with 'ON CONFLICT clause does not match any
    PRIMARY KEY or UNIQUE constraint'. Anonymous calls now no-op."""

    @pytest.fixture()
    def migrated_db(self, tmp_db):
        db_mod._item_status_has_user_id = None
        db_mod.migrate_item_status_add_user_id(tmp_db, 'default-user')
        yield tmp_db
        db_mod._item_status_has_user_id = None

    def test_anon_caller_noops_instead_of_crashing(self, migrated_db):
        db_mod.batch_upsert(migrated_db, [_make_item()])
        db_mod.set_status(migrated_db, 'test-1', 'starred', force=True, user_id=None)
        rows = migrated_db.execute("SELECT * FROM item_status WHERE item_id='test-1'").fetchall()
        assert rows == []

    def test_user_scoped_insert(self, migrated_db):
        db_mod.batch_upsert(migrated_db, [_make_item()])
        db_mod.set_status(migrated_db, 'test-1', 'starred', force=True, user_id='u1')
        row = migrated_db.execute(
            "SELECT user_id, starred_at FROM item_status WHERE item_id='test-1'"
        ).fetchone()
        assert row is not None
        assert row['user_id'] == 'u1'
        assert row['starred_at'] is not None

    def test_user_scoped_upsert_updates_existing(self, migrated_db):
        db_mod.batch_upsert(migrated_db, [_make_item()])
        db_mod.set_status(migrated_db, 'test-1', 'clicked', user_id='u1')
        db_mod.set_status(migrated_db, 'test-1', 'clicked', user_id='u1')
        rows = migrated_db.execute(
            "SELECT * FROM item_status WHERE item_id='test-1' AND user_id='u1'"
        ).fetchall()
        assert len(rows) == 1

    def test_empty_string_user_id_also_noops(self, migrated_db):
        db_mod.batch_upsert(migrated_db, [_make_item()])
        db_mod.set_status(migrated_db, 'test-1', 'starred', force=True, user_id='')
        rows = migrated_db.execute("SELECT * FROM item_status WHERE item_id='test-1'").fetchall()
        assert rows == []

    def test_anonymous_feed_does_not_duplicate_user_status_rows(self, migrated_db):
        db_mod.batch_upsert(migrated_db, [_make_item()])
        db_mod.set_status(migrated_db, 'test-1', 'clicked', user_id='u1')
        db_mod.set_status(migrated_db, 'test-1', 'starred', user_id='u2')

        items = db_mod.query_feed(migrated_db, user_id=None)
        stats = db_mod.get_stats(migrated_db, public_only=True)

        assert [item['id'] for item in items] == ['test-1']
        assert stats['twitter']['total'] == 1


class TestStatusEndpointAnonymous:
    """Route-level regression: POST /api/status from anonymous caller used to
    500 with the ON CONFLICT message. Now returns 200 {ok:true}, no row written."""

    @pytest.fixture()
    def client(self, monkeypatch, tmp_path):
        db_path = str(tmp_path / 'route.db')
        monkeypatch.setattr(db_mod, 'DB_PATH', db_path)
        monkeypatch.setenv('JWT_SECRET', 'test-secret-key')
        monkeypatch.setenv('RATELIMIT_ENABLED', 'false')
        # Initialize DB + migrate item_status to composite PK (reproduce prod shape)
        db_mod._item_status_has_user_id = None
        conn = db_mod.get_conn()
        db_mod.batch_upsert(conn, [_make_item()])
        db_mod.migrate_item_status_add_user_id(conn, 'default-user')
        conn.close()
        db_mod._item_status_has_user_id = None

        sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
        from fastapi.testclient import TestClient
        from app import app
        if hasattr(app.state, 'limiter'):
            app.state.limiter.enabled = False
        yield TestClient(app)
        db_mod._item_status_has_user_id = None

    def test_anonymous_post_status_no_longer_500(self, client):
        resp = client.post('/api/status', json={'item_id': 'test-1', 'action': 'starred'})
        assert resp.status_code == 200
        assert resp.json().get('ok') is True
        # No row should be written for anonymous caller
        conn = db_mod.get_conn()
        rows = conn.execute("SELECT * FROM item_status WHERE item_id='test-1'").fetchall()
        conn.close()
        assert rows == []


# ── stats ──

class TestStats:
    def test_stats_per_platform(self, tmp_db):
        db_mod.batch_upsert(tmp_db, [
            _make_item(id='tw-1', platform='twitter'),
            _make_item(id='tw-2', platform='twitter'),
            _make_item(id='xhs-1', platform='xiaohongshu'),
        ])
        db_mod.set_status(tmp_db, 'tw-1', 'clicked')
        stats = db_mod.get_stats(tmp_db)
        assert stats['twitter']['total'] == 2
        assert stats['twitter']['unread'] == 1
        assert stats['xiaohongshu']['total'] == 1


# ── feedback ──

class TestFeedback:
    def test_add_and_get_feedback(self, tmp_db):
        db_mod.batch_upsert(tmp_db, [_make_item()])
        db_mod.add_feedback(tmp_db, 'test-1', 'positive', 'AI 开发')
        db_mod.add_feedback(tmp_db, 'test-1', 'low_quality')
        scores = db_mod.get_feedback_scores(tmp_db)
        assert 'test-1' in scores['item_feedback']
        assert 'positive' in scores['item_feedback']['test-1']

    def test_author_scores(self, tmp_db):
        db_mod.batch_upsert(tmp_db, [_make_item(author_name='bob')])
        db_mod.add_feedback(tmp_db, 'test-1', 'positive')
        db_mod.add_feedback(tmp_db, 'test-1', 'positive')
        db_mod.add_feedback(tmp_db, 'test-1', 'low_quality')
        scores = db_mod.get_feedback_scores(tmp_db)
        assert scores['author_scores']['bob'] == 1  # 2 positive - 1 low_quality


# ── AI summary ──

class TestAISummary:
    def test_update_ai_summary(self, tmp_db):
        db_mod.batch_upsert(tmp_db, [_make_item()])
        db_mod.update_ai_summary(tmp_db, 'test-1', 'Great summary', ['point1', 'point2'])
        items = db_mod.query_feed(tmp_db)
        assert items[0]['ai_summary'] == 'Great summary'
        kp = json.loads(items[0]['ai_key_points'])
        assert kp == ['point1', 'point2']


@pytest.mark.skip(
    reason="v3.1 legacy alias 行为(tools→ai_tools / insights→tech)已被 v4.0 废弃。"
           "v4.0 query_feed_sections/by_category 加了 ai_categories IS NOT NULL 过滤,"
           "老 item 不展示;ai_tools L1 改名 efficiency_tools。"
           "新分类规则见 tests/test_classification_config.py 的 v4 套件。"
)
class TestCategoryAliases:
    def test_query_feed_sections_merges_legacy_categories(self, tmp_db):
        db_mod.batch_upsert(tmp_db, [
            _make_item(id='legacy-tools', fetched_at='2026-03-18T00:00:00'),
            _make_item(id='new-tools', fetched_at='2026-03-18T00:01:00'),
            _make_item(id='legacy-insights', fetched_at='2026-03-18T00:02:00'),
            _make_item(id='new-tech', fetched_at='2026-03-18T00:03:00'),
        ])
        tmp_db.execute("UPDATE items SET ai_category=? WHERE id=?", ('tools', 'legacy-tools'))
        tmp_db.execute("UPDATE items SET ai_category=? WHERE id=?", ('ai_tools', 'new-tools'))
        tmp_db.execute("UPDATE items SET ai_category=? WHERE id=?", ('insights', 'legacy-insights'))
        tmp_db.execute("UPDATE items SET ai_category=? WHERE id=?", ('tech', 'new-tech'))
        tmp_db.commit()

        sections, counts = db_mod.query_feed_sections(tmp_db, per_category=None)

        assert counts['ai_tools'] == 2
        assert counts['tech'] == 2
        assert 'tools' not in counts
        assert 'insights' not in counts
        assert {item['ai_category'] for item in sections['ai_tools']} == {'ai_tools'}
        assert {item['ai_category'] for item in sections['tech']} == {'tech'}

    def test_query_feed_by_category_accepts_new_ids_for_legacy_rows(self, tmp_db):
        db_mod.batch_upsert(tmp_db, [
            _make_item(id='legacy-tools', fetched_at='2026-03-18T00:00:00'),
            _make_item(id='legacy-insights', fetched_at='2026-03-18T00:01:00'),
        ])
        tmp_db.execute("UPDATE items SET ai_category=? WHERE id=?", ('tools', 'legacy-tools'))
        tmp_db.execute("UPDATE items SET ai_category=? WHERE id=?", ('insights', 'legacy-insights'))
        tmp_db.commit()

        tool_items = db_mod.query_feed_by_category(tmp_db, 'ai_tools')
        tech_items = db_mod.query_feed_by_category(tmp_db, 'tech')

        assert [item['id'] for item in tool_items] == ['legacy-tools']
        assert [item['ai_category'] for item in tool_items] == ['ai_tools']
        assert [item['id'] for item in tech_items] == ['legacy-insights']
        assert [item['ai_category'] for item in tech_items] == ['tech']


# ── fetch runs ──

class TestFetchRuns:
    def test_start_and_finish(self, tmp_db):
        run_id = db_mod.start_fetch_run(tmp_db)
        assert run_id > 0
        db_mod.finish_fetch_run(tmp_db, run_id, {'twitter': 10})
        last = db_mod.get_last_fetch(tmp_db)
        assert last['status'] == 'done'
        assert last['finished_at'] is not None

    def test_finish_with_error(self, tmp_db):
        run_id = db_mod.start_fetch_run(tmp_db)
        db_mod.finish_fetch_run(tmp_db, run_id, {}, 'timeout')
        last = db_mod.get_last_fetch(tmp_db)
        assert last['status'] == 'error'
        assert last['error_msg'] == 'timeout'


# ── v18.0 Spec-2.5 (rev1, 2026-05-15): query_feed_sections AI 过滤口径 ──

class TestQueryFeedSectionsAIFilterAlignment:
    """v18.0 Spec-2.5 rev1：query_feed_sections 的 WHERE 与 query_feed_platforms
    保持同一份双字段 OR AI 过滤口径，保证「按频道」/「按分类」两个视角看到同一批
    数据。原 v4.0 单字段 ai_categories IS NOT NULL 严格过滤会丢长尾 multi-tag
    数据 + 与 platforms 入口口径不一致。
    """

    def _seed(self, tmp_db):
        db_mod.batch_upsert(tmp_db, [
            _make_item(id='multi-1', platform='twitter', fetched_at='2026-05-15T10:00:00'),
            _make_item(id='multi-2', platform='reddit', fetched_at='2026-05-15T09:00:00'),
            # 用 twitter 而非 github，避免触发 _add_display_visibility 的 github_min_stars=50 过滤
            _make_item(id='single-1', platform='twitter', fetched_at='2026-05-15T08:00:00'),
            _make_item(id='other-1', platform='twitter', fetched_at='2026-05-15T07:00:00'),
            _make_item(id='null-1', platform='hackernews', fetched_at='2026-05-15T06:00:00'),
            _make_item(id='empty-arr-1', platform='twitter', fetched_at='2026-05-15T05:00:00'),
        ])
        # multi-tag: ai_categories 含主分类 + 副分类
        tmp_db.execute(
            "UPDATE items SET ai_categories='[\"products\",\"ai_news\"]', "
            "ai_category='products', visible=1 WHERE id='multi-1'"
        )
        tmp_db.execute(
            "UPDATE items SET ai_categories='[\"efficiency_tools\"]', "
            "ai_category='efficiency_tools', visible=1 WHERE id='multi-2'"
        )
        # single-only: 只有 ai_category 单字段（v4 之前长尾数据），ai_categories 为空数组
        # rev1 OR 口径下应被纳入
        tmp_db.execute(
            "UPDATE items SET ai_categories=NULL, ai_category='coding', visible=1 "
            "WHERE id='single-1'"
        )
        # other：ai_category='other' + 无 multi-tag → 应被过滤掉
        tmp_db.execute(
            "UPDATE items SET ai_categories=NULL, ai_category='other', visible=1 "
            "WHERE id='other-1'"
        )
        # null：两个字段都空 → 应被过滤掉
        tmp_db.execute(
            "UPDATE items SET ai_categories=NULL, ai_category=NULL, visible=1 "
            "WHERE id='null-1'"
        )
        # 空 JSON 数组 + ai_category=null → 双字段都无效，应被过滤
        tmp_db.execute(
            "UPDATE items SET ai_categories='[]', ai_category=NULL, visible=1 "
            "WHERE id='empty-arr-1'"
        )
        tmp_db.commit()

    def test_sections_total_aligns_with_platforms_total(self, tmp_db):
        """2.5 验收 #1：sections 总数 = platforms 总数（同口径，差距 0）。"""
        self._seed(tmp_db)
        sections, cat_counts = db_mod.query_feed_sections(tmp_db, per_category=None)
        _, platform_counts, _ = db_mod.query_feed_platforms(tmp_db, per_platform=None)
        sections_total = sum(cat_counts.values())
        platforms_total = sum(platform_counts.values())
        # 严格相等：两个视角必须看到同一批 item
        assert sections_total == platforms_total, (
            f"sections={sections_total} platforms={platforms_total} "
            f"cat_counts={cat_counts} platform_counts={platform_counts}"
        )
        assert sections_total == 3  # multi-1 + multi-2 + single-1，过滤掉 other/null/empty-arr

    def test_sections_groups_by_ai_categories_primary(self, tmp_db):
        """2.5 验收 #2：multi-tag item 按 ai_categories[0] 主分类分组。"""
        self._seed(tmp_db)
        sections, cat_counts = db_mod.query_feed_sections(tmp_db, per_category=None)
        # multi-1 主分类 = products；multi-2 主分类 = efficiency_tools
        assert 'products' in sections
        assert {item['id'] for item in sections['products']} == {'multi-1'}
        assert 'efficiency_tools' in sections
        assert {item['id'] for item in sections['efficiency_tools']} == {'multi-2'}

    def test_sections_falls_back_to_single_ai_category(self, tmp_db):
        """2.5 验收 #3：rev1 单字段 fallback —— ai_categories 为空但 ai_category 单字段
        有效（如 'coding'）的 item 应按单字段分组进入对应 section。"""
        self._seed(tmp_db)
        sections, cat_counts = db_mod.query_feed_sections(tmp_db, per_category=None)
        # single-1 只有 ai_category='coding'，ai_categories 为 NULL
        # rev1 OR 口径下应被纳入并按 'coding' 分组
        assert 'coding' in sections, (
            f"single-only 长尾数据未被纳入 (rev1 回归)。sections keys={list(sections.keys())}"
        )
        assert {item['id'] for item in sections['coding']} == {'single-1'}
        assert cat_counts['coding'] == 1

    def test_sections_excludes_other_and_null_and_empty(self, tmp_db):
        """2.5 异常态：ai_category='other' / 都 NULL / 空 JSON 数组都不出现。"""
        self._seed(tmp_db)
        sections, cat_counts = db_mod.query_feed_sections(tmp_db, per_category=None)
        all_ids = {item['id'] for cat_items in sections.values() for item in cat_items}
        assert 'other-1' not in all_ids, "ai_category='other' 不应出现"
        assert 'null-1' not in all_ids, "两字段都 NULL 不应出现"
        assert 'empty-arr-1' not in all_ids, "空 JSON 数组 + ai_category NULL 不应出现"
        # _uncategorized 不应被创建（OR 过滤已剔除所有空分类 item）
        assert '_uncategorized' not in cat_counts
