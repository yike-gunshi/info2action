"""Tests for feedback_store.py — independent feedback database."""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
import feedback_store as fs


@pytest.fixture()
def fb_conn(monkeypatch, tmp_path):
    db_path = str(tmp_path / 'test_feedback.db')
    monkeypatch.setattr(fs, 'FB_DB_PATH', db_path)
    conn = fs.get_conn()
    yield conn
    conn.close()


class TestItemFeedback:
    def test_record_and_retrieve(self, fb_conn):
        fs.record_item_feedback(
            fb_conn, 'item-1', 'positive',
            platform='twitter', title='Test', author='alice',
            url='https://x.com/1', reason='Very insightful', topic='AI 开发',
        )
        rows = fs.get_all_item_feedback(fb_conn)
        assert len(rows) == 1
        assert rows[0]['item_id'] == 'item-1'
        assert rows[0]['action'] == 'positive'
        assert rows[0]['reason'] == 'Very insightful'
        assert rows[0]['topic_at_time'] == 'AI 开发'

    def test_multiple_feedback(self, fb_conn):
        fs.record_item_feedback(fb_conn, 'item-1', 'positive')
        fs.record_item_feedback(fb_conn, 'item-2', 'irrelevant')
        fs.record_item_feedback(fb_conn, 'item-3', 'low_quality')
        rows = fs.get_all_item_feedback(fb_conn)
        assert len(rows) == 3

    def test_set_should_feature_is_idempotent_and_supports_text(self, fb_conn):
        fs.set_item_feedback(
            fb_conn,
            'item-1',
            'should_feature',
            active=True,
            title='Test',
            reason='这条有关键上下文',
        )
        fs.set_item_feedback(
            fb_conn,
            'item-1',
            'should_feature',
            active=True,
            title='Test',
            reason='更新后的判断',
        )

        rows = fs.get_all_item_feedback(fb_conn)
        assert len(rows) == 1
        assert rows[0]['action'] == 'should_feature'
        assert rows[0]['reason'] == '更新后的判断'

        fs.set_item_feedback(
            fb_conn,
            'item-1',
            'should_feature',
            active=False,
        )
        assert fs.get_all_item_feedback(fb_conn) == []

    def test_set_should_drop_is_idempotent_and_supports_text(self, fb_conn):
        fs.set_item_feedback(
            fb_conn,
            'item-1',
            'should_drop',
            active=True,
            reason='排除营销内容',
        )
        fs.set_item_feedback(
            fb_conn,
            'item-1',
            'should_drop',
            active=True,
            reason='排除互动诱饵',
        )

        rows = fs.get_all_item_feedback(fb_conn)
        assert len(rows) == 1
        assert rows[0]['action'] == 'should_drop'
        assert rows[0]['reason'] == '排除互动诱饵'


class TestSystemFeedback:
    def test_record_system_feedback(self, fb_conn):
        fs.record_system_feedback(fb_conn, 'trend', '趋势词里出现了 the')
        fs.record_system_feedback(fb_conn, 'classification', '文章归类到了未分类', {'item_id': 'x'})
        summary = fs.get_feedback_summary(fb_conn)
        assert summary['system'] == 2


class TestPreferenceSignals:
    def test_record_and_retrieve(self, fb_conn):
        fs.record_preference(fb_conn, 'author_like', 'vista8', note='Consistently good AI content')
        fs.record_preference(fb_conn, 'keyword_block', 'vibe', note='Too vague')
        prefs = fs.get_all_preferences(fb_conn)
        assert len(prefs) == 2
        types = {p['signal_type'] for p in prefs}
        targets = {p['target'] for p in prefs}
        assert 'keyword_block' in types
        assert 'vista8' in targets


class TestFeedbackSummary:
    def test_summary_counts(self, fb_conn):
        fs.record_item_feedback(fb_conn, 'a', 'positive')
        fs.record_item_feedback(fb_conn, 'b', 'positive')
        fs.record_item_feedback(fb_conn, 'c', 'irrelevant')
        fs.record_item_feedback(fb_conn, 'd', 'low_quality')
        fs.record_system_feedback(fb_conn, 'ui', 'pill too tall')
        fs.record_preference(fb_conn, 'topic_interest', 'AI 产品')
        s = fs.get_feedback_summary(fb_conn)
        assert s['positive'] == 2
        assert s['irrelevant'] == 1
        assert s['low_quality'] == 1
        assert s['system'] == 1
        assert s['preferences'] == 1
