"""Build immutable/fresh daily-digest snapshots from visible event clusters."""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from zoneinfo import ZoneInfo


# Existing src modules use top-level imports. Support both direct and
# ``python -m src.daily_digest`` execution without changing that convention.
SRC_DIR = Path(__file__).resolve().parent
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

import enrich_items  # noqa: E402
import highlight_score_v26  # noqa: E402
import remote_db  # noqa: E402
from category_taxonomy import ACTIVE_CATEGORY_IDS  # noqa: E402
from prompt_loader import load_prompt  # noqa: E402


logger = logging.getLogger(__name__)
SHANGHAI = ZoneInfo("Asia/Shanghai")
MAX_ITEMS = 10
PROMPT_FILE = "16_daily_digest_editor_v2.md"


def _prompt_version(filename: str) -> str:
    prompt_path = SRC_DIR.parent / "prompts" / filename
    for line in prompt_path.read_text(encoding="utf-8").splitlines()[:10]:
        stripped = line.strip()
        if stripped.startswith("> 版本：`") and stripped.endswith("`"):
            return stripped.removeprefix("> 版本：`")[:-1]
    raise RuntimeError(f"missing version header in prompt: {filename}")


PROMPT_VERSION = _prompt_version(PROMPT_FILE)


def _as_dict(row: Any) -> dict[str, Any]:
    if isinstance(row, dict):
        return dict(row)
    try:
        return dict(row)
    except (TypeError, ValueError):
        return {}


def _json_value(value: Any, default: Any) -> Any:
    if value is None:
        return default
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return default
    return value


def _json_default(value: Any) -> float:
    if isinstance(value, Decimal):
        return float(value)
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=_json_default)


def _id_key(value: Any) -> str:
    return str(value)


def _raise_with_migration_hint(exc: Exception) -> None:
    text = str(exc).lower()
    if "daily_digest" in text and any(
        marker in text
        for marker in ("does not exist", "undefinedtable", "undefined table", "42p01")
    ):
        raise RuntimeError("daily_digest 表不存在，请先跑迁移 0038") from exc
    raise exc


def _normalize_links(value: Any) -> list[dict[str, str]]:
    normalized: list[dict[str, str]] = []
    for link in _normalize_candidate_links(value):
        url = link["url"]
        label = link["author"] or link["platform"]
        normalized.append({"url": url, "label": label or (urlparse(url).hostname or url)})
    return normalized


def _normalize_candidate_links(value: Any) -> list[dict[str, str]]:
    links = _json_value(value, [])
    if not isinstance(links, list):
        return []
    normalized: list[dict[str, str]] = []
    seen: set[str] = set()
    for link in links:
        if not isinstance(link, dict):
            continue
        url = str(link.get("url") or "").strip()
        if not url or url in seen:
            continue
        seen.add(url)
        normalized.append({
            "author": str(link.get("author") or "").strip(),
            "platform": str(link.get("platform") or "").strip(),
            "url": url,
        })
        if len(normalized) == 3:
            break
    return normalized


def _summary(value: Any) -> str | None:
    if value is None or value == "":
        return None
    text = str(value)
    return text if len(text) <= 400 else text[:400] + "…"


def _key_points(value: Any) -> list[Any] | None:
    points = _json_value(value, None)
    return points[:5] if isinstance(points, list) else None


def _normalize_candidate(row: Any) -> dict[str, Any]:
    candidate = _as_dict(row)
    source_count = int(candidate.get("unique_source_count") or 0)
    candidate["unique_source_count"] = source_count
    candidate["single_source"] = bool(candidate.get("single_source", source_count < 2))
    candidate["links"] = _normalize_candidate_links(candidate.get("links"))
    return candidate


