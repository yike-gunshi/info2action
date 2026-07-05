#!/usr/bin/env python3
"""Offline cluster highlight scoring calibration.

Modes:
- snapshot: read recent remote clusters and write local JSONL samples.
- classify-offline: score snapshot rows with an LLM and derive internal scores.
- analyze: summarize duplicate/rerun stability from classification artifacts.

No mode writes remote tables or refreshes highlights_v1.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import enrich_items  # noqa: E402
import highlight_scoring  # noqa: E402
from env_utils import load_project_env  # noqa: E402
from prompt_loader import load_prompt  # noqa: E402


DEFAULT_STATE_DIR = ROOT / "data" / "highlights_score_calibration"
DEFAULT_SNAPSHOT_FILE = DEFAULT_STATE_DIR / "snapshot.jsonl"
DEFAULT_CLASSIFICATION_FILE = DEFAULT_STATE_DIR / "classified.jsonl"
DEFAULT_REDERIVED_FILE = DEFAULT_STATE_DIR / "classified_rederived.jsonl"
DEFAULT_ANALYSIS_FILE = DEFAULT_STATE_DIR / "analysis.json"
DEFAULT_REVIEW_FILE = DEFAULT_STATE_DIR / "human_review.csv"
DEFAULT_CALIBRATION_FILE = DEFAULT_STATE_DIR / "threshold_calibration.json"
PROMPT_FILE = "11_cluster_highlight_score.md"
DEFAULT_APP_BASE_URL = "https://www.info2act.com"
HIDDEN_CATEGORY_IDS = {"other", "_uncategorized", "__uncategorized__"}
DIMENSION_FEEDBACK_VALUES = {"ok", "high", "low", "unsure", "unchecked"}
EVIDENCE_FEEDBACK_VALUES = {"supported", "insufficient", "wrong", "unsure", "unchecked"}
SCORE_FEEDBACK_VALUES = {"ok", "high", "low", "unsure", "unchecked"}
ERROR_REASON_VALUES = {
    "low_value",
    "category_wrong",
    "dimension_wrong",
    "evidence_unsupported",
    "score_wrong",
    "marketing_noise",
    "borderline",
}

REVIEW_FIELDS = [
    "sample_key",
    "sample_variant",
    "cluster_id",
    "title",
    "system_categories_json",
    "cluster_url",
    "sources_json",
    "content_type",
    "hard_gate",
    "bucket",
    "diagnostic_band",
    "derived_score",
    "confidence",
    "marketing_noise",
    "applied_caps_json",
    "source_count",
    "platforms",
    "reason_codes",
    "dimension_scores_json",
    "dimension_evidence_json",
    "raw_result_json",
    "pairwise_group_id",
    "pairwise_role",
    "human_should_featured",
    "human_categories_json",
    "human_dimension_feedback_json",
    "human_evidence_feedback",
    "human_score_feedback",
    "human_error_reasons_json",
    "human_notes",
]
EDITABLE_REVIEW_FIELDS = {
    "human_should_featured",
    "human_categories_json",
    "human_dimension_feedback_json",
    "human_evidence_feedback",
    "human_score_feedback",
    "human_error_reasons_json",
    "human_notes",
}


def _iso_utc(value: datetime | None = None) -> str:
    value = value or datetime.now(timezone.utc)
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _json_default(value: Any) -> str:
    if isinstance(value, datetime):
        return _iso_utc(value)
    return str(value)


def _write_jsonl(path: Path, rows: Iterable[dict[str, Any]], *, append: bool = True) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = "a" if append else "w"
    with path.open(mode, encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, default=_json_default))
            handle.write("\n")


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            text = line.strip()
            if not text:
                continue
            rows.append(json.loads(text))
    return rows


def _parse_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        values[key] = value
    return values


def load_env_file_into_process(path_value: str | None) -> dict[str, str]:
    """Load an optional .env file into os.environ without printing secrets."""
    if not path_value:
        return {}
    path = Path(path_value).expanduser()
    if path.is_dir():
        values = load_project_env(path)
    else:
        if not path.exists():
            raise FileNotFoundError(f"env file not found: {path}")
        values = _parse_env_file(path)
    loaded: dict[str, str] = {}
    for key, value in values.items():
        if key in os.environ:
            loaded[key] = "existing"
            continue
        os.environ[key] = value
        loaded[key] = "loaded"
    return loaded


def _coerce_json_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, list) else []
        except (TypeError, ValueError, json.JSONDecodeError):
            return []
    return []


def _coerce_json_dict(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except (TypeError, ValueError, json.JSONDecodeError):
            return {}
    return {}


def _source_count_bucket(row: dict[str, Any]) -> str:
    count = int(row.get("source_count") or row.get("unique_source_count") or row.get("doc_count") or 0)
    if count <= 1:
        return "single_source"
    if count <= 3:
        return "few_sources"
    return "multi_source"


def select_snapshot_rows(
    clusters: list[dict[str, Any]],
    *,
    sample_limit: int,
    duplicate_count: int,
) -> list[dict[str, Any]]:
    """Deterministically stratify recent clusters and add duplicate samples."""
    limit = max(1, int(sample_limit))
    duplicate_count = max(0, int(duplicate_count))
    sorted_rows = sorted(
        clusters,
        key=lambda row: str(row.get("last_updated_at") or row.get("sort_at") or ""),
        reverse=True,
    )
    buckets: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    bucket_order: list[tuple[str, str]] = []
    for row in sorted_rows:
        bucket = (str(row.get("category") or "unknown"), _source_count_bucket(row))
        if bucket not in buckets:
            bucket_order.append(bucket)
        buckets[bucket].append(row)

    selected: list[dict[str, Any]] = []
    while len(selected) < limit and bucket_order:
        made_progress = False
        for bucket in list(bucket_order):
            if len(selected) >= limit:
                break
            rows = buckets[bucket]
            if not rows:
                bucket_order.remove(bucket)
                continue
            selected.append(rows.pop(0))
            made_progress = True
        if not made_progress:
            break

    outputs: list[dict[str, Any]] = []
    for row in selected:
        cluster_id = str(row.get("cluster_id") or row.get("id") or "")
        outputs.append({
            **row,
            "sample_key": cluster_id,
            "sample_variant": "original",
        })

    for idx, row in enumerate(outputs[:duplicate_count], start=1):
        outputs.append({
            **row,
            "sample_variant": f"duplicate_{idx}",
        })
    return outputs


def build_cluster_payload(cluster: dict[str, Any]) -> str:
    """Build LLM input without exposing any internal percent score."""
    platforms = cluster.get("platforms") or _coerce_json_list(cluster.get("platforms_json"))
    sources = cluster.get("sources") or []
    lines = [
        f"cluster_id: {cluster.get('cluster_id') or cluster.get('id') or ''}",
        f"title: {cluster.get('title') or cluster.get('ai_title') or ''}",
        f"summary: {cluster.get('summary') or cluster.get('ai_summary') or ''}",
        f"doc_count: {cluster.get('doc_count') or ''}",
        f"source_count: {cluster.get('source_count') or cluster.get('unique_source_count') or ''}",
        f"platforms: {', '.join(str(item) for item in platforms)}",
        f"category: {cluster.get('category') or ''}",
        f"first_doc_at: {cluster.get('first_doc_at') or ''}",
        f"last_doc_at: {cluster.get('last_doc_at') or ''}",
        "",
        "sources:",
    ]
    for idx, source in enumerate(sources[:10], start=1):
        lines.extend([
            f"- source_{idx}_title: {source.get('title') or ''}",
            f"  platform: {source.get('platform') or ''}",
            f"  source: {source.get('source') or source.get('author_name') or ''}",
            f"  published_at: {source.get('published_at') or source.get('fetched_at') or ''}",
            f"  url: {source.get('url') or ''}",
            f"  summary: {source.get('summary') or source.get('ai_summary') or ''}",
        ])
    return "\n".join(lines)


def _json_dumps(value: Any) -> str:
    return json.dumps(value if value is not None else {}, ensure_ascii=False, default=_json_default)


def _category_id(value: Any) -> str:
    text = str(value or "").strip().lower()
    if not text:
        return ""
    return text.split("[", 1)[0].strip()


def _append_unique(values: list[str], value: Any) -> None:
    text = _category_id(value)
    if text and text not in HIDDEN_CATEGORY_IDS and text not in values:
        values.append(text)


def system_categories(row: dict[str, Any]) -> list[str]:
    categories: list[str] = []
    for field in ("category", "ai_category"):
        _append_unique(categories, row.get(field))
    for field in ("categories", "ai_categories"):
        for item in _coerce_json_list(row.get(field)):
            _append_unique(categories, item)
    for source in row.get("sources") or []:
        if not isinstance(source, dict):
            continue
        _append_unique(categories, source.get("ai_category"))
        for item in _coerce_json_list(source.get("ai_categories")):
            _append_unique(categories, item)
    return categories


def review_sources(row: dict[str, Any], *, limit: int = 10) -> list[dict[str, Any]]:
    sources: list[dict[str, Any]] = []
    for source in (row.get("sources") or [])[:limit]:
        if not isinstance(source, dict):
            continue
        sources.append({
            "id": source.get("id") or "",
            "title": source.get("title") or "",
            "url": source.get("url") or "",
            "platform": source.get("platform") or "",
            "source": source.get("source") or "",
            "author_name": source.get("author_name") or "",
            "published_at": source.get("published_at") or source.get("fetched_at") or "",
            "ai_summary": source.get("ai_summary") or source.get("summary") or "",
            "is_primary_source": bool(source.get("is_primary_source")),
            "ai_category": source.get("ai_category") or "",
            "ai_categories": _coerce_json_list(source.get("ai_categories")),
        })
    return sources


def cluster_url(row: dict[str, Any], *, app_base_url: str = DEFAULT_APP_BASE_URL) -> str:
    cluster_id = str(row.get("cluster_id") or row.get("id") or row.get("sample_key") or "").strip()
    if not cluster_id:
        return ""
    base = str(app_base_url or DEFAULT_APP_BASE_URL).strip().rstrip("/")
    return f"{base}#cluster={cluster_id}"


def default_dimension_feedback() -> dict[str, str]:
    return {dimension: "unchecked" for dimension in (*highlight_scoring.DIMENSIONS, highlight_scoring.NOISE_FIELD)}


def normalize_dimension_feedback(value: Any) -> dict[str, str]:
    raw = _coerce_json_dict(value)
    normalized = default_dimension_feedback()
    for dimension in normalized:
        candidate = str(raw.get(dimension) or "").strip()
        if candidate in DIMENSION_FEEDBACK_VALUES:
            normalized[dimension] = candidate
    return normalized


def _score_value(row: dict[str, Any]) -> float:
    try:
        return float(row.get("derived_score") or 0)
    except (TypeError, ValueError):
        return 0.0


def _int_value(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _split_reason_field(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return [item.strip() for item in str(value or "").split(",") if item.strip()]


def build_review_rows(
    classified_rows: list[dict[str, Any]],
    *,
    pairwise_count: int,
    include_duplicates: bool,
    app_base_url: str = DEFAULT_APP_BASE_URL,
) -> list[dict[str, Any]]:
    """Build rows for human calibration labels and within-type A/B review."""
    rows = [
        row for row in classified_rows
        if include_duplicates or str(row.get("sample_variant") or "original") == "original"
    ]
    rows = sorted(rows, key=lambda row: (_score_value(row), str(row.get("sample_key") or "")), reverse=True)
    review_rows: list[dict[str, Any]] = []
    for row in rows:
        raw_result = row.get("raw_result") if isinstance(row.get("raw_result"), dict) else {}
        platforms = row.get("platforms") or _coerce_json_list(row.get("platforms_json"))
        key = str(row.get("sample_key") or row.get("cluster_id") or row.get("id") or "")
        review_rows.append({
            "sample_key": key,
            "sample_variant": row.get("sample_variant") or "original",
            "cluster_id": row.get("cluster_id") or row.get("id") or "",
            "title": row.get("title") or row.get("ai_title") or "",
            "system_categories_json": _json_dumps(system_categories(row)),
            "cluster_url": cluster_url(row, app_base_url=app_base_url),
            "sources_json": _json_dumps(review_sources(row)),
            "content_type": row.get("content_type") or row.get("profile_id") or "",
            "hard_gate": row.get("hard_gate") or "",
            "bucket": row.get("bucket") or row.get("advisory_bucket") or "",
            "diagnostic_band": row.get("diagnostic_band") or "",
            "derived_score": row.get("derived_score") if row.get("derived_score") is not None else "",
            "confidence": row.get("confidence") if row.get("confidence") is not None else "",
            "marketing_noise": row.get("marketing_noise") if row.get("marketing_noise") is not None else "",
            "applied_caps_json": _json_dumps(row.get("applied_caps") or []),
            "source_count": row.get("source_count") or row.get("unique_source_count") or row.get("doc_count") or "",
            "platforms": ",".join(str(item) for item in platforms),
            "reason_codes": ",".join(str(item) for item in (row.get("reason_codes") or [])),
            "dimension_scores_json": _json_dumps(row.get("dimension_scores") or raw_result.get("dimension_scores") or {}),
            "dimension_evidence_json": _json_dumps(raw_result.get("dimension_evidence") or row.get("dimension_evidence") or {}),
            "raw_result_json": _json_dumps(raw_result),
            "pairwise_group_id": "",
            "pairwise_role": "",
            "human_should_featured": "",
            "human_categories_json": "[]",
            "human_dimension_feedback_json": _json_dumps(default_dimension_feedback()),
            "human_evidence_feedback": "unchecked",
            "human_score_feedback": "unchecked",
            "human_error_reasons_json": "[]",
            "human_notes": "",
        })

    by_type: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in review_rows:
        by_type[str(row.get("content_type") or "unknown")].append(row)
    pair_total = 0
    for content_type in sorted(by_type):
        typed_rows = by_type[content_type]
        for idx in range(0, len(typed_rows) - 1, 2):
            if pair_total >= pairwise_count:
                break
            pair_total += 1
            group_id = f"{content_type}_pair_{pair_total}"
            typed_rows[idx]["pairwise_group_id"] = group_id
            typed_rows[idx]["pairwise_role"] = "A"
            typed_rows[idx + 1]["pairwise_group_id"] = group_id
            typed_rows[idx + 1]["pairwise_role"] = "B"
        if pair_total >= pairwise_count:
            break
    return review_rows


def write_review_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=REVIEW_FIELDS)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in REVIEW_FIELDS})


def _normalize_json_list_field(value: Any, *, allowed: set[str] | None = None) -> str:
    items = []
    for item in _coerce_json_list(value):
        text = str(item or "").strip()
        if not text:
            continue
        if allowed is not None and text not in allowed:
            continue
        if text not in items:
            items.append(text)
    return _json_dumps(items)


def _normalize_choice_field(value: Any, allowed: set[str], *, default: str = "unchecked") -> str:
    text = str(value or "").strip()
    return text if text in allowed else default


def _normalize_review_update_field(field: str, value: Any) -> str:
    if field == "human_should_featured":
        return _normalize_human_label(value) or str(value or "").strip()
    if field == "human_categories_json":
        return _normalize_json_list_field(value)
    if field == "human_dimension_feedback_json":
        return _json_dumps(normalize_dimension_feedback(value))
    if field == "human_evidence_feedback":
        return _normalize_choice_field(value, EVIDENCE_FEEDBACK_VALUES)
    if field == "human_score_feedback":
        return _normalize_choice_field(value, SCORE_FEEDBACK_VALUES)
    if field == "human_error_reasons_json":
        return _normalize_json_list_field(value, allowed=ERROR_REASON_VALUES)
    return str(value or "").strip()


def apply_review_updates(review_file: Path, updates: list[dict[str, Any]]) -> dict[str, Any]:
    rows = _read_review_csv(review_file)
    by_key = {str(row.get("sample_key") or ""): row for row in rows}
    missing: list[str] = []
    updated = 0
    for update in updates:
        key = str(update.get("sample_key") or "").strip()
        if not key or key not in by_key:
            if key:
                missing.append(key)
            continue
        row = by_key[key]
        for field in EDITABLE_REVIEW_FIELDS:
            if field not in update:
                continue
            row[field] = _normalize_review_update_field(field, update.get(field))
        updated += 1
    write_review_csv(review_file, rows)
    return {"updated": updated, "missing": missing}


def preserve_existing_review_fields(new_rows: list[dict[str, Any]], existing_rows: list[dict[str, Any]]) -> None:
    existing_by_key = {
        str(row.get("sample_key") or ""): row
        for row in existing_rows
        if str(row.get("sample_key") or "")
    }
    for row in new_rows:
        previous = existing_by_key.get(str(row.get("sample_key") or ""))
        if not previous:
            continue
        for field in EDITABLE_REVIEW_FIELDS:
            value = str(previous.get(field) or "").strip()
            if value:
                row[field] = value


def summarize_classification_rows(rows: list[dict[str, Any]]) -> dict[str, Any]:
    scores = sorted(_score_value(row) for row in rows)
    midpoint = len(scores) // 2
    score_summary = {
        "count": len(scores),
        "min": scores[0] if scores else None,
        "max": scores[-1] if scores else None,
        "avg": round(sum(scores) / len(scores), 2) if scores else None,
        "median": scores[midpoint] if scores and len(scores) % 2 == 1 else (
            round((scores[midpoint - 1] + scores[midpoint]) / 2, 2) if scores else None
        ),
    }
    return {
        "content_type_counts": dict(Counter(str(row.get("content_type") or row.get("profile_id") or "unknown") for row in rows)),
        "hard_gate_counts": dict(Counter(str(row.get("hard_gate") or "unknown") for row in rows)),
        "bucket_counts": dict(Counter(str(row.get("bucket") or row.get("advisory_bucket") or "unknown") for row in rows)),
        "score_summary": score_summary,
    }


def summarize_review_file(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"present": False}
    with path.open("r", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    label_values = [
        str(row.get("human_should_featured") or "").strip()
        for row in rows
        if str(row.get("human_should_featured") or "").strip()
    ]
    error_reasons = [
        str(reason)
        for row in rows
        for reason in _coerce_json_list(row.get("human_error_reasons_json"))
        if str(reason).strip()
    ]
    pairwise_labeled = [
        row for row in rows
        if str(row.get("pairwise_group_id") or "").strip()
        and str(row.get("pairwise_winner_sample_key") or "").strip()
    ]
    return {
        "present": True,
        "row_count": len(rows),
        "labeled_count": len(label_values),
        "human_should_featured_counts": dict(Counter(label_values)),
        "human_error_reason_counts": dict(Counter(error_reasons)),
        "human_error_reasons_counts": dict(Counter(error_reasons)),
        "pairwise_labeled_count": len(pairwise_labeled),
    }


def build_review_progress(
    rows: list[dict[str, Any]],
    *,
    min_labeled: int = 30,
    min_positive: int = 10,
    min_negative: int = 10,
) -> dict[str, Any]:
    normalized_labels = [_normalize_human_label(row.get("human_should_featured")) for row in rows]
    yes_count = sum(1 for label in normalized_labels if label == "yes")
    no_count = sum(1 for label in normalized_labels if label == "no")
    review_count = sum(1 for label in normalized_labels if label == "review")
    calibration_labeled = yes_count + no_count
    remaining = {
        "labeled": max(0, min_labeled - calibration_labeled),
        "yes": max(0, min_positive - yes_count),
        "no": max(0, min_negative - no_count),
    }
    ready = not any(remaining.values())
    if remaining["yes"]:
        next_action = f"标注至少 {remaining['yes']} 条 yes"
    elif remaining["no"]:
        next_action = f"标注至少 {remaining['no']} 条 no"
    elif remaining["labeled"]:
        next_action = f"再标注至少 {remaining['labeled']} 条 yes/no"
    else:
        next_action = "可以运行阈值校准"
    return {
        "row_count": len(rows),
        "human_label_counts": dict(Counter(label or "empty" for label in normalized_labels)),
        "calibration_labeled_count": calibration_labeled,
        "yes_count": yes_count,
        "no_count": no_count,
        "review_count": review_count,
        "min_labeled": min_labeled,
        "min_positive": min_positive,
        "min_negative": min_negative,
        "remaining": remaining,
        "ready_for_threshold_inputs": ready,
        "next_action": next_action,
    }


def load_review_category_options() -> list[dict[str, Any]]:
    path = ROOT / "config" / "classification.json"
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    categories: list[dict[str, Any]] = []
    for category in data.get("categories") or []:
        category_id = str(category.get("id") or "").strip()
        if not category_id or category_id in HIDDEN_CATEGORY_IDS or not category.get("visible", True):
            continue
        categories.append({
            "id": category_id,
            "name": category.get("name") or category_id,
            "priority": category.get("priority") or 99,
        })
    categories.sort(key=lambda item: (int(item.get("priority") or 99), str(item.get("id") or "")))
    return categories


def _normalize_human_label(value: Any) -> str:
    text = str(value or "").strip().lower()
    yes_values = {"yes", "y", "true", "1", "是", "进", "精选", "should", "include"}
    no_values = {"no", "n", "false", "0", "否", "不进", "不要", "exclude"}
    review_values = {"review", "manual_review", "复核", "待定", "边界"}
    if text in yes_values:
        return "yes"
    if text in no_values:
        return "no"
    if text in review_values:
        return "review"
    return ""


def _review_by_sample_key(review_rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {
        str(row.get("sample_key") or ""): row
        for row in review_rows
        if str(row.get("sample_key") or "")
    }


def _labeled_threshold_rows(
    classified_rows: list[dict[str, Any]],
    review_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    review_by_key = _review_by_sample_key(review_rows)
    labeled: list[dict[str, Any]] = []
    for row in classified_rows:
        if str(row.get("sample_variant") or "original") != "original":
            continue
        key = str(row.get("sample_key") or row.get("cluster_id") or row.get("id") or "")
        review = review_by_key.get(key) or {}
        label = _normalize_human_label(review.get("human_should_featured"))
        if label not in {"yes", "no"}:
            continue
        labeled.append({
            **row,
            "human_should_featured": label,
            "human_error_reasons_json": review.get("human_error_reasons_json") or "[]",
            "human_notes": review.get("human_notes") or "",
            "calibration_score": _score_value(row),
            "calibration_content_type": str(row.get("content_type") or row.get("profile_id") or "unknown"),
        })
    return labeled


def _threshold_candidates(rows: list[dict[str, Any]]) -> list[int]:
    candidates = {0, 101}
    for row in rows:
        score = _score_value(row)
        candidates.add(max(0, min(100, int(score) + 1)))
        candidates.add(max(0, min(100, int(score))))
    return sorted(candidates)


def _evaluate_threshold(rows: list[dict[str, Any]], threshold: int) -> dict[str, Any]:
    true_positive = false_positive = true_negative = false_negative = 0
    false_positives: list[dict[str, Any]] = []
    false_negatives: list[dict[str, Any]] = []
    for row in rows:
        score = _score_value(row)
        predicted = score >= threshold
        actual = row["human_should_featured"] == "yes"
        if predicted and actual:
            true_positive += 1
        elif predicted and not actual:
            false_positive += 1
            false_positives.append(row)
        elif not predicted and actual:
            false_negative += 1
            false_negatives.append(row)
        else:
            true_negative += 1
    precision = true_positive / (true_positive + false_positive) if (true_positive + false_positive) else 0.0
    recall = true_positive / (true_positive + false_negative) if (true_positive + false_negative) else 0.0
    f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) else 0.0
    return {
        "threshold": threshold,
        "true_positive_count": true_positive,
        "false_positive_count": false_positive,
        "true_negative_count": true_negative,
        "false_negative_count": false_negative,
        "precision": round(precision, 4),
        "recall": round(recall, 4),
        "f1": round(f1, 4),
        "error_count": false_positive + false_negative,
        "false_positive_samples": [
            {
                "sample_key": row.get("sample_key"),
                "title": row.get("title"),
                "score": _score_value(row),
                "content_type": row.get("content_type"),
                "human_error_reasons_json": row.get("human_error_reasons_json"),
            }
            for row in false_positives[:20]
        ],
        "false_negative_samples": [
            {
                "sample_key": row.get("sample_key"),
                "title": row.get("title"),
                "score": _score_value(row),
                "content_type": row.get("content_type"),
                "human_error_reasons_json": row.get("human_error_reasons_json"),
            }
            for row in false_negatives[:20]
        ],
    }


def _best_threshold(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not rows:
        return None
    evaluated = [_evaluate_threshold(rows, threshold) for threshold in _threshold_candidates(rows)]
    return min(
        evaluated,
        key=lambda item: (
            item["error_count"],
            -item["f1"],
            -item["precision"],
            -item["recall"],
            item["threshold"],
        ),
    )


def calibrate_pairwise_preferences(
    classified_rows: list[dict[str, Any]],
    review_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    score_by_key = {
        str(row.get("sample_key") or row.get("cluster_id") or row.get("id") or ""): _score_value(row)
        for row in classified_rows
        if str(row.get("sample_variant") or "original") == "original"
    }
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in review_rows:
        group_id = str(row.get("pairwise_group_id") or "").strip()
        if group_id:
            groups[group_id].append(row)
    checked = 0
    agreed = 0
    disagreements: list[dict[str, Any]] = []
    for group_id, rows in groups.items():
        winner = ""
        for row in rows:
            candidate = str(row.get("pairwise_winner_sample_key") or "").strip()
            if candidate:
                winner = candidate
                break
        if not winner or winner not in score_by_key:
            continue
        scored_rows = [
            row for row in rows
            if str(row.get("sample_key") or "") in score_by_key
        ]
        if len(scored_rows) < 2:
            continue
        checked += 1
        top = max(scored_rows, key=lambda row: score_by_key[str(row.get("sample_key") or "")])
        top_key = str(top.get("sample_key") or "")
        if top_key == winner:
            agreed += 1
        else:
            disagreements.append({
                "pairwise_group_id": group_id,
                "human_winner_sample_key": winner,
                "score_winner_sample_key": top_key,
                "human_winner_score": score_by_key.get(winner),
                "score_winner_score": score_by_key.get(top_key),
            })
    return {
        "pairwise_group_count": checked,
        "score_agrees_with_winner_count": agreed,
        "score_agreement_rate": round(agreed / checked, 4) if checked else None,
        "disagreements": disagreements[:20],
    }


def calibrate_thresholds(
    classified_rows: list[dict[str, Any]],
    review_rows: list[dict[str, Any]],
    *,
    min_labeled: int = 30,
    min_positive: int = 10,
    min_negative: int = 10,
    min_type_labeled: int = 8,
) -> dict[str, Any]:
    labeled = _labeled_threshold_rows(classified_rows, review_rows)
    positive_count = sum(1 for row in labeled if row["human_should_featured"] == "yes")
    negative_count = sum(1 for row in labeled if row["human_should_featured"] == "no")
    blocking_reasons: list[str] = []
    if len(labeled) < min_labeled:
        blocking_reasons.append("not_enough_labeled_rows")
    if positive_count < min_positive:
        blocking_reasons.append("not_enough_positive_labels")
    if negative_count < min_negative:
        blocking_reasons.append("not_enough_negative_labels")

    by_type: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in labeled:
        by_type[row["calibration_content_type"]].append(row)
    type_thresholds: dict[str, Any] = {}
    for content_type, rows in sorted(by_type.items()):
        type_positive = sum(1 for row in rows if row["human_should_featured"] == "yes")
        type_negative = sum(1 for row in rows if row["human_should_featured"] == "no")
        if len(rows) < min_type_labeled or type_positive == 0 or type_negative == 0:
            type_thresholds[content_type] = {
                "ready": False,
                "labeled_count": len(rows),
                "positive_count": type_positive,
                "negative_count": type_negative,
            }
            continue
        type_thresholds[content_type] = {
            "ready": True,
            "labeled_count": len(rows),
            "positive_count": type_positive,
            "negative_count": type_negative,
            **(_best_threshold(rows) or {}),
        }
    return {
        "ready_for_threshold_decision": not blocking_reasons,
        "blocking_reasons": blocking_reasons,
        "labeled_count": len(labeled),
        "positive_count": positive_count,
        "negative_count": negative_count,
        "global": _best_threshold(labeled),
        "type_thresholds": type_thresholds,
        "pairwise": calibrate_pairwise_preferences(classified_rows, review_rows),
    }


def query_recent_clusters(*, days: int, scan_limit: int, statement_timeout_sec: int) -> list[dict[str, Any]]:
    """Read recent cluster samples from the remote DB in a read-only transaction."""
    import remote_db  # noqa: PLC0415

    schema = remote_db.remote_schema()
    safe_days = max(1, min(int(days), 30))
    safe_limit = max(1, min(int(scan_limit), 5000))
    cluster_sql = f"""
        WITH base_clusters AS (
            SELECT c.id AS cluster_id,
                   c.ai_title AS title,
                   c.ai_summary AS summary,
                   c.doc_count,
                   c.unique_source_count AS source_count,
                   c.platforms_json,
                   c.first_doc_at,
                   c.last_doc_at,
                   c.last_updated_at,
                   COALESCE(c.published_at, c.first_doc_at, c.last_doc_at, c.last_updated_at) AS sort_at
              FROM {schema}.clusters c
             WHERE c.is_visible_in_feed = true
               AND COALESCE(c.archived, false) = false
               AND c.merged_into IS NULL
               AND c.last_updated_at > now() - (%(days)s::int * interval '1 day')
             ORDER BY c.last_updated_at DESC NULLS LAST
             LIMIT %(limit)s
        ),
        category_counts AS (
            SELECT ci.cluster_id, i.ai_category AS category, count(*) AS n
              FROM {schema}.cluster_items ci
              JOIN {schema}.items i ON i.id = ci.item_id
              JOIN base_clusters b ON b.cluster_id = ci.cluster_id
             WHERE NULLIF(i.ai_category, '') IS NOT NULL
             GROUP BY ci.cluster_id, i.ai_category
        ),
        category_ranked AS (
            SELECT cluster_id,
                   category,
                   row_number() OVER (PARTITION BY cluster_id ORDER BY n DESC, category ASC) AS rn
              FROM category_counts
        )
        SELECT b.*, cr.category
          FROM base_clusters b
          LEFT JOIN category_ranked cr
                 ON cr.cluster_id = b.cluster_id
                AND cr.rn = 1
    """
    params = {"days": safe_days, "limit": safe_limit}
    with remote_db.connect() as conn:
        conn.execute("SET TRANSACTION READ ONLY")
        conn.execute(f"SET LOCAL statement_timeout = '{int(statement_timeout_sec)}s'")
        rows = [dict(row) for row in conn.execute(cluster_sql, params).fetchall()]
        cluster_ids = [row["cluster_id"] for row in rows]
        sources_by_cluster: dict[Any, list[dict[str, Any]]] = defaultdict(list)
        if cluster_ids:
            source_rows = conn.execute(
                f"""
                SELECT ci.cluster_id,
                       i.id,
                       i.title,
                       i.platform,
                       i.source,
                       i.author_name,
                       i.url,
                       i.ai_summary,
                       i.ai_category,
                       i.ai_categories,
                       i.ai_subcategories,
                       i.published_at,
                       i.fetched_at,
                       COALESCE(ci.is_primary_source, false) AS is_primary_source,
                       ci.rank_in_cluster
                  FROM {schema}.cluster_items ci
                  JOIN {schema}.items i ON i.id = ci.item_id
                 WHERE ci.cluster_id = ANY(%(cluster_ids)s)
                 ORDER BY ci.cluster_id ASC,
                          is_primary_source DESC,
                          ci.rank_in_cluster ASC NULLS LAST,
                          COALESCE(i.published_at, i.fetched_at) DESC NULLS LAST
                """,
                {"cluster_ids": cluster_ids},
            ).fetchall()
            for source in source_rows:
                source_dict = dict(source)
                if len(sources_by_cluster[source_dict["cluster_id"]]) < 10:
                    sources_by_cluster[source_dict["cluster_id"]].append(source_dict)
    for row in rows:
        row["platforms"] = _coerce_json_list(row.get("platforms_json"))
        row["sources"] = sources_by_cluster.get(row["cluster_id"], [])
    return rows


def classify_snapshot_row(
    row: dict[str, Any],
    *,
    system_prompt: str,
    api_key: str,
    api_base: str,
    model: str,
    max_tokens: int,
    temperature: float,
    rate_gate: enrich_items.MiniMaxRateLimitGate,
) -> dict[str, Any]:
    raw = enrich_items.call_minimax(
        api_key,
        api_base,
        model,
        system_prompt,
        build_cluster_payload(row),
        max_tokens=max_tokens,
        temperature=temperature,
        rate_gate=rate_gate,
    )
    normalized = highlight_scoring.normalize_llm_result(raw)
    derived = highlight_scoring.derive_highlight_score(normalized)
    return {
        **row,
        "raw_result": normalized,
        "hard_gate": normalized.get("hard_gate"),
        "content_type": normalized.get("content_type"),
        "dimension_scores": normalized.get("dimension_scores"),
        "marketing_noise": normalized.get("marketing_noise"),
        "bucket": normalized.get("bucket"),
        "confidence": normalized.get("confidence"),
        "reason_codes": normalized.get("reason_codes"),
        **derived,
        "classified_at": _iso_utc(),
    }


def _classification_key(row: dict[str, Any]) -> str:
    return f"{row.get('sample_key')}:{row.get('sample_variant')}"


def pending_classification_rows(
    snapshot_rows: list[dict[str, Any]],
    existing_rows: list[dict[str, Any]],
    *,
    resume: bool,
    retry_errors: bool,
    offline_limit: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    kept_rows = list(existing_rows)
    if resume and retry_errors:
        kept_rows = [row for row in existing_rows if not row.get("error")]
    existing_keys = {_classification_key(row) for row in kept_rows} if resume else set()
    pending = [row for row in snapshot_rows if _classification_key(row) not in existing_keys]
    if offline_limit:
        pending = pending[:offline_limit]
    return pending, kept_rows


def rederive_classification_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    outputs: list[dict[str, Any]] = []
    for row in rows:
        raw_result = row.get("raw_result") if isinstance(row.get("raw_result"), dict) else {}
        derived = highlight_scoring.derive_highlight_score(raw_result)
        outputs.append({
            **row,
            "previous_derived_score": row.get("derived_score"),
            "previous_policy_version": row.get("policy_version"),
            **derived,
            "rederived_at": _iso_utc(),
        })
    return outputs


def run_snapshot_mode(args: argparse.Namespace) -> int:
    rows = query_recent_clusters(
        days=args.days,
        scan_limit=args.scan_limit,
        statement_timeout_sec=args.db_statement_timeout_sec,
    )
    selected = select_snapshot_rows(rows, sample_limit=args.sample_limit, duplicate_count=args.duplicate_count)
    path = Path(args.snapshot_file)
    if path.exists() and not args.append_output:
        path.unlink()
    _write_jsonl(path, selected, append=True)
    print(f"[highlight-calibration] snapshot rows={len(selected)} file={path}", flush=True)
    return 0


def run_classify_offline_mode(args: argparse.Namespace) -> int:
    snapshot_file = Path(args.snapshot_file)
    classification_file = Path(args.classification_file)
    snapshot_rows = _read_jsonl(snapshot_file)
    if not snapshot_rows:
        print(f"[highlight-calibration] no snapshot rows: {snapshot_file}", flush=True)
        return 0
    existing_rows: list[dict[str, Any]] = []
    if args.resume_classification:
        existing_rows = _read_jsonl(classification_file)
    elif classification_file.exists() and not args.append_output:
        classification_file.unlink()

    pending, kept_rows = pending_classification_rows(
        snapshot_rows,
        existing_rows,
        resume=args.resume_classification,
        retry_errors=args.retry_errors,
        offline_limit=args.offline_limit,
    )
    if args.resume_classification and args.retry_errors:
        _write_jsonl(classification_file, kept_rows, append=False)
    if not pending:
        print("[highlight-calibration] no pending classification rows", flush=True)
        return 0

    config = enrich_items.load_config()
    api_key, api_base, model = enrich_items.resolve_minimax_runtime_config(config.get("ai_summary", {}))
    if not api_key:
        raise RuntimeError("MiniMax API key missing")
    system_prompt = load_prompt(PROMPT_FILE)
    if not system_prompt:
        raise RuntimeError(f"prompt missing: {PROMPT_FILE}")
    gate = enrich_items.MiniMaxRateLimitGate(min_interval=args.request_interval_sec)
    concurrency = min(args.classification_concurrency, max(1, len(pending)))
    print(
        f"[highlight-calibration] classify pending={len(pending)} concurrency={concurrency}",
        flush=True,
    )

    def classify(row: dict[str, Any]) -> dict[str, Any]:
        try:
            return classify_snapshot_row(
                row,
                system_prompt=system_prompt,
                api_key=api_key,
                api_base=api_base,
                model=model,
                max_tokens=args.max_tokens,
                temperature=args.temperature,
                rate_gate=gate,
            )
        except Exception as exc:  # noqa: BLE001
            return {
                **row,
                "hard_gate": "review",
                "content_type": "general",
                "bucket": "manual_review",
                "confidence": 0.0,
                "error": f"classify_error: {str(exc)[:200]}",
                "classified_at": _iso_utc(),
            }

    if concurrency <= 1:
        for idx, row in enumerate(pending, start=1):
            output = classify(row)
            _write_jsonl(classification_file, [output], append=True)
            print(
                f"[highlight-calibration] [{idx:04d}/{len(pending):04d}] "
                f"score={output.get('derived_score')} gate={output.get('hard_gate')} "
                f"type={output.get('content_type')} title={str(output.get('title') or '')[:80]}",
                flush=True,
            )
            if args.request_interval_sec > 0:
                time.sleep(0)
        return 0

    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        futures = {executor.submit(classify, row): idx for idx, row in enumerate(pending, start=1)}
        for done_count, future in enumerate(as_completed(futures), start=1):
            idx = futures[future]
            output = future.result()
            _write_jsonl(classification_file, [output], append=True)
            print(
                f"[highlight-calibration] [{done_count:04d}/{len(pending):04d}] "
                f"row={idx} score={output.get('derived_score')} gate={output.get('hard_gate')} "
                f"type={output.get('content_type')}",
                flush=True,
            )
    return 0


def run_analyze_mode(args: argparse.Namespace) -> int:
    rows = _read_jsonl(Path(args.classification_file))
    if not rows:
        print(f"[highlight-calibration] no classification rows: {args.classification_file}", flush=True)
        return 0
    summary = highlight_scoring.analyze_reliability(
        rows,
        score_jump_threshold=args.score_jump_threshold,
    )
    summary["classification_summary"] = summarize_classification_rows(rows)
    summary["human_review_summary"] = summarize_review_file(Path(args.review_file))
    review_rows = _read_review_csv(Path(args.review_file))
    summary["threshold_calibration"] = calibrate_thresholds(
        rows,
        review_rows,
        min_labeled=args.min_labeled,
        min_positive=args.min_positive,
        min_negative=args.min_negative,
        min_type_labeled=args.min_type_labeled,
    )
    summary["analysis_at"] = _iso_utc()
    summary["classification_file"] = str(args.classification_file)
    summary["final_threshold"] = args.final_threshold
    out = Path(args.analysis_file)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
    return 0


def _read_review_csv(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def run_export_review_mode(args: argparse.Namespace) -> int:
    rows = _read_jsonl(Path(args.classification_file))
    if not rows:
        print(f"[highlight-calibration] no classification rows: {args.classification_file}", flush=True)
        return 0
    review_rows = build_review_rows(
        rows,
        pairwise_count=args.pairwise_count,
        include_duplicates=args.include_duplicates,
        app_base_url=args.app_base_url,
    )
    out = Path(args.review_file)
    if out.exists():
        preserve_existing_review_fields(review_rows, _read_review_csv(out))
    write_review_csv(out, review_rows)
    print(
        f"[highlight-calibration] review rows={len(review_rows)} "
        f"pairwise_count={args.pairwise_count} file={out}",
        flush=True,
    )
    return 0


def run_calibrate_mode(args: argparse.Namespace) -> int:
    rows = _read_jsonl(Path(args.classification_file))
    review_rows = _read_review_csv(Path(args.review_file))
    result = calibrate_thresholds(
        rows,
        review_rows,
        min_labeled=args.min_labeled,
        min_positive=args.min_positive,
        min_negative=args.min_negative,
        min_type_labeled=args.min_type_labeled,
    )
    result["calibrated_at"] = _iso_utc()
    result["classification_file"] = str(args.classification_file)
    result["review_file"] = str(args.review_file)
    out = Path(args.calibration_file)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
    return 0


def _review_server_html() -> bytes:
    return """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Cluster Highlight Review</title>
  <style>
    :root { --border: #dfe3ea; --muted: #667085; --blue: #155eef; --green: #188038; --amber: #b06000; --red: #b3261e; }
    body { margin: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background: #f7f7f5; color: #1d1d1f; }
    header { position: sticky; top: 0; z-index: 2; display: flex; gap: 12px; align-items: center; padding: 12px 16px; background: #ffffff; border-bottom: 1px solid var(--border); }
    h1 { font-size: 18px; margin: 0; }
    select, input, button, textarea { font: inherit; }
    button { border: 1px solid #c8c8c8; background: #fff; border-radius: 6px; padding: 6px 10px; cursor: pointer; }
    button.primary { background: #155eef; border-color: #155eef; color: #fff; }
    a { color: var(--blue); text-decoration: none; }
    a:hover { text-decoration: underline; }
    main { display: grid; grid-template-columns: 320px minmax(520px, 1fr) 390px; min-height: calc(100vh - 56px); }
    aside { overflow: auto; border-right: 1px solid var(--border); background: #fff; }
    .row { padding: 10px 12px; border-bottom: 1px solid #eee; cursor: pointer; }
    .row.active { background: #eef4ff; }
    .row-title { font-weight: 600; font-size: 14px; line-height: 1.35; }
    .meta { color: var(--muted); font-size: 12px; margin-top: 4px; }
    section { padding: 18px 22px; overflow: auto; }
    .side { border-left: 1px solid var(--border); background: #fff; }
    .card { background: #fff; border: 1px solid var(--border); border-radius: 8px; padding: 16px; margin-bottom: 14px; }
    .grid { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 10px; }
    .pill { display: inline-block; border: 1px solid #ccd2dc; border-radius: 999px; padding: 2px 8px; margin: 2px; font-size: 12px; color: #344054; background: #f8fafc; }
    .score { font-weight: 700; color: var(--green); }
    label { display: block; font-size: 13px; color: #555; margin-bottom: 4px; }
    textarea { width: 100%; min-height: 92px; box-sizing: border-box; }
    select, input, textarea { border: 1px solid #ccd2dc; border-radius: 6px; padding: 7px 8px; box-sizing: border-box; background: #fff; }
    .full { width: 100%; }
    .actions { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 10px; }
    .source { padding: 11px 0; border-top: 1px solid #edf0f5; }
    .source:first-child { border-top: 0; }
    .source-title { font-weight: 600; font-size: 14px; line-height: 1.35; }
    .summary { font-size: 13px; color: #344054; line-height: 1.55; margin-top: 6px; }
    .dim-row, .feedback-row { display: grid; grid-template-columns: 118px 70px 1fr; gap: 10px; align-items: start; padding: 8px 0; border-top: 1px solid #edf0f5; }
    .feedback-row { grid-template-columns: 110px 1fr; }
    .dim-name { font-weight: 600; font-size: 13px; }
    .dim-score { justify-self: start; min-width: 32px; text-align: center; padding: 2px 8px; border-radius: 6px; border: 1px solid #b7dfc2; background: #f1fbf4; color: var(--green); font-weight: 700; }
    .reason-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 8px; }
    .check { display: flex; align-items: center; gap: 6px; font-size: 13px; color: #344054; }
    .check input { width: auto; }
    pre { white-space: pre-wrap; background: #f6f6f6; padding: 10px; border-radius: 6px; overflow: auto; }
  </style>
</head>
<body>
  <header>
    <h1>Cluster Review</h1>
    <select id="filter">
      <option value="">全部</option>
      <option value="unlabeled">未标注</option>
      <option value="yes">已标 yes</option>
      <option value="no">已标 no</option>
      <option value="review">已标 review</option>
    </select>
    <input id="query" placeholder="搜索标题 / 来源 / sample_key" size="32" />
    <button id="reload">刷新</button>
    <span id="stats" class="meta"></span>
  </header>
  <main>
    <aside id="list"></aside>
    <section>
      <div id="progress" class="card">加载中...</div>
      <div id="detail">加载中...</div>
    </section>
    <section class="side">
      <div id="feedback" class="card">加载中...</div>
    </section>
  </main>
  <script>
    let rows = [];
    let categories = [];
    let activeKey = "";
    const list = document.getElementById("list");
    const detail = document.getElementById("detail");
    const feedback = document.getElementById("feedback");
    const progress = document.getElementById("progress");
    const filter = document.getElementById("filter");
    const query = document.getElementById("query");
    const stats = document.getElementById("stats");
    const dimLabels = {
      information_value: "信息价值",
      usefulness: "有用性",
      timeliness: "时效性",
      authority_trust: "权威可信",
      content_depth: "内容深度",
      domain_fit: "领域匹配",
      cluster_incremental_value: "去重增量",
      marketing_noise: "营销噪声"
    };
    const dimensionIds = Object.keys(dimLabels);
    const dimensionFeedbackOptions = [
      ["unchecked", "未检查"],
      ["ok", "合理"],
      ["high", "偏高"],
      ["low", "偏低"],
      ["unsure", "不确定"]
    ];
    const evidenceOptions = [
      ["unchecked", "未检查"],
      ["supported", "支持"],
      ["insufficient", "不足"],
      ["wrong", "错误"],
      ["unsure", "不确定"]
    ];
    const scoreOptions = [
      ["unchecked", "未检查"],
      ["ok", "合理"],
      ["high", "偏高"],
      ["low", "偏低"],
      ["unsure", "不确定"]
    ];
    const errorReasons = [
      ["low_value", "价值不足"],
      ["category_wrong", "分类不准"],
      ["dimension_wrong", "维度分不准"],
      ["evidence_unsupported", "证据不支撑"],
      ["score_wrong", "综合分不准"],
      ["marketing_noise", "营销/噪声"],
      ["borderline", "边界样本"]
    ];
    function esc(s) { return String(s ?? "").replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }
    function parseJson(value, fallback) {
      if (value && typeof value === "object") return value;
      try { return JSON.parse(value || ""); } catch { return fallback; }
    }
    function chips(items) {
      return (items || []).filter(Boolean).map(x => `<span class="pill">${esc(x)}</span>`).join("");
    }
    function categoryLabel(id) {
      return (categories.find(c => c.id === id) || {}).name || id;
    }
    function optionTags(options, value) {
      return options.map(([id, label]) => `<option value="${esc(id)}" ${id === value ? "selected" : ""}>${esc(label)}</option>`).join("");
    }
    function reviewProgress() {
      const counts = {yes: 0, no: 0, review: 0, empty: 0};
      for (const r of rows) {
        const label = (r.human_should_featured || "").trim() || "empty";
        counts[label] = (counts[label] || 0) + 1;
      }
      const calibration = counts.yes + counts.no;
      const remaining = {
        labeled: Math.max(0, 30 - calibration),
        yes: Math.max(0, 10 - counts.yes),
        no: Math.max(0, 10 - counts.no)
      };
      let next = "可以运行阈值校准";
      if (remaining.yes) next = `至少还要 ${remaining.yes} 条 yes`;
      else if (remaining.no) next = `至少还要 ${remaining.no} 条 no`;
      else if (remaining.labeled) next = `至少还要 ${remaining.labeled} 条 yes/no`;
      return {counts, calibration, remaining, next, ready: !remaining.labeled && !remaining.yes && !remaining.no};
    }
    function renderProgress() {
      const p = reviewProgress();
      progress.innerHTML = `
        <div class="grid">
          <div><label>threshold labels</label><strong>${p.calibration}/30</strong></div>
          <div><label>yes</label><strong>${p.counts.yes}/10</strong></div>
          <div><label>no</label><strong>${p.counts.no}/10</strong></div>
          <div><label>review</label><strong>${p.counts.review}</strong></div>
        </div>
        <p class="meta">${esc(p.ready ? "校准输入已达最低门槛" : p.next)} · 最低校准门槛：30 条 yes/no，并保持 yes/no 都有样本。</p>`;
    }
    function filteredRows() {
      const f = filter.value;
      const q = query.value.trim().toLowerCase();
      return rows.filter(r => {
        const label = (r.human_should_featured || "").trim();
        if (f === "unlabeled" && label) return false;
        if (["yes","no","review"].includes(f) && label !== f) return false;
        if (q) {
          const sources = parseJson(r.sources_json, []);
          const hay = [
            r.sample_key, r.title, r.reason_codes, r.content_type,
            ...sources.map(s => `${s.title || ""} ${s.source || ""} ${s.author_name || ""}`)
          ].join(" ").toLowerCase();
          if (!hay.includes(q)) return false;
        }
        return true;
      });
    }
    function renderList() {
      const visible = filteredRows();
      const labeled = rows.filter(r => (r.human_should_featured || "").trim()).length;
      const p = reviewProgress();
      stats.textContent = `${labeled}/${rows.length} 已标注 · 阈值 ${p.calibration}/30`;
      list.innerHTML = visible.map(r => `
        <div class="row ${r.sample_key === activeKey ? "active" : ""}" data-key="${esc(r.sample_key)}">
          <div class="row-title">${esc(r.title)}</div>
          <div class="meta">${esc(r.sample_key)} · LLM ${esc(r.content_type)} · <span class="score">${esc(r.derived_score)}</span> · ${esc(r.human_should_featured || "unlabeled")}</div>
          <div class="meta">${chips(parseJson(r.system_categories_json, []).map(categoryLabel))}</div>
        </div>`).join("");
      for (const node of list.querySelectorAll(".row")) {
        node.onclick = () => show(node.dataset.key);
      }
      if (!activeKey && visible[0]) show(visible[0].sample_key);
    }
    function show(key) {
      activeKey = key;
      const r = rows.find(x => x.sample_key === key);
      renderList();
      if (!r) {
        detail.textContent = "没有选中样本";
        feedback.textContent = "没有选中样本";
        return;
      }
      const sources = parseJson(r.sources_json, []);
      const systemCats = parseJson(r.system_categories_json, []);
      const dimScores = parseJson(r.dimension_scores_json, {});
      const evidence = parseJson(r.dimension_evidence_json, {});
      const raw = parseJson(r.raw_result_json, {});
      const appliedCaps = parseJson(r.applied_caps_json, []);
      const evidenceBullets = dimensionIds.flatMap(id => (evidence[id] || []).slice(0, 2).map(text => [id, text])).slice(0, 8);
      detail.innerHTML = `
        <h2>${esc(r.title)}</h2>
        <div class="grid">
          <div><label>derived score</label><strong class="score">${esc(r.derived_score)} / 100</strong></div>
          <div><label>source count</label>${esc(r.source_count)}</div>
          <div><label>system categories</label>${chips(systemCats.map(categoryLabel)) || "-"}</div>
          <div><label>cluster</label>${r.cluster_url ? `<a href="${esc(r.cluster_url)}" target="_blank">Info2Act Cluster</a>` : "-"}</div>
        </div>
        <div class="card">
          <h3>原始 docs</h3>
          ${sources.length ? sources.map(source => `
            <div class="source">
              <div class="source-title">${source.url ? `<a href="${esc(source.url)}" target="_blank">${esc(source.title || "(无标题)")}</a>` : esc(source.title || "(无标题)")}</div>
              <div class="meta">${esc(source.platform || "")} · ${esc(source.source || source.author_name || "")} · ${esc(source.published_at || "")}</div>
              <div class="summary">${esc(source.ai_summary || "")}</div>
            </div>`).join("") : `<p class="meta">没有 sources 数据。</p>`}
        </div>
        <div class="card">
          <h3>当前评分框架结果</h3>
          ${dimensionIds.map(id => `
            <div class="dim-row">
              <div class="dim-name">${esc(dimLabels[id])}</div>
              <div class="dim-score">${esc(id === "marketing_noise" ? r.marketing_noise : (dimScores[id] ?? ""))}</div>
              <div class="meta">${esc((evidence[id] || [])[0] || "")}</div>
            </div>`).join("")}
          ${appliedCaps.length ? `<p class="meta">applied caps: ${esc(JSON.stringify(appliedCaps))}</p>` : ""}
        </div>
        <div class="card">
          <h3>LLM 评分相关输出</h3>
          <div class="grid">
            <div><label>hard gate</label>${esc(r.hard_gate)}</div>
            <div><label>content type</label>${esc(r.content_type)}</div>
            <div><label>confidence</label>${esc(r.confidence)}</div>
            <div><label>bucket</label>${esc(r.bucket)}</div>
          </div>
          <p>${chips(String(r.reason_codes || "").split(","))}</p>
          <ul>${evidenceBullets.map(([id, text]) => `<li><strong>${esc(dimLabels[id])}</strong>: ${esc(text)}</li>`).join("")}</ul>
          <details><summary>查看 raw LLM JSON</summary><pre>${esc(JSON.stringify(raw, null, 2))}</pre></details>
        </div>
        ${r.pairwise_group_id ? `<p class="meta">pair: ${esc(r.pairwise_group_id)} ${esc(r.pairwise_role)}</p>` : ""}`;
      renderFeedback(r);
    }
    function renderFeedback(r) {
      const selectedCats = parseJson(r.human_categories_json, []);
      const dimFeedback = parseJson(r.human_dimension_feedback_json, {});
      const errorValues = parseJson(r.human_error_reasons_json, []);
      feedback.innerHTML = `
        <h2>人工反馈</h2>
        <p><label>最终判断</label><select id="decision" class="full">
          <option value=""></option><option value="yes">进精选</option><option value="no">不进精选</option><option value="review">边界复核</option>
        </select></p>
        <div>
          <label>线上分类校验（可多选）</label>
          <div class="reason-grid">
            ${categories.map(cat => `<label class="check"><input type="checkbox" name="category" value="${esc(cat.id)}" ${selectedCats.includes(cat.id) ? "checked" : ""}>${esc(cat.name)}</label>`).join("")}
          </div>
          <p class="meta">当前系统分类：${chips(parseJson(r.system_categories_json, []).map(categoryLabel)) || "-"}</p>
        </div>
        <div>
          <label>维度评分校验</label>
          ${dimensionIds.map(id => `
            <div class="feedback-row">
              <div>${esc(dimLabels[id])}</div>
              <select data-dim="${esc(id)}">${optionTags(dimensionFeedbackOptions, dimFeedback[id] || "unchecked")}</select>
            </div>`).join("")}
        </div>
        <p><label>证据是否支持评分</label><select id="evidenceFeedback" class="full">${optionTags(evidenceOptions, r.human_evidence_feedback || "unchecked")}</select></p>
        <p><label>综合分是否符合体感</label><select id="scoreFeedback" class="full">${optionTags(scoreOptions, r.human_score_feedback || "unchecked")}</select></p>
        <div>
          <label>主要错因（可多选）</label>
          <div class="reason-grid">
            ${errorReasons.map(([id, label]) => `<label class="check"><input type="checkbox" name="reason" value="${esc(id)}" ${errorValues.includes(id) ? "checked" : ""}>${esc(label)}</label>`).join("")}
          </div>
        </div>
        <p><label>整体反馈</label><textarea id="notes" placeholder="一句话说明为什么该进/不进，或评分哪里不符合体感。">${esc(r.human_notes)}</textarea></p>
        <div class="actions"><button class="primary" id="save">保存反馈</button></div>`;
      document.getElementById("decision").value = r.human_should_featured || "";
      document.getElementById("save").onclick = save;
    }
    async function save() {
      const r = rows.find(x => x.sample_key === activeKey);
      const dimFeedback = {};
      for (const select of feedback.querySelectorAll("[data-dim]")) {
        dimFeedback[select.dataset.dim] = select.value;
      }
      const selectedCats = Array.from(feedback.querySelectorAll('input[name="category"]:checked')).map(x => x.value);
      const selectedReasons = Array.from(feedback.querySelectorAll('input[name="reason"]:checked')).map(x => x.value);
      const payload = {
        updates: [{
          sample_key: activeKey,
          human_should_featured: document.getElementById("decision").value,
          human_categories_json: selectedCats,
          human_dimension_feedback_json: dimFeedback,
          human_evidence_feedback: document.getElementById("evidenceFeedback").value,
          human_score_feedback: document.getElementById("scoreFeedback").value,
          human_error_reasons_json: selectedReasons,
          human_notes: document.getElementById("notes").value
        }]
      };
      const res = await fetch("/api/label", {method:"POST", headers:{"Content-Type":"application/json"}, body: JSON.stringify(payload)});
      if (!res.ok) alert(await res.text());
      Object.assign(r, {
        human_should_featured: payload.updates[0].human_should_featured,
        human_categories_json: JSON.stringify(payload.updates[0].human_categories_json),
        human_dimension_feedback_json: JSON.stringify(payload.updates[0].human_dimension_feedback_json),
        human_evidence_feedback: payload.updates[0].human_evidence_feedback,
        human_score_feedback: payload.updates[0].human_score_feedback,
        human_error_reasons_json: JSON.stringify(payload.updates[0].human_error_reasons_json),
        human_notes: payload.updates[0].human_notes
      });
      renderProgress();
      renderList();
      show(activeKey);
    }
    async function load() {
      const [rowData, categoryData] = await Promise.all([
        fetch("/api/rows").then(r => r.json()),
        fetch("/api/classification").then(r => r.json())
      ]);
      rows = rowData;
      categories = categoryData.categories || [];
      activeKey = "";
      renderProgress();
      renderList();
    }
    filter.onchange = renderList;
    query.oninput = renderList;
    document.getElementById("reload").onclick = load;
    load();
  </script>
</body>
</html>""".encode("utf-8")


def run_review_server_mode(args: argparse.Namespace) -> int:
    from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer
    from urllib.parse import urlparse
    import webbrowser

    review_file = Path(args.review_file)

    class Handler(BaseHTTPRequestHandler):
        def _send_json(self, payload: Any, status: int = 200) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            path = urlparse(self.path).path
            if path == "/":
                body = _review_server_html()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if path == "/api/rows":
                self._send_json(_read_review_csv(review_file))
                return
            if path == "/api/classification":
                self._send_json({"categories": load_review_category_options()})
                return
            self._send_json({"error": "not_found"}, status=404)

        def do_POST(self) -> None:  # noqa: N802
            path = urlparse(self.path).path
            if path != "/api/label":
                self._send_json({"error": "not_found"}, status=404)
                return
            length = int(self.headers.get("Content-Length") or 0)
            try:
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
                updates = payload.get("updates") if isinstance(payload, dict) else []
                if not isinstance(updates, list):
                    raise ValueError("updates must be a list")
                result = apply_review_updates(review_file, updates)
            except Exception as exc:  # noqa: BLE001
                self._send_json({"error": str(exc)}, status=400)
                return
            self._send_json(result)

        def log_message(self, fmt: str, *args: Any) -> None:
            if args and not getattr(self.server, "quiet", False):
                print(f"[review-server] {fmt % args}", flush=True)

    server_cls = HTTPServer if args.once else ThreadingHTTPServer
    server = server_cls((args.host, args.port), Handler)
    server.quiet = args.quiet  # type: ignore[attr-defined]
    url = f"http://{args.host}:{server.server_port}/"
    print(f"[highlight-calibration] review server: {url}", flush=True)
    if args.open_browser:
        webbrowser.open(url)
    if args.once:
        server.handle_request()
        return 0
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("[highlight-calibration] review server stopped", flush=True)
    return 0


def run_rederive_mode(args: argparse.Namespace) -> int:
    rows = _read_jsonl(Path(args.classification_file))
    if not rows:
        print(f"[highlight-calibration] no classification rows: {args.classification_file}", flush=True)
        return 0
    outputs = rederive_classification_rows(rows)
    out = Path(args.output_classification_file)
    _write_jsonl(out, outputs, append=False)
    print(f"[highlight-calibration] rederived rows={len(outputs)} file={out}", flush=True)
    return 0


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--snapshot-file", default=str(DEFAULT_SNAPSHOT_FILE))
    common.add_argument("--classification-file", default=str(DEFAULT_CLASSIFICATION_FILE))
    common.add_argument("--analysis-file", default=str(DEFAULT_ANALYSIS_FILE))
    common.add_argument("--review-file", default=str(DEFAULT_REVIEW_FILE))
    common.add_argument("--calibration-file", default=str(DEFAULT_CALIBRATION_FILE))
    common.add_argument("--env-file", default="", help="optional .env file or project dir; values stay out of logs")
    common.add_argument("--duplicate-count", type=int, default=20)
    common.add_argument("--final-threshold", type=float, default=None)
    common.add_argument("--append-output", action="store_true")

    parser = argparse.ArgumentParser(description="Offline cluster highlight scoring calibration")
    subparsers = parser.add_subparsers(dest="mode", required=True)

    snapshot = subparsers.add_parser("snapshot", parents=[common])
    snapshot.add_argument("--days", type=int, default=14)
    snapshot.add_argument("--sample-limit", type=int, default=100)
    snapshot.add_argument("--scan-limit", type=int, default=500)
    snapshot.add_argument("--db-statement-timeout-sec", type=int, default=8)

    classify = subparsers.add_parser("classify-offline", parents=[common])
    classify.add_argument("--offline-limit", type=int, default=0)
    classify.add_argument("--resume-classification", action="store_true")
    classify.add_argument("--retry-errors", action="store_true", help="with resume, rerun rows whose previous output had error")
    classify.add_argument("--request-interval-sec", type=float, default=2.0)
    classify.add_argument("--max-tokens", type=int, default=1600)
    classify.add_argument("--temperature", type=float, default=0.0)
    classify.add_argument("--classification-concurrency", type=int, default=1)

    analyze = subparsers.add_parser("analyze", parents=[common])
    analyze.add_argument("--score-jump-threshold", type=float, default=10.0)
    analyze.add_argument("--min-labeled", type=int, default=30)
    analyze.add_argument("--min-positive", type=int, default=10)
    analyze.add_argument("--min-negative", type=int, default=10)
    analyze.add_argument("--min-type-labeled", type=int, default=8)

    calibrate = subparsers.add_parser("calibrate", parents=[common])
    calibrate.add_argument("--min-labeled", type=int, default=30)
    calibrate.add_argument("--min-positive", type=int, default=10)
    calibrate.add_argument("--min-negative", type=int, default=10)
    calibrate.add_argument("--min-type-labeled", type=int, default=8)

    review = subparsers.add_parser("export-review", parents=[common])
    review.add_argument("--pairwise-count", type=int, default=20)
    review.add_argument("--include-duplicates", action="store_true")
    review.add_argument("--app-base-url", default=os.environ.get("APP_BASE_URL", DEFAULT_APP_BASE_URL))

    rederive = subparsers.add_parser("rederive", parents=[common])
    rederive.add_argument("--output-classification-file", default=str(DEFAULT_REDERIVED_FILE))

    review_server = subparsers.add_parser("review-server", parents=[common])
    review_server.add_argument("--host", default="127.0.0.1")
    review_server.add_argument("--port", type=int, default=8765)
    review_server.add_argument("--open-browser", action="store_true")
    review_server.add_argument("--quiet", action="store_true")
    review_server.add_argument("--once", action="store_true", help="handle one HTTP request then exit; useful for smoke tests")
    return parser


def normalize_args(args: argparse.Namespace) -> argparse.Namespace:
    args.duplicate_count = max(0, min(int(getattr(args, "duplicate_count", 20)), 40))
    args.final_threshold = getattr(args, "final_threshold", None)
    if getattr(args, "mode", "") == "snapshot":
        args.days = max(1, min(int(args.days), 30))
        args.sample_limit = max(1, min(int(args.sample_limit), 200))
        args.scan_limit = max(args.sample_limit, min(int(args.scan_limit), 5000))
        args.db_statement_timeout_sec = max(5, min(int(args.db_statement_timeout_sec), 60))
    if getattr(args, "mode", "") == "classify-offline":
        args.offline_limit = max(0, min(int(args.offline_limit), 20000))
        args.request_interval_sec = max(0.8, min(float(args.request_interval_sec), 30.0))
        args.max_tokens = max(400, min(int(args.max_tokens), 3000))
        args.temperature = max(0.0, min(float(args.temperature), 1.0))
        args.classification_concurrency = max(1, min(int(args.classification_concurrency), 20))
    if getattr(args, "mode", "") == "analyze":
        args.score_jump_threshold = max(1.0, min(float(args.score_jump_threshold), 50.0))
    if getattr(args, "mode", "") in {"analyze", "calibrate"}:
        args.min_labeled = max(1, min(int(getattr(args, "min_labeled", 30)), 10000))
        args.min_positive = max(1, min(int(getattr(args, "min_positive", 10)), 10000))
        args.min_negative = max(1, min(int(getattr(args, "min_negative", 10)), 10000))
        args.min_type_labeled = max(1, min(int(getattr(args, "min_type_labeled", 8)), 10000))
    if getattr(args, "mode", "") == "export-review":
        args.pairwise_count = max(0, min(int(args.pairwise_count), 200))
    if getattr(args, "mode", "") == "review-server":
        args.port = max(0, min(int(args.port), 65535))
    return args


def main() -> int:
    args = normalize_args(build_parser().parse_args())
    loaded_env = load_env_file_into_process(getattr(args, "env_file", ""))
    if loaded_env:
        print(f"[highlight-calibration] env-file loaded keys={len(loaded_env)}", flush=True)
    if args.mode == "snapshot":
        return run_snapshot_mode(args)
    if args.mode == "classify-offline":
        return run_classify_offline_mode(args)
    if args.mode == "analyze":
        return run_analyze_mode(args)
    if args.mode == "calibrate":
        return run_calibrate_mode(args)
    if args.mode == "export-review":
        return run_export_review_mode(args)
    if args.mode == "rederive":
        return run_rederive_mode(args)
    if args.mode == "review-server":
        return run_review_server_mode(args)
    raise ValueError(f"unsupported mode: {args.mode}")


if __name__ == "__main__":
    raise SystemExit(main())
