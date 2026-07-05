"""Tests for automatic user feedback classification."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from feedback_classifier import classify_feedback_entries, classify_feedback_entry


def test_classify_data_analysis_feedback():
    result = classify_feedback_entry(
        {"label": "数据分析", "text": "反馈数据需要分类处理"}
    )

    assert result["feedback_type"] == "system_feedback"
    assert result["category"] == "data_analysis"
    assert result["priority"] == "medium"


def test_sort_feedback_by_priority():
    results = classify_feedback_entries(
        [
            {"label": "体验问题", "text": "页面一直报错，无法提交反馈"},
            {"label": "数据分析", "text": "反馈数据需要分类处理"},
            {"label": "建议", "text": "希望后续增加导出功能"},
        ]
    )

    assert [item["priority"] for item in results] == ["high", "medium", "low"]
    assert results[0]["category"] == "bug_report"