def fetch_candidates(target_date: date) -> list[dict[str, Any]]:
    """Return Top-30 candidates using the shared production display gate."""
    schema = remote_db.remote_schema()
    threshold = remote_db._highlights_display_threshold()
    display_condition = remote_db._highlights_display_cluster_condition(
        schema, "c", threshold=threshold
    )
    sql = f"""
        SELECT c.id AS cluster_id,
               c.ai_title,
               c.why_read,
               c.ai_summary,
               c.ai_key_points,
               COALESCE(c.unique_source_count, 0) AS unique_source_count,
               hcd.highlight_score,
               (COALESCE(c.unique_source_count, 0) < 2) AS single_source,
               COALESCE(link_data.links, '[]'::jsonb) AS links
          FROM {schema}.clusters c
          LEFT JOIN {schema}.highlight_cluster_decisions hcd
            ON hcd.cluster_id = c.id
          LEFT JOIN LATERAL (
            SELECT jsonb_agg(
                     jsonb_build_object(
                       'author', member.author_name,
                       'platform', member.platform,
                       'url', member.url
                     )
                     ORDER BY member.is_primary_source DESC,
                              member.rank_in_cluster ASC NULLS LAST,
                              member.item_time DESC NULLS LAST
                   ) AS links
              FROM (
                SELECT i.author_name,
                       i.url,
                       i.platform,
                       COALESCE(ci.is_primary_source, false) AS is_primary_source,
                       ci.rank_in_cluster,
                       COALESCE(i.published_at, i.fetched_at) AS item_time
                  FROM {schema}.cluster_items ci
                  JOIN {schema}.items i ON i.id = ci.item_id
                 WHERE ci.cluster_id = c.id
                   AND NULLIF(i.url, '') IS NOT NULL
                 ORDER BY COALESCE(ci.is_primary_source, false) DESC,
                          ci.rank_in_cluster ASC NULLS LAST,
                          COALESCE(i.published_at, i.fetched_at) DESC NULLS LAST,
                          i.id DESC
                 LIMIT 3
              ) member
          ) link_data ON true
         WHERE c.is_visible_in_feed = true
           AND c.published_at IS NOT NULL
           AND COALESCE(c.archived, false) = false
           AND c.merged_into IS NULL
           AND {display_condition}
           AND (
             COALESCE(c.unique_source_count, 0) >= 2
             OR (hcd.score_inputs->>'max_flag_score10')::float >= 9.0
             OR hcd.manual_display = 'force_show'
           )
           AND (COALESCE(c.first_doc_at, c.last_doc_at, c.last_updated_at)
                AT TIME ZONE 'Asia/Shanghai')::date = %s
           AND NOT EXISTS (
             SELECT 1
               FROM {schema}.cluster_items ci_private
               JOIN {schema}.items i_private ON i_private.id = ci_private.item_id
              WHERE ci_private.cluster_id = c.id
                AND (i_private.platform = 'manual' OR i_private.user_id IS NOT NULL)
           )
         ORDER BY (hcd.manual_display = 'force_show') DESC,
                  hcd.highlight_score DESC NULLS LAST, c.id DESC
         LIMIT 30
    """
    try:
        with remote_db.connect() as conn:
            rows = conn.execute(sql, (target_date,)).fetchall()
    except Exception as exc:
        _raise_with_migration_hint(exc)
    return [_normalize_candidate(row) for row in rows]


def fetch_eligible_previous_cluster_ids(target_date: date, cluster_ids: list[Any]) -> set[str]:
    """Return previous cluster IDs that still satisfy the candidate-pool gate."""
    if not cluster_ids:
        return set()
    schema = remote_db.remote_schema()
    threshold = remote_db._highlights_display_threshold()
    display_condition = remote_db._highlights_display_cluster_condition(
        schema, "c", threshold=threshold
    )
    sql = f"""
        SELECT c.id AS cluster_id
          FROM {schema}.clusters c
          LEFT JOIN {schema}.highlight_cluster_decisions hcd
            ON hcd.cluster_id = c.id
         WHERE c.id = ANY(%s)
           AND c.is_visible_in_feed = true
           AND c.published_at IS NOT NULL
           AND COALESCE(c.archived, false) = false
           AND c.merged_into IS NULL
           AND {display_condition}
           AND (
             COALESCE(c.unique_source_count, 0) >= 2
             OR (hcd.score_inputs->>'max_flag_score10')::float >= 9.0
             OR hcd.manual_display = 'force_show'
           )
           AND (COALESCE(c.first_doc_at, c.last_doc_at, c.last_updated_at)
                AT TIME ZONE 'Asia/Shanghai')::date = %s
           AND NOT EXISTS (
             SELECT 1
               FROM {schema}.cluster_items ci_private
               JOIN {schema}.items i_private ON i_private.id = ci_private.item_id
              WHERE ci_private.cluster_id = c.id
                AND (i_private.platform = 'manual' OR i_private.user_id IS NOT NULL)
           )
    """
    try:
        with remote_db.connect() as conn:
            rows = conn.execute(sql, (cluster_ids, target_date)).fetchall()
    except Exception as exc:
        _raise_with_migration_hint(exc)
    return {
        _id_key(_as_dict(row).get("cluster_id"))
        for row in rows
        if _as_dict(row).get("cluster_id") is not None
    }


