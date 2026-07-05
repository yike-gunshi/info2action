"""
Ranking engine for info2action feed (v12.0).

Public flow:  quality × (1 + 0.1 × engagement_percentile) × freshness_decay
Personal flow: quality × match_score × (1 + 0.1 × engagement_percentile) × freshness_decay

All scores are computed in Python after DB fetch — no LLM calls.
"""

import json
import math
from datetime import datetime, timezone

from category_taxonomy import canonicalize_category, ensure_all_categories

# ── Role → category weight mappings ──

_ROLE_CATEGORY_WEIGHTS = {
    'developer': {'ai_tools': 2.5, 'tech': 2.0, 'tutorials': 1.8, 'products': 1.2, 'models': 1.3, 'industry': 0.5, 'creator': 0.3, 'investment': 0.2, 'other': 0.4},
    'pm': {'products': 2.5, 'industry': 2.0, 'tech': 1.3, 'ai_tools': 1.2, 'tutorials': 1.0, 'models': 0.6, 'creator': 0.6, 'investment': 0.8, 'other': 0.4},
    'founder': {'products': 2.2, 'industry': 2.5, 'tech': 1.6, 'investment': 1.8, 'ai_tools': 1.2, 'tutorials': 0.8, 'models': 0.8, 'creator': 0.7, 'other': 0.4},
    'researcher': {'models': 2.5, 'tech': 2.2, 'tutorials': 2.0, 'ai_tools': 1.0, 'products': 0.6, 'industry': 0.5, 'creator': 0.3, 'investment': 0.2, 'other': 0.4},
    'investor': {'investment': 2.5, 'industry': 2.5, 'products': 1.6, 'tech': 1.0, 'ai_tools': 0.5, 'models': 0.6, 'tutorials': 0.3, 'creator': 0.3, 'other': 0.4},
    'creator': {'creator': 2.5, 'ai_tools': 2.0, 'products': 1.5, 'tutorials': 1.5, 'tech': 0.8, 'models': 0.5, 'industry': 0.5, 'investment': 0.3, 'other': 0.4},
    'student': {'tutorials': 2.5, 'ai_tools': 2.0, 'models': 1.8, 'tech': 1.8, 'products': 1.0, 'industry': 0.5, 'creator': 0.8, 'investment': 0.2, 'other': 0.4},
    'other': {'products': 1.0, 'ai_tools': 1.0, 'models': 1.0, 'tech': 1.0, 'tutorials': 1.0, 'industry': 1.0, 'creator': 1.0, 'investment': 1.0, 'other': 0.4},
}

# Interest → keyword boosts
_INTEREST_KEYWORDS = {
    'ai-tools': ['claude code', 'cursor', 'copilot', 'ai tool', 'ai 工具', 'developer tool'],
    'ai-coding': ['coding', 'programming', 'vibe coding', 'code generation', 'ai 编程'],
    'ai-agents': ['agent', 'agentic', 'multi-agent', 'autonomous', 'mcp'],
    'llm-models': ['gpt', 'claude', 'llama', 'gemini', 'deepseek', 'model', '大模型'],
    'open-source': ['open source', 'github', '开源', 'oss'],
    'ai-products': ['product', 'launch', '产品', 'saas', 'app'],
    'ai-research': ['paper', 'research', 'arxiv', '论文', '研究'],
    'ai-industry': ['funding', 'acquisition', '融资', '收购', 'valuation'],
    'ai-investment': ['investment', 'stock', 'etf', '投资', 'portfolio'],
    'prompt-eng': ['prompt', 'system prompt', 'prompt engineering'],
    'ai-creative': ['midjourney', 'stable diffusion', 'suno', 'ai 创作', '生图', '生视频'],
    'ai-infra': ['gpu', 'inference', 'training', 'infrastructure', 'cuda', 'vllm'],
}


