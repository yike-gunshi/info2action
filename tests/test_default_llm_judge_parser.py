"""Unit tests for the merge-decision parser fix (V2.3 §13.1, BF v15.1).

Covers:
- Yes / No string variants
- True / False boolean variants
- Extra fields don't break parsing
- Empty / null / malformed → None (and _default_llm_judge returns False)
- Markdown code-fence wrapped JSON
"""
import os
import sys
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
from clustering import pipeline as pl  # noqa: E402


class TestParseMergeDecision:
    def test_yes_string(self):
        assert pl._parse_merge_decision('{"same_event": "yes"}') is True

    def test_no_string(self):
        assert pl._parse_merge_decision('{"same_event": "no"}') is False

    def test_true_bool(self):
        assert pl._parse_merge_decision('{"same_event": true}') is True

    def test_false_bool(self):
        assert pl._parse_merge_decision('{"same_event": false}') is False

    def test_extra_fields_dont_break(self):
        raw = (
            '{"same_event": "yes", "confidence": "high", '
            '"rationale": "same product launch", "title": "garbage"}'
        )
        assert pl._parse_merge_decision(raw) is True

    def test_uppercase_yes(self):
        assert pl._parse_merge_decision('{"same_event": "YES"}') is True

    def test_uppercase_no_with_whitespace(self):
        assert pl._parse_merge_decision('{"same_event": "  No  "}') is False

    def test_empty_string_returns_none(self):
        assert pl._parse_merge_decision('') is None

    def test_none_returns_none(self):
        assert pl._parse_merge_decision(None) is None

    def test_whitespace_only_returns_none(self):
        assert pl._parse_merge_decision('   \n\t  ') is None

    def test_invalid_json_returns_none(self):
        assert pl._parse_merge_decision('not json at all') is None

    def test_missing_same_event_returns_none(self):
        assert pl._parse_merge_decision('{"confidence": "high"}') is None

    def test_unsupported_value_returns_none(self):
        assert pl._parse_merge_decision('{"same_event": "maybe"}') is None

    def test_non_object_returns_none(self):
        assert pl._parse_merge_decision('["yes"]') is None
        assert pl._parse_merge_decision('"yes"') is None

    def test_markdown_code_fence_json(self):
        raw = '```json\n{"same_event": "yes"}\n```'
        assert pl._parse_merge_decision(raw) is True

    def test_markdown_code_fence_no_lang(self):
        raw = '```\n{"same_event": "no"}\n```'
        assert pl._parse_merge_decision(raw) is False


class TestDefaultLlmJudgeBehavior:
    """Integration: _default_llm_judge calls _call_llm_chat → _parse_merge_decision."""

    def _patch_llm(self, raw):
        return patch.object(
            pl.summary_writer, '_call_llm_chat', return_value=raw,
        )

    def test_yes_returns_true(self):
        with self._patch_llm('{"same_event": "yes", "confidence": "high"}'):
            r = pl._default_llm_judge(
                'doc a', 'doc b', scenario='new_doc_vs_cluster_member',
                api_key='k', api_base=None, model='m',
            )
        assert r is True

    def test_no_returns_false(self):
        with self._patch_llm('{"same_event": "no"}'):
            r = pl._default_llm_judge(
                'doc a', 'doc b', scenario='new_doc_vs_cluster_member',
                api_key='k', api_base=None, model='m',
            )
        assert r is False

    def test_bool_true_returns_true(self):
        """V1 bug fix: previously _parse_llm_json swallowed valid merge JSON
        and the function unconditionally returned False. Now bool true should
        return True."""
        with self._patch_llm('{"same_event": true, "rationale": "x"}'):
            r = pl._default_llm_judge(
                'a', 'b', scenario='cluster_a_vs_cluster_b',
                api_key='k', api_base=None, model='m',
            )
        assert r is True

    def test_parse_fail_returns_false_and_logs(self):
        with self._patch_llm('not valid json'):
            with patch.object(pl, '_log_event') as mock_log:
                r = pl._default_llm_judge(
                    'a', 'b', scenario='new_doc_vs_cluster_member',
                    api_key='k', api_base=None, model='m',
                )
        assert r is False
        events = [c.args[0] for c in mock_log.call_args_list]
        assert 'llm_judge_parse_fail' in events

    def test_empty_response_returns_false_and_logs(self):
        with self._patch_llm(''):
            with patch.object(pl, '_log_event') as mock_log:
                r = pl._default_llm_judge(
                    'a', 'b', scenario='new_doc_vs_cluster_member',
                    api_key='k', api_base=None, model='m',
                )
        assert r is False
        events = [c.args[0] for c in mock_log.call_args_list]
        assert 'llm_judge_parse_fail' in events

    def test_llm_call_raises_propagates(self):
        with patch.object(pl.summary_writer, '_call_llm_chat',
                          side_effect=RuntimeError('timeout')):
            with pytest.raises(RuntimeError):
                pl._default_llm_judge(
                    'a', 'b', scenario='new_doc_vs_cluster_member',
                    api_key='k', api_base=None, model='m',
                )
