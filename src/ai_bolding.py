"""Helpers for measuring AI-summary Markdown bolding."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


_BOLD_SPAN_RE = re.compile(r"\*\*[^*\n]+?\*\*")
_HEADING_BOLD_LINE_RE = re.compile(r"^\s*\*\*[^*\n]+?\*\*\s*$", re.MULTILINE)


def summarize_cluster_bolding(summary: str | None) -> dict[str, Any]:
    text = summary or ""
    speed, breakdown = _split_cluster_sections(text)
    summary_bold = count_bold_spans(speed)
    breakdown_bold = count_bold_spans(breakdown)
    heading_bold = len(_HEADING_BOLD_LINE_RE.findall(breakdown))
    body_bold = max(0, breakdown_bold - heading_bold)
    total = summary_bold + breakdown_bold
    return {
        "summary_bold_spans": summary_bold,
        "body_bold_spans": body_bold,
        "heading_bold_spans": heading_bold,
        "total_bold_spans": total,
        "has_bold": total > 0,
        "has_non_heading_bold": (summary_bold + body_bold) > 0,
        "only_heading_bold": total > 0 and summary_bold == 0 and body_bold == 0,
    }


def summarize_item_bolding(summary: str | None, key_points: Any) -> dict[str, Any]:
    summary_bold = count_bold_spans(summary or "")
    key_point_text = "\n".join(_flatten_key_point_text(key_points))
    body_bold = count_bold_spans(key_point_text)
    total = summary_bold + body_bold
    return {
        "summary_bold_spans": summary_bold,
        "body_bold_spans": body_bold,
        "heading_bold_spans": 0,
        "total_bold_spans": total,
        "has_bold": total > 0,
        "has_non_heading_bold": total > 0,
        "only_heading_bold": False,
    }


def count_bold_spans(text: str | None) -> int:
    return len(_BOLD_SPAN_RE.findall(text or ""))


def record_bolding_stats(
    *,
    source: str,
    record_id: str | int,
    candidate_count: int,
    stats: dict[str, Any],
) -> None:
    """Best-effort JSONL metrics; never affect generation or publishing."""
    try:
        root = Path(__file__).resolve().parents[1]
        logs = root / "logs"
        logs.mkdir(exist_ok=True)
        line = json.dumps(
            {
                "ts": datetime.now(timezone.utc).isoformat(),
                "source": source,
                "id": record_id,
                "candidate_count": candidate_count,
                **stats,
            },
            ensure_ascii=False,
        )
        with open(logs / "ai_bolding_stats.jsonl", "a") as f:
            f.write(line + "\n")
    except Exception:
        pass


def cluster_needs_bolding(summary: str | None) -> bool:
    stats = summarize_cluster_bolding(summary)
    return not stats["has_non_heading_bold"]


def item_needs_bolding(summary: str | None, key_points: Any) -> bool:
    return not summarize_item_bolding(summary, key_points)["has_non_heading_bold"]


def _split_cluster_sections(text: str) -> tuple[str, str]:
    if "【全文拆解】" not in text:
        return re.sub(r"^【精华速览】\s*", "", text).strip(), ""
    speed, breakdown = text.split("【全文拆解】", 1)
    speed = re.sub(r"^【精华速览】\s*", "", speed).strip()
    return speed, breakdown.strip()


def _flatten_key_point_text(key_points: Any) -> list[str]:
    lines: list[str] = []
    if not isinstance(key_points, list):
        return lines
    for item in key_points:
        if isinstance(item, str):
            if item.strip():
                lines.append(item.strip())
        elif isinstance(item, dict):
            points = item.get("points")
            if isinstance(points, list):
                lines.extend(str(p).strip() for p in points if str(p).strip())
    return lines
