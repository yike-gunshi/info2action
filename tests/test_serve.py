"""Tests for serve.py — trend computation and stopwords."""
import os
import re
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
from routes.feed import _TREND_STOP, _compute_trends
import db as db_mod


@pytest.fixture()
def tmp_db(monkeypatch, tmp_path):
    db_path = str(tmp_path / 'test_feed.db')
    monkeypatch.setattr(db_mod, 'DB_PATH', db_path)
    conn = db_mod.get_conn()
    yield conn
    conn.close()


class TestStopwords:
    """Verify stopword list filters common noise."""

    def test_english_common_words_filtered(self):
        noise = ['the', 'this', 'that', 'with', 'from', 'your', 'have', 'has',
                 'for', 'are', 'was', 'will', 'can', 'not', 'but', 'and', 'you']
        for w in noise:
            assert w in _TREND_STOP, f"'{w}' should be in stopwords"

    def test_url_fragments_filtered(self):
        for w in ['https', 'http', 'www', 'com', 'org', 'net', 'html']:
            assert w in _TREND_STOP

    def test_vague_chinese_filtered(self):
        for w in ['教程', '文章', '分享', '推荐', '内容', '视频', '大家', '自己']:
            assert w in _TREND_STOP

    def test_vague_english_filtered(self):
        for w in ['vibe', 'coding', 'tutorial']:
            assert w in _TREND_STOP

    def test_product_names_not_filtered(self):
        """Actual product/tech names should NOT be in stopwords."""
        for w in ['claude', 'cursor', 'openai', 'gpt', 'gemini', 'mcp']:
            assert w not in _TREND_STOP, f"'{w}' should NOT be in stopwords"


class TestTrendComputation:
    def test_empty_db(self, tmp_db):
        result = _compute_trends(tmp_db)
        assert result['keywords'] == []
        assert result['item_count'] == 0

    def test_keywords_extracted(self, tmp_db):
        from datetime import datetime
        now = datetime.now().isoformat()
        for i in range(3):
            db_mod.upsert_item(tmp_db, dict(
                id=f't-{i}', platform='twitter', source='following',
                title=f'Claude Code 发布新版本 #{i}', content='',
                author_name='test', author_id='', author_avatar='',
                url='', cover_url=None, media_json=None,
                metrics_json='{}', tags_json=None, lang='en',
                detail_json=None, comments_json=None,
                ai_summary='Claude Code 是一个强大的工具',
                relevance_score=1.0, fetched_at=now, published_at=now,
            ))
        tmp_db.commit()
        result = _compute_trends(tmp_db)
        words = [k['word'] for k in result['keywords']]
        assert 'claude' in words or 'Claude' in [k['word'] for k in result['keywords']]
        assert result['item_count'] == 3

    def test_stopwords_not_in_trends(self, tmp_db):
        from datetime import datetime
        now = datetime.now().isoformat()
        for i in range(5):
            db_mod.upsert_item(tmp_db, dict(
                id=f's-{i}', platform='twitter', source='following',
                title=f'这个教程内容分享视频 https://t.co/abc{i}',
                content='', author_name='test', author_id='', author_avatar='',
                url='', cover_url=None, media_json=None,
                metrics_json='{}', tags_json=None, lang='en',
                detail_json=None, comments_json=None,
                ai_summary=None, relevance_score=1.0,
                fetched_at=now, published_at=now,
            ))
        tmp_db.commit()
        result = _compute_trends(tmp_db)
        words = [k['word'] for k in result['keywords']]
        for noise in ['教程', '内容', '分享', '视频', 'https']:
            assert noise not in words, f"'{noise}' should be filtered from trends"

    def test_no_authors_in_result(self, tmp_db):
        """Authors should always be empty (removed feature)."""
        result = _compute_trends(tmp_db)
        assert result['authors'] == []


class TestIngest:
    """Basic ingest smoke tests."""

    def test_twitter_item_structure(self, tmp_db):
        """Verify a Twitter item can be upserted with all required fields."""
        item = dict(
            id='tw-test-1', platform='twitter', source='for_you',
            title='Test tweet', content='Full tweet text here',
            author_name='bob', author_id='b1', author_avatar='',
            url='https://x.com/bob/status/1', cover_url=None,
            media_json=None, metrics_json='{"likes":5,"views":100}',
            tags_json=None, lang='en',
            detail_json='{"urls":["https://example.com"]}',
            comments_json=None, ai_summary=None,
            relevance_score=10.0, fetched_at='2026-03-18T12:00:00',
            published_at='2026-03-18T11:00:00',
        )
        db_mod.upsert_item(tmp_db, item)
        tmp_db.commit()
        rows = db_mod.query_feed(tmp_db)
        assert len(rows) == 1
        assert rows[0]['platform'] == 'twitter'
