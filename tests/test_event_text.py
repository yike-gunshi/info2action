"""Tests for src/clustering/event_text.py (v15.1 Stage 0 upgrade).

Covers feature-spec R1.1 / R1.2 / R1.3 + V2.3 §3 / §0.7 hard constraints.
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
from clustering import event_text as et  # noqa: E402


def _full_item(**overrides):
    base = {
        'id': 'item-123',
        'title': 'GPT-5.5 正式发布',
        'ai_summary': 'OpenAI 今日发布 GPT-5.5 模型，支持 1M context window。',
        'ai_key_points': json.dumps([
            '1M context window 上限',
            '速度提升 2 倍',
            '价格下调 30%',
        ], ensure_ascii=False),
        'ai_keywords': 'GPT-5.5, OpenAI, 大模型',
        'ai_category': 'tech',
        'content_type': 'news',
        'content': '正文内容：OpenAI 在今日发布会上宣布 GPT-5.5 ...',
        'asr_text_cn': None,
        'asr_text': None,
    }
    base.update(overrides)
    return base


class TestNormalScenario_R1_1:
    def test_full_enrich_no_fallback(self):
        text, meta = et.build_event_embedding_text(_full_item())
        assert meta['has_ai_summary'] is True
        assert meta['has_ai_key_points'] is True
        assert meta['has_ai_keywords'] is True
        assert meta['used_fallback_content'] is False
        assert meta['embedding_text_chars'] == len(text)
        assert meta['embedding_text_chars'] <= et.MAX_CHARS

    def test_priority_order_in_text(self):
        text, _ = et.build_event_embedding_text(_full_item())
        # Title should come before summary, summary before key points, etc.
        assert '标题:' in text
        assert 'GPT-5.5 正式发布' in text
        assert 'AI摘要:' in text
        assert '结构化要点:' in text
        assert '1M context window 上限' in text  # key point bullet
        assert '关键词:' in text
        assert text.index('标题:') < text.index('AI摘要:')
        assert text.index('AI摘要:') < text.index('结构化要点:')
        assert text.index('结构化要点:') < text.index('关键词:')


class TestFallbackScenario_R1_2:
    def test_missing_ai_summary_triggers_fallback(self):
        item = _full_item(ai_summary=None)
        text, meta = et.build_event_embedding_text(item)
        assert meta['has_ai_summary'] is False
        assert meta['used_fallback_content'] is True
        # Title and content still present
        assert '标题:' in text
        assert '正文/转写:' in text

    def test_missing_ai_key_points_triggers_fallback(self):
        item = _full_item(ai_key_points=None)
        text, meta = et.build_event_embedding_text(item)
        assert meta['has_ai_key_points'] is False
        assert meta['used_fallback_content'] is True

    def test_empty_array_keypoints_triggers_fallback(self):
        item = _full_item(ai_key_points='[]')
        text, meta = et.build_event_embedding_text(item)
        assert meta['has_ai_key_points'] is False
        assert meta['used_fallback_content'] is True

    def test_corrupted_keypoints_no_exception(self):
        item = _full_item(ai_key_points='{not valid json')
        text, meta = et.build_event_embedding_text(item)
        # Should not raise; keypoints treated as empty
        assert meta['has_ai_key_points'] is False
        assert meta['used_fallback_content'] is True
        assert text  # still produces some text

    def test_keypoints_non_list_shape(self):
        item = _full_item(ai_key_points='"a string not a list"')
        _, meta = et.build_event_embedding_text(item)
        assert meta['has_ai_key_points'] is False

    def test_both_summary_and_keypoints_missing(self):
        item = _full_item(ai_summary=None, ai_key_points=None)
        text, meta = et.build_event_embedding_text(item)
        assert meta['used_fallback_content'] is True
        assert '标题:' in text
        assert '正文/转写:' in text


class TestLongContent_R1_3:
    def test_total_under_max_chars(self):
        item = _full_item(content='X' * 50000)
        text, meta = et.build_event_embedding_text(item)
        assert len(text) <= et.MAX_CHARS
        assert meta['embedding_text_chars'] == len(text)

    def test_long_content_keeps_head_and_tail(self):
        head_marker = 'HEAD_SIGNAL_AAA'
        tail_marker = 'TAIL_SIGNAL_ZZZ'
        body = head_marker + ('X' * 40000) + tail_marker
        item = _full_item(content=body)
        text, _ = et.build_event_embedding_text(item)
        assert et._TRIM_MARKER.strip() in text
        assert head_marker in text
        assert tail_marker in text

    def test_structured_fields_preserved_when_content_huge(self):
        item = _full_item(content='Y' * 50000)
        text, _ = et.build_event_embedding_text(item)
        # All structured fields SHALL be preserved (R1.3)
        assert 'GPT-5.5 正式发布' in text
        assert 'AI摘要:' in text
        assert '1M context window 上限' in text  # first keypoint
        assert '关键词:' in text


class TestCommentsExclusion_V2_3_Section_0_7:
    def test_comments_json_not_in_output(self):
        item = _full_item()
        item['comments_json'] = json.dumps([
            {'text': 'COMMENTS_SHOULD_NOT_LEAK_INTO_EMBEDDING'},
        ], ensure_ascii=False)
        text, _ = et.build_event_embedding_text(item)
        assert 'COMMENTS_SHOULD_NOT_LEAK_INTO_EMBEDDING' not in text


class TestTitleCleaning:
    def test_strip_bracket_platform_prefix(self):
        item = _full_item(title='[YouTube] GPT-5.5 正式发布')
        text, _ = et.build_event_embedding_text(item)
        assert 'GPT-5.5 正式发布' in text
        # Bracketed prefix removed
        assert '[YouTube]' not in text

    def test_strip_chinese_bracket_platform_prefix(self):
        item = _full_item(title='【B站】GPT-5.5 正式发布')
        text, _ = et.build_event_embedding_text(item)
        assert 'GPT-5.5 正式发布' in text
        assert '【B站】' not in text

    def test_emoji_in_title_kept(self):
        item = _full_item(title='🎉 GPT-5.5 正式发布！')
        text, _ = et.build_event_embedding_text(item)
        # Emoji is core content (not platform prefix), keep it
        assert 'GPT-5.5 正式发布' in text


class TestEdgeCases:
    def test_all_empty_fields_returns_placeholder(self):
        item = {
            'id': 'empty-1',
            'title': None,
            'ai_summary': None,
            'ai_key_points': None,
            'ai_keywords': None,
            'ai_category': None,
            'content_type': None,
            'content': None,
        }
        text, meta = et.build_event_embedding_text(item)
        assert text == '(empty item empty-1)'
        assert meta['used_fallback_content'] is True
        assert meta['embedding_text_chars'] == len(text)

    def test_only_title_and_content(self):
        item = {
            'id': 'i-1',
            'title': '简单标题',
            'content': '一段正文',
        }
        text, meta = et.build_event_embedding_text(item)
        assert '简单标题' in text
        assert '一段正文' in text
        assert meta['used_fallback_content'] is True
        assert meta['has_ai_summary'] is False
        assert meta['has_ai_key_points'] is False

    def test_asr_fallback_used_when_content_missing(self):
        item = _full_item(content=None, asr_text_cn='ASR 中文转写内容', ai_summary=None, ai_key_points=None)
        text, meta = et.build_event_embedding_text(item)
        assert 'ASR 中文转写内容' in text
        assert meta['used_fallback_content'] is True

    def test_keypoints_as_python_list_passthrough(self):
        # Tolerate already-parsed list (some callers may pre-parse)
        item = _full_item(ai_key_points=['point A', 'point B'])
        text, meta = et.build_event_embedding_text(item)
        assert meta['has_ai_key_points'] is True
        assert 'point A' in text
        assert 'point B' in text
