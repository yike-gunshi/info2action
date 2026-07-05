"""Latest-events visibility policy shared by clustering stages.

BF-0501-1: being an event is not the same thing as having multiple source
identities. Multi-source clusters are still a strong signal, but high-value
singletons in product/tool/model/tech/etc. categories can also be displayed.
"""
from __future__ import annotations

from collections import Counter
from typing import Iterable

from category_taxonomy import canonicalize_category


HIGH_VALUE_SINGLE_SOURCE_CATEGORIES = {
    "products",
    "efficiency_tools",
    "coding",
    "skill",
    "models",
    "eval",
    "tech",
    "tutorials",
    "industry",
    "creator",
    "investment",
    "startup",
    "events",
}

HIGH_VALUE_SINGLE_SOURCE_CATEGORY_ALIASES = {
    "products",
    "efficiency_tools",
    "ai_tools",
    "tools",
    "coding",
    "skill",
    "models",
    "eval",
    "tech",
    "insights",
    "tutorials",
    "industry",
    "creator",
    "investment",
    "startup",
    "events",
}

DOMINANT_CATEGORY_PRIORITY = (
    "products",
    "models",
    "eval",
    "efficiency_tools",
    "coding",
    "skill",
    "tech",
    "industry",
    "investment",
    "startup",
    "events",
    "tutorials",
    "creator",
    "other",
)

INVALID_FEED_WARNING_KEYWORDS = (
    "主体不一致",
    "事件不一致",
    "无法构成同一事件",
    "不属于同一事件",
    "不构成事件聚合",
    "不构成具体事件",
    "不构成技术事件",
    "事件性不足",
    "无共同主题",
    "无可识别的中心实体",
    "不建议进入",
    "不建议纳入",
    "不建议展示",
    "不符合 info2action",
    "低信息",
    "信息量极低",
    "缺乏实质性信息",
    "信息不足",
    "与 info2action 定位无关联",
    "与info2action定位无关联",
    "不属于ai/科技",
    "不属于 ai/科技",
    "不属于AI/科技",
    "不属于 AI/科技",
    "非科技领域",
    "定位存在偏差",
    "纯娱乐 meme",
    "交易信号",
    "喊单",
    "买入信号",
    "看涨信号",
    "meme 币交易",
    "无关联",
)

LOW_VALUE_TRADING_SIGNAL_KEYWORDS = (
    "交易信号",
    "喊单",
    "买入信号",
    "看涨信号",
    "目标涨幅",
    "meme 币交易",
    "meme币交易",
)

FINANCING_KEYWORDS = (
    "融资",
    "投资方",
    "种子轮",
    "pre-seed",
    "seed round",
    "series a",
    "funding",
)


def normalize_category(category: str | None) -> str | None:
    value = (category or "").strip().lower()
    if not value:
        return None
    return canonicalize_category(value)


def is_high_value_single_source_category(category: str | None) -> bool:
    normalized = normalize_category(category)
    return normalized in HIGH_VALUE_SINGLE_SOURCE_CATEGORIES


def invalid_feed_warnings(warnings) -> list[str]:
    """Return invalid-display keywords matched by summary warnings.

    The old source-count gate used "非跨源事件" as an invalid warning. BF-0501-1
    removes that assumption, so non-cross-source wording alone is not fatal.
    """
    if not warnings or not isinstance(warnings, list):
        return []
    matched: list[str] = []
    for warning in warnings:
        try:
            text = str(warning) if warning is not None else ""
        except Exception:
            continue
        if not text:
            continue
        for keyword in INVALID_FEED_WARNING_KEYWORDS:
            if keyword in text and keyword not in matched:
                matched.append(keyword)
    return matched


def looks_like_low_value_trading_signal(*texts: str | None) -> bool:
    combined = " ".join(text or "" for text in texts).lower()
    if not combined:
        return False
    if any(keyword.lower() in combined for keyword in FINANCING_KEYWORDS):
        return False
    return any(keyword.lower() in combined for keyword in LOW_VALUE_TRADING_SIGNAL_KEYWORDS)


def dominant_category(categories: Iterable[str | None]) -> str | None:
    counts: Counter[str] = Counter()
    for category in categories:
        normalized = normalize_category(category)
        if normalized:
            counts[normalized] += 1
    if not counts:
        return None
    max_count = max(counts.values())
    candidates = {category for category, count in counts.items() if count == max_count}
    if len(candidates) == 1:
        return next(iter(candidates))
    for category in DOMINANT_CATEGORY_PRIORITY:
        if category in candidates:
            return category
    return sorted(candidates)[0]


def cluster_dominant_category(conn, cluster_id: int) -> str | None:
    rows = conn.execute(
        """SELECT i.ai_category
             FROM items i
             JOIN cluster_items ci ON ci.item_id = i.id
            WHERE ci.cluster_id = ?""",
        (cluster_id,),
    ).fetchall()
    return dominant_category(row["ai_category"] for row in rows)


def should_summarize_cluster(*, unique_source_count: int | None,
                             category: str | None) -> bool:
    if (unique_source_count or 0) >= 2:
        return True
    return is_high_value_single_source_category(category)


def is_displayable_event(*, title: str | None, summary: str | None,
                         unique_source_count: int | None,
                         category: str | None,
                         warnings=None,
                         event_certainty: str | None = None) -> bool:
    if not (title and str(title).strip() and summary and str(summary).strip()):
        return False
    normalized_category = normalize_category(category)
    if normalized_category == "other":
        return False
    if (event_certainty or "").strip().lower() == "low":
        return False
    if invalid_feed_warnings(warnings):
        return False
    warning_text = " ".join(str(w) for w in warnings or [])
    if looks_like_low_value_trading_signal(title, warning_text):
        return False
    if (unique_source_count or 0) >= 2:
        return True
    return is_high_value_single_source_category(normalized_category)
