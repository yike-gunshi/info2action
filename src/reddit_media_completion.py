"""Best-effort Reddit media completion for items already visible in Highlights."""
from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

import remote_db
from reddit_media import complete_reddit_media


_SCHEMA_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_DEFAULT_LIMIT = 20
_MAX_LIMIT = 25


def select_candidates(conn: Any, *, schema: str, limit: int = _DEFAULT_LIMIT) -> list[dict]:
    """Return visible Reddit items whose active Highlights row has no media."""
    if not _SCHEMA_RE.fullmatch(schema):
        raise ValueError("invalid database schema")
    bounded_limit = max(1, min(int(limit), _MAX_LIMIT))
    rows = conn.execute(
        f"""SELECT DISTINCT i.id AS item_id, i.url AS source_url
              FROM {schema}.highlights_read_model_state st
              JOIN {schema}.highlights_scope_items h
                ON h.version_id = st.active_version_id
               AND h.scope_key = 'all'
              JOIN {schema}.cluster_items ci ON ci.cluster_id = h.cluster_id
              JOIN {schema}.items i ON i.id = ci.item_id
             WHERE st.key = %(state_key)s
               AND i.platform = 'reddit'
               AND i.url IS NOT NULL
               AND i.url <> ''
               AND (
                    i.media_json IS NULL
                    OR i.media_json = '[]'::jsonb
                    OR i.media_json = 'null'::jsonb
               )
             ORDER BY i.id
             LIMIT %(limit)s""",
        {
            "state_key": remote_db.HIGHLIGHTS_READ_MODEL_STATE_KEY,
            "limit": bounded_limit,
        },
    ).fetchall()
    return [dict(row) for row in rows]


def _load_candidates(limit: int) -> list[dict]:
    with remote_db.connect() as conn:
        return select_candidates(
            conn,
            schema=remote_db.remote_schema(),
            limit=limit,
        )


def _update_item(item_id: str, media: list[dict]) -> None:
    updates: dict[str, Any] = {"media_json": media}
    poster_url = next(
        (
            str(entry.get("poster_url") or "").strip()
            for entry in media
            if isinstance(entry, dict) and entry.get("poster_url")
        ),
        "",
    )
    if poster_url:
        updates["cover_url"] = poster_url
    remote_db.update_item_light_fields_remote(None, item_id, updates)


def _run_asr(item_ids: list[str]) -> None:
    import ingest

    ingest._run_asr_for_video_items_inline(None, item_ids)


def complete_highlighted_reddit_media(
    *,
    limit: int = _DEFAULT_LIMIT,
    candidate_loader: Callable[[int], list[dict]] = _load_candidates,
    media_loader: Callable[[str], list[dict]] = complete_reddit_media,
    item_updater: Callable[[str, list[dict]], None] = _update_item,
    asr_runner: Callable[[list[str]], None] = _run_asr,
) -> dict[str, int]:
    """Complete a bounded batch without letting one extractor failure stop peers."""
    bounded_limit = max(1, min(int(limit), _MAX_LIMIT))
    candidates = list(candidate_loader(bounded_limit))[:bounded_limit]
    result = {
        "candidates": len(candidates),
        "completed": 0,
        "no_media": 0,
        "failed": 0,
        "asr_triggered": 0,
    }
    for candidate in candidates:
        item_id = str(candidate.get("item_id") or "").strip()
        source_url = str(candidate.get("source_url") or "").strip()
        try:
            media = media_loader(source_url)
            if not media:
                result["no_media"] += 1
                continue
            item_updater(item_id, media)
            result["completed"] += 1
            if any(
                isinstance(entry, dict) and entry.get("type") == "video"
                for entry in media
            ):
                asr_runner([item_id])
                result["asr_triggered"] += 1
        except Exception as exc:
            result["failed"] += 1
            print(
                f"[reddit-media] completion failed item_id={item_id!r}: {exc!r}",
                flush=True,
            )
    return result
