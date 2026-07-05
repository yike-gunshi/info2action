"""Helpers for canonical category IDs and legacy alias compatibility."""

from __future__ import annotations


ACTIVE_CATEGORY_IDS = (
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
    "other",
)


LEGACY_CATEGORY_ALIASES = {
    # v3.1 → v4.0 改名
    "ai_tools": "efficiency_tools",
    "tools": "efficiency_tools",
    "insights": "tech",
}


def canonicalize_category(category: str | None) -> str | None:
    """Map legacy category IDs to the active taxonomy."""
    if not category:
        return category
    return LEGACY_CATEGORY_ALIASES.get(category, category)


def expand_query_categories(category: str | None) -> list[str]:
    """Return all DB category IDs that should be treated as this category."""
    canonical = canonicalize_category(category)
    if not canonical:
        return []

    aliases = [canonical]
    for legacy, active in LEGACY_CATEGORY_ALIASES.items():
        if active == canonical and legacy not in aliases:
            aliases.append(legacy)
    return aliases


def ensure_all_categories(weights: dict[str, float] | None, fill: float = 1.0) -> dict[str, float]:
    """Backfill any missing active categories with a default value."""
    normalized: dict[str, float] = {}
    for key, value in (weights or {}).items():
        normalized[canonicalize_category(key) or key] = value
    for category_id in ACTIVE_CATEGORY_IDS:
        normalized.setdefault(category_id, fill)
    return normalized