def load_digest(target_date: date) -> dict[str, Any] | None:
    schema = remote_db.remote_schema()
    try:
        with remote_db.connect() as conn:
            row = conn.execute(
                f"""SELECT digest_date, status, entries, candidate_ids, source,
                           prompt_version, model, generated_at, updated_at
                      FROM {schema}.daily_digest
                     WHERE digest_date = %s""",
                (target_date,),
            ).fetchone()
    except Exception as exc:
        _raise_with_migration_hint(exc)
    if not row:
        return None
    digest = _as_dict(row)
    digest["entries"] = _json_value(digest.get("entries"), [])
    digest["candidate_ids"] = _json_value(digest.get("candidate_ids"), [])
    return digest


def save_digest(
    *,
    target_date: date,
    status: str,
    entries: list[dict[str, Any]],
    candidate_ids: list[Any],
    source: str,
    prompt_version: str = PROMPT_VERSION,
    model: str | None = None,
) -> None:
    schema = remote_db.remote_schema()
    try:
        with remote_db.connect() as conn:
            conn.execute(
                f"""INSERT INTO {schema}.daily_digest AS existing_digest
                       (digest_date, status, entries, candidate_ids, source,
                        prompt_version, model, generated_at, updated_at)
                     VALUES (%s, %s, %s::jsonb, %s::jsonb, %s, %s, %s, now(), now())
                     ON CONFLICT (digest_date) DO UPDATE
                       SET status = EXCLUDED.status,
                           entries = EXCLUDED.entries,
                           candidate_ids = EXCLUDED.candidate_ids,
                           source = EXCLUDED.source,
                           prompt_version = EXCLUDED.prompt_version,
                           model = EXCLUDED.model,
                           generated_at = EXCLUDED.generated_at,
                           updated_at = now()
                     WHERE existing_digest.status <> 'final'""",
                (
                    target_date,
                    status,
                    _json_dumps(entries),
                    _json_dumps(candidate_ids),
                    source,
                    prompt_version,
                    model,
                ),
            )
    except Exception as exc:
        _raise_with_migration_hint(exc)


def _digest_categories(conn: Any, schema: str, cluster_ids: list[int]) -> dict[int, str | None]:
    """Resolve current primary categories without rewriting editorial snapshots.

    The active projection wins, including a known null category. Historical
    IDs outside that projection use the same voting rules, with no time window
    or display gate and no redirection to a merged cluster.
    """
    if not cluster_ids:
        return {}
    category_sql = remote_db._highlights_category_sql("i")
    category_priority = remote_db._highlights_category_priority_sql("category")
    active_categories = [category for category in ACTIVE_CATEGORY_IDS if category != "other"]
    rows = conn.execute(
        f"""WITH projected AS (
                SELECT h.cluster_id, h.card_json->>'category' AS category
                  FROM {schema}.highlights_read_model_state st
                  JOIN {schema}.highlights_read_model_versions v
                    ON v.version_id = st.active_version_id AND v.status = 'complete'
                  JOIN {schema}.highlights_scope_items h
                    ON h.version_id = v.version_id AND h.scope_key = 'all'
                 WHERE st.key = %(state_key)s
                   AND h.cluster_id = ANY(%(cluster_ids)s)
            ), member_categories AS (
                SELECT ci.cluster_id, {category_sql} AS category
                  FROM {schema}.cluster_items ci
                  JOIN {schema}.items i ON i.id = ci.item_id
                 WHERE ci.cluster_id = ANY(%(cluster_ids)s)
                   AND NOT EXISTS (SELECT 1 FROM projected p WHERE p.cluster_id = ci.cluster_id)
            ), category_counts AS (
                SELECT cluster_id, category, count(*) AS n
                  FROM member_categories
                 WHERE category = ANY(%(active_categories)s::text[])
                 GROUP BY cluster_id, category
            ), ranked AS (
                SELECT cluster_id, category,
                       row_number() OVER (
                           PARTITION BY cluster_id
                           ORDER BY n DESC, {category_priority}, category ASC
                       ) AS rn
                  FROM category_counts
            )
            SELECT cluster_id, category FROM projected
            UNION ALL
            SELECT cluster_id, category FROM ranked WHERE rn = 1""",
        {
            "state_key": remote_db.HIGHLIGHTS_READ_MODEL_STATE_KEY,
            "cluster_ids": cluster_ids,
            "active_categories": active_categories,
        },
    ).fetchall()
    return {
        int(row["cluster_id"]): row["category"] if row["category"] in active_categories else None
        for row in rows
    }


