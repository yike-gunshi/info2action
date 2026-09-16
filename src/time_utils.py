"""Shared timestamp parsing/normalization helpers.

The DB currently contains a mix of ISO strings, RFC 2822 feed dates, and
timezone-less local timestamps. Normalize them before comparing or returning
them to the frontend.
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
from typing import Any


LOCAL_NAIVE_TZ = timezone(timedelta(hours=8))


def highlights_published_before(now: datetime | None = None) -> datetime:
    """Exclusive UTC cutoff for today's Beijing publication issue.

    Use the server clock, never a client-supplied timezone or date. A future
    source date stays stored and becomes eligible when that publication day
    arrives; timestamps later within today remain eligible.
    """
    local_now = (now or datetime.now(timezone.utc)).astimezone(LOCAL_NAIVE_TZ)
    tomorrow = local_now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
    return tomorrow.astimezone(timezone.utc)


def parse_datetime(value: Any) -> datetime | None:
    """Parse supported timestamp formats into UTC-aware datetime.

    Naive timestamps in this project have historically been written in local
    Beijing time, so they are interpreted as UTC+8 before conversion.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value).strip()
        if not text:
            return None
        normalized = text
        if normalized.endswith("Z"):
            normalized = normalized[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(normalized)
        except ValueError:
            try:
                dt = parsedate_to_datetime(text)
            except (TypeError, ValueError, IndexError, OverflowError):
                return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=LOCAL_NAIVE_TZ)
    return dt.astimezone(timezone.utc)


def to_utc_iso(value: Any) -> str | None:
    """Return canonical UTC ISO string: YYYY-MM-DDTHH:MM:SSZ."""
    dt = parse_datetime(value)
    if dt is None:
        return None
    return dt.replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def sort_key(value: Any) -> float:
    """Numeric sort key for timestamps. Invalid/missing values sort oldest."""
    dt = parse_datetime(value)
    if dt is None:
        return float("-inf")
    return dt.timestamp()