def profile_to_weights(profile):
    """Convert user profile to ranking weight vector.

    Args:
        profile: dict with 'role', 'interests' (list), 'tools' (list)

    Returns:
        dict with 'category_weights' and 'keyword_boosts'
    """
    if not profile:
        return None

    role = profile.get('role', '')
    interests = profile.get('interests') or []
    tools = profile.get('tools') or []

    # Base category weights from role
    role_weights = _ROLE_CATEGORY_WEIGHTS.get(role)
    if role_weights:
        cat_weights = ensure_all_categories(dict(role_weights))
    else:
        # Default neutral weights
        cat_weights = ensure_all_categories({}, fill=1.0)
        cat_weights['other'] = 0.4

    # Boost categories based on interests (larger increments, higher caps)
    for interest in interests:
        if interest in ('ai-tools', 'ai-coding'):
            cat_weights['ai_tools'] = min(3.5, cat_weights.get('ai_tools', 1.0) + 0.5)
            if interest == 'ai-coding':
                cat_weights['tech'] = min(3.5, cat_weights.get('tech', 1.0) + 0.2)
        elif interest == 'ai-agents':
            cat_weights['ai_tools'] = min(3.5, cat_weights.get('ai_tools', 1.0) + 0.3)
            cat_weights['tech'] = min(3.5, cat_weights.get('tech', 1.0) + 0.4)
        elif interest == 'llm-models':
            cat_weights['models'] = min(3.5, cat_weights.get('models', 1.0) + 0.5)
        elif interest == 'ai-products':
            cat_weights['products'] = min(3.5, cat_weights.get('products', 1.0) + 0.5)
        elif interest == 'ai-research':
            cat_weights['models'] = min(3.5, cat_weights.get('models', 1.0) + 0.3)
            cat_weights['tech'] = min(3.5, cat_weights.get('tech', 1.0) + 0.3)
        elif interest == 'ai-industry':
            cat_weights['industry'] = min(3.5, cat_weights.get('industry', 1.0) + 0.5)
        elif interest == 'ai-investment':
            cat_weights['investment'] = min(3.5, cat_weights.get('investment', 1.0) + 0.5)
        elif interest == 'ai-creative':
            cat_weights['creator'] = min(3.5, cat_weights.get('creator', 1.0) + 0.5)
        elif interest == 'open-source':
            cat_weights['ai_tools'] = min(3.5, cat_weights.get('ai_tools', 1.0) + 0.4)
            cat_weights['tech'] = min(3.5, cat_weights.get('tech', 1.0) + 0.2)
        elif interest == 'prompt-eng':
            cat_weights['ai_tools'] = min(3.5, cat_weights.get('ai_tools', 1.0) + 0.3)
            cat_weights['tech'] = min(3.5, cat_weights.get('tech', 1.0) + 0.2)
        elif interest == 'ai-infra':
            cat_weights['tech'] = min(3.5, cat_weights.get('tech', 1.0) + 0.5)
            cat_weights['models'] = min(3.5, cat_weights.get('models', 1.0) + 0.3)

    # Build keyword boosts from interests + tools
    keyword_boosts = []
    for interest in interests:
        keyword_boosts.extend(_INTEREST_KEYWORDS.get(interest, []))
    # Tool names as keyword boosts
    for tool in tools:
        keyword_boosts.append(tool.replace('-', ' '))

    return {
        'category_weights': cat_weights,
        'keyword_boosts': list(set(keyword_boosts)),
    }


# ── Freshness decay ──

def freshness_decay(hours_age, gravity=1.5):
    """HN-style decay: 1 / (1 + hours)^gravity.

    - 0 hours → 1.0
    - 6 hours → ~0.26
    - 24 hours → ~0.064
    - 48 hours → ~0.022
    """
    return 1.0 / math.pow(1 + max(0, hours_age), gravity)