def list_digests(start: date, end: date) -> list[dict[str, Any]]:
    schema = remote_db.remote_schema()
    try:
        with remote_db.connect() as conn:
            rows = conn.execute(
                f"""SELECT digest_date, status, entries, updated_at
                      FROM {schema}.daily_digest
                     WHERE digest_date BETWEEN %s AND %s
                     ORDER BY digest_date DESC""",
                (start, end),
            ).fetchall()
            digests = []
            for row in rows:
                data = _as_dict(row)
                digest_date = data.get("digest_date")
                updated_at = data.get("updated_at")
                digests.append({
                    "date": digest_date.isoformat() if hasattr(digest_date, "isoformat") else str(digest_date),
                    "status": data.get("status"),
                    "entries": _json_value(data.get("entries"), []),
                    "updated_at": updated_at.isoformat() if hasattr(updated_at, "isoformat") else str(updated_at),
                })
            cluster_ids = list(dict.fromkeys(
                int(entry["cluster_id"])
                for digest in digests for entry in digest["entries"]
            ))
            categories = _digest_categories(conn, schema, cluster_ids)
            for digest in digests:
                digest["entries"] = [
                    {**entry, "category": categories.get(int(entry["cluster_id"]))}
                    for entry in digest["entries"]
                ]
    except Exception as exc:
        _raise_with_migration_hint(exc)
    return digests


def _editor_payload(
    target_date: date,
    candidates: list[dict[str, Any]],
    current_entries: list[dict[str, Any]],
    *,
    is_final_pass: bool,
) -> dict[str, Any]:
    return {
        "target_date": target_date.isoformat(),
        "is_final_pass": is_final_pass,
        "max_items": MAX_ITEMS,
        "current_entries": [
            {"cluster_id": entry.get("cluster_id"), "title": entry.get("title")}
            for entry in current_entries
        ],
        "candidates": [
            {
                "cluster_id": candidate.get("cluster_id"),
                "title": str(candidate.get("ai_title") or "")[:40],
                "why_read": str(candidate.get("why_read") or "")[:200],
                "summary": _summary(candidate.get("ai_summary")),
                "key_points": _key_points(candidate.get("ai_key_points")),
                "links": _normalize_candidate_links(candidate.get("links")),
                "source_count": float(candidate.get("unique_source_count") or 0),
                "highlight_score": (
                    float(candidate["highlight_score"])
                    if candidate.get("highlight_score") is not None
                    else None
                ),
                "single_source": bool(candidate.get("single_source")),
            }
            for candidate in candidates
        ],
    }


def call_editor(
    target_date: date,
    candidates: list[dict[str, Any]],
    current_entries: list[dict[str, Any]],
    *,
    is_final_pass: bool,
) -> dict[str, Any]:
    ai_config = enrich_items.load_config().get("ai_summary", {})
    api_key, api_base, model = enrich_items.resolve_minimax_runtime_config(ai_config)
    if not api_key:
        raise RuntimeError("No MiniMax API key configured")
    payload = _editor_payload(target_date, candidates, current_entries, is_final_pass=is_final_pass)
    system_prompt = load_prompt(
        PROMPT_FILE,
        payload_json=_json_dumps(payload),
        max_items=MAX_ITEMS,
    )
    if not system_prompt:
        raise RuntimeError(f"missing prompt: {PROMPT_FILE}")
    raw = enrich_items.call_minimax(
        api_key,
        api_base,
        model,
        system_prompt,
        "按照系统提示完成每日要点选稿。",
        max_tokens=8192,
        temperature=0.0,
    )
    try:
        parsed = json.loads(highlight_score_v26._json_text(raw))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError(f"daily digest editor returned invalid JSON: {exc}") from exc
    if not isinstance(parsed, dict) or not isinstance(parsed.get("selected"), list):
        raise ValueError("daily digest editor response missing selected array")
    parsed["_model"] = model
    return parsed


