#!/usr/bin/env python3
"""Lightweight classifier for structured user feedback."""

from __future__ import annotations

from typing import Iterable


PRIORITY_ORDER = {"high": 0, "medium": 1, "low": 2}


CATEGORY_RULES = (
    {
        "category": "bug_report",
        "feedback_type": "system_feedback",
        "priority": "high",
        "keywords": ("报错", "错误", "失败", "无法", "崩溃", "卡死", "bug"),
    },
    {
        "category": "data_analysis",
        "feedback_type": "system_feedback",
        "priority": "medium",
        "keywords": ("数据分析", "分类处理", "反馈数据", "统计", "分析"),
    },
    {
        "category": "feature_request",
        "feedback_type": "system_feedback",
        "priority": "low",
        "keywords": ("希望", "建议", "增加", "新增", "支持", "导出", "优化"),
    },
)


def _compose_text(entry: dict) -> str:
    return " ".join(str(entry.get(key, "")).strip() for key in ("label", "text")).strip()


def classify_feedback_entry(entry: dict) -> dict:
    """Classify a single feedback entry into a category and priority."""
    combined_text = _compose_text(entry)

    for rule in CATEGORY_RULES:
        if any(keyword in combined_text for keyword in rule["keywords"]):
            return {
                **entry,
                "feedback_type": rule["feedback_type"],
                "category": rule["category"],
                "priority": rule["priority"],
            }

    return {
        **entry,
        "feedback_type": "system_feedback",
        "category": "other",
        "priority": "low",
    }


def classify_feedback_entries(entries: Iterable[dict]) -> list[dict]:
    """Classify feedback entries and sort them by priority."""
    results = [classify_feedback_entry(entry) for entry in entries]
    return sorted(results, key=lambda item: PRIORITY_ORDER.get(item["priority"], 99))