def item_age_hours(item, now=None):
    """Compute item age in hours from fetched_at or published_at."""
    if now is None:
        now = datetime.now(timezone.utc)

    ts_str = item.get('published_at') or item.get('fetched_at')
    if not ts_str:
        return 168  # default 1 week old

    try:
        # Handle various datetime formats
        if 'T' in ts_str:
            ts = datetime.fromisoformat(ts_str.replace('Z', '+00:00'))
        else:
            ts = datetime.strptime(ts_str, '%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone.utc)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        delta = (now - ts).total_seconds() / 3600.0
        return max(0, delta)
    except (ValueError, TypeError):
        return 168


# ── Engagement normalization ──

def extract_engagement_value(item):
    """Extract a single engagement number from metrics_json for percentile ranking."""
    metrics = item.get('metrics_json')
    if not metrics:
        return 0

    if isinstance(metrics, str):
        try:
            metrics = json.loads(metrics)
        except (json.JSONDecodeError, TypeError):
            return 0

    platform = item.get('platform', '')

    # Platform-specific primary engagement metric
    if platform == 'twitter':
        return metrics.get('likes', 0) + metrics.get('retweets', 0) * 2
    elif platform == 'bilibili':
        return metrics.get('likes', 0) + metrics.get('coins', 0) * 3 + metrics.get('favorites', 0) * 2
    elif platform == 'reddit':
        return metrics.get('score', 0)
    elif platform == 'xiaohongshu':
        return metrics.get('likes', 0) + metrics.get('collects', 0) * 2
    elif platform == 'github':
        return metrics.get('stars', 0) or metrics.get('stargazers_count', 0)
    elif platform == 'hackernews':
        return metrics.get('score', 0) or metrics.get('points', 0)
    else:
        # Generic: sum all numeric values
        total = 0
        for v in metrics.values():
            if isinstance(v, (int, float)) and v > 0:
                total += v
        return total


def compute_engagement_percentiles(items):
    """Compute engagement percentiles within a list of items.

    Groups by platform, ranks within platform, returns {item_id: percentile (0-1)}.
    """
    # Group by platform
    platform_groups = {}
    for item in items:
        p = item.get('platform', 'unknown')
        if p not in platform_groups:
            platform_groups[p] = []
        platform_groups[p].append(item)

    percentiles = {}
    for platform, group in platform_groups.items():
        # Sort by engagement value
        values = [(item, extract_engagement_value(item)) for item in group]
        values.sort(key=lambda x: x[1])
        n = len(values)
        for rank, (item, val) in enumerate(values):
            # Percentile: what fraction of items have lower engagement
            percentiles[item['id']] = rank / max(n - 1, 1)

    return percentiles


# ── Ranking ──

def rank_items(items, personalized=False, user_weights=None):
    """Sort items by computed ranking score.

    Args:
        items: list of item dicts (must have 'id', 'ai_quality_score', 'metrics_json', 'fetched_at')
        personalized: if True, apply match_score from user_weights
        user_weights: dict with 'category_weights' and 'keyword_boosts' (for personalized mode)

    Returns:
        items sorted by ranking score (descending), with 'ranking_score' added to each.
    """
    now = datetime.now(timezone.utc)

    # Step 1: compute engagement percentiles
    percentiles = compute_engagement_percentiles(items)

    # Step 2: compute ranking score for each item
    for item in items:
        quality = item.get('ai_quality_score') or 0.5  # default if not scored yet
        engagement_pct = percentiles.get(item['id'], 0.5)
        hours = item_age_hours(item, now)
        decay = freshness_decay(hours)

        base_score = quality * (1 + 0.1 * engagement_pct) * decay

        if personalized and user_weights:
            match = compute_match_score(item, user_weights)
            item['ranking_score'] = base_score * match
            item['match_score'] = round(match, 3)
        else:
            item['ranking_score'] = base_score

        item['ranking_score'] = round(item['ranking_score'], 6)

    # Step 3: sort descending
    items.sort(key=lambda x: x.get('ranking_score', 0), reverse=True)
    return items


def compute_match_score(item, user_weights):
    """Compute personalization match score (pure computation, no LLM).

    match_score = category_weight × keyword_boost
    """
    if not user_weights:
        return 1.0

    cat_weights = user_weights.get('category_weights', {})
    keyword_boosts = user_weights.get('keyword_boosts', [])

    # Category weight
    cat = canonicalize_category(item.get('ai_category', 'other')) or 'other'
    cat_w = cat_weights.get(cat, 1.0)
    # Floor at 0.15 to avoid total suppression
    cat_w = max(0.15, cat_w)

    # Keyword boost
    item_keywords = []
    kw_raw = item.get('ai_keywords')
    if kw_raw:
        if isinstance(kw_raw, str):
            try:
                item_keywords = json.loads(kw_raw)
            except (json.JSONDecodeError, TypeError):
                item_keywords = []
        elif isinstance(kw_raw, list):
            item_keywords = kw_raw

    item_keywords_lower = [k.lower() for k in item_keywords if k]
    title_lower = (item.get('title') or '').lower()
    summary_lower = (item.get('ai_summary') or '').lower()

    overlap = 0
    for boost_kw in keyword_boosts:
        kw_lower = boost_kw.lower()
        if kw_lower in item_keywords_lower or kw_lower in title_lower or kw_lower in summary_lower:
            overlap += 1

    keyword_mult = 1.0 + 0.25 * overlap

    return cat_w * keyword_mult