def _valid_selected(
    candidates_by_id: dict[str, dict[str, Any]], selected: Any
) -> list[dict[str, Any]]:
    if not isinstance(selected, list):
        return []
    valid = []
    used: set[str] = set()
    for item in selected:
        if not isinstance(item, dict) or item.get("cluster_id") is None:
            continue
        key = _id_key(item["cluster_id"])
        if key not in candidates_by_id or key in used:
            continue
        used.add(key)
        valid.append({"cluster_id": key})
    return valid


def _entry_from_spec(
    spec: dict[str, Any], candidates_by_id: dict[str, dict[str, Any]], rank: int
) -> dict[str, Any]:
    candidate = candidates_by_id[spec["cluster_id"]]
    return {
        "rank": rank,
        "cluster_id": candidate.get("cluster_id"),
        "title": candidate.get("ai_title"),
        "source_count": int(candidate.get("unique_source_count") or 0),
        "links": _normalize_links(candidate.get("links")),
    }


def validate_and_build_entries(
    candidates: list[dict[str, Any]],
    selected: Any,
    *,
    previous_entries: list[dict[str, Any]] | None = None,
    eligible_previous_ids: set[str] | None = None,
    rolling: bool,
    max_items: int = MAX_ITEMS,
) -> list[dict[str, Any]]:
    candidates_by_id = {
        _id_key(candidate.get("cluster_id")): candidate
        for candidate in map(_normalize_candidate, candidates)
        if candidate.get("cluster_id") is not None
    }
    specs = _valid_selected(candidates_by_id, selected)[:max_items]
    previous_by_id = {
        _id_key(entry.get("cluster_id")): entry
        for entry in previous_entries or []
        if entry.get("cluster_id") is not None
    }
    if rolling:
        previous_ids = []
        for entry in previous_entries or []:
            key = _id_key(entry.get("cluster_id"))
            if (
                key in candidates_by_id or key in (eligible_previous_ids or set())
            ) and key not in previous_ids:
                previous_ids.append(key)
        protected = set(previous_ids)
        represented = {spec["cluster_id"] for spec in specs}
        specs.extend(
            {"cluster_id": cluster_id}
            for cluster_id in previous_ids
            if cluster_id not in represented
        )
        while len(specs) > max_items:
            drop_index = next((
                index
                for index in range(len(specs) - 1, -1, -1)
                if specs[index]["cluster_id"] not in protected
            ), None)
            if drop_index is None:
                specs = specs[:max_items]
                break
            specs.pop(drop_index)
    entries = []
    for rank, spec in enumerate(specs[:max_items], start=1):
        cluster_id = spec["cluster_id"]
        if cluster_id in candidates_by_id:
            entries.append(_entry_from_spec(spec, candidates_by_id, rank))
        else:
            entries.append({**previous_by_id[cluster_id], "rank": rank})
    return entries


def generate_for_date(target_date: date, *, status: str) -> dict[str, Any]:
    if status not in {"rolling", "final"}:
        raise ValueError(f"invalid digest status: {status}")
    existing = load_digest(target_date)
    if existing and existing.get("status") == "final":
        logger.info("daily_digest skip date=%s reason=final_exists", target_date)
        return {"date": target_date.isoformat(), "status": status, "outcome": "skipped_final_exists"}

    candidates = fetch_candidates(target_date)
    if not candidates:
        if existing:
            save_digest(
                target_date=target_date,
                status=status,
                entries=[],
                candidate_ids=[],
                source=existing.get("source") or "editor",
                model=None,
            )
            logger.info("daily_digest save empty date=%s status=%s", target_date, status)
            return {
                "date": target_date.isoformat(),
                "status": status,
                "outcome": "saved_empty",
                "entries": 0,
            }
        logger.info("daily_digest skip date=%s reason=no_candidates", target_date)
        return {"date": target_date.isoformat(), "status": status, "outcome": "skipped_no_candidates"}

    candidate_ids = [candidate.get("cluster_id") for candidate in candidates]
    current_entries = list((existing or {}).get("entries") or [])
    candidate_keys = {_id_key(cluster_id) for cluster_id in candidate_ids}
    previous_outside_top30 = list(dict.fromkeys(
        entry.get("cluster_id")
        for entry in current_entries
        if entry.get("cluster_id") is not None
        and _id_key(entry.get("cluster_id")) not in candidate_keys
    ))
    eligible_previous_ids = (
        fetch_eligible_previous_cluster_ids(target_date, previous_outside_top30)
        if status == "rolling" and previous_outside_top30
        else set()
    )
    has_ineligible_previous = any(
        _id_key(cluster_id) not in eligible_previous_ids
        for cluster_id in previous_outside_top30
    )
    if (
        status == "rolling"
        and existing
        and existing.get("source") == "editor"
        and existing.get("candidate_ids") == candidate_ids
        and not has_ineligible_previous
    ):
        logger.info("daily_digest skip date=%s reason=unchanged_candidates", target_date)
        return {
            "date": target_date.isoformat(),
            "status": status,
            "outcome": "skipped_unchanged_candidates",
        }

    try:
        editor_output = call_editor(
            target_date,
            candidates,
            current_entries,
            is_final_pass=status == "final",
        )
    except Exception as exc:
        logger.exception("daily_digest editor failed date=%s status=%s: %s", target_date, status, exc)
        if existing:
            return {
                "date": target_date.isoformat(),
                "status": status,
                "outcome": "llm_failed_kept_existing",
            }
        entries = validate_and_build_entries(
            candidates,
            [{"cluster_id": c.get("cluster_id")} for c in candidates[:MAX_ITEMS]],
            rolling=False,
        )
        save_digest(
            target_date=target_date,
            status=status,
            entries=entries,
            candidate_ids=candidate_ids,
            source="rules_fallback",
            model=None,
        )
        return {
            "date": target_date.isoformat(),
            "status": status,
            "outcome": "saved_rules_fallback",
            "entries": len(entries),
        }

    entries = validate_and_build_entries(
        candidates,
        editor_output.get("selected"),
        previous_entries=current_entries,
        eligible_previous_ids=eligible_previous_ids,
        rolling=status == "rolling",
    )
    save_digest(
        target_date=target_date,
        status=status,
        entries=entries,
        candidate_ids=candidate_ids,
        source="editor",
        model=editor_output.get("_model"),
    )
    return {
        "date": target_date.isoformat(),
        "status": status,
        "outcome": "saved_editor",
        "entries": len(entries),
    }


def _shanghai_now(now: datetime | None = None) -> datetime:
    current = now or datetime.now(SHANGHAI)
    return current.replace(tzinfo=SHANGHAI) if current.tzinfo is None else current.astimezone(SHANGHAI)


def run_auto(*, now: datetime | None = None) -> list[dict[str, Any]]:
    current = _shanghai_now(now)
    results = []
    if current.hour >= 2:
        results.append(generate_for_date(current.date() - timedelta(days=1), status="final"))
    results.append(generate_for_date(current.date(), status="rolling"))
    return results


def run_backfill(days: int, *, now: datetime | None = None) -> list[dict[str, Any]]:
    if days < 1:
        raise ValueError("days must be at least 1")
    today = _shanghai_now(now).date()
    return [
        generate_for_date(today - timedelta(days=offset), status="final")
        for offset in range(days, 0, -1)
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate daily digest snapshots")
    parser.add_argument("--mode", choices=("auto", "backfill"), default="auto")
    parser.add_argument("--days", type=int, default=3)
    args = parser.parse_args(argv)
    results = run_auto() if args.mode == "auto" else run_backfill(args.days)
    print(_json_dumps({"mode": args.mode, "results": results}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
