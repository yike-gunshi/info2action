"""Admin sources registry endpoints for subscription configuration."""
import json
import os
import re
import subprocess
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

import feedparser
import requests
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

import db
import fetch_lingowhale
import fetch_x_users
import remote_db
import x_list_registry
from authz import require_admin
from deps import BASE

router = APIRouter()

_X_HANDLE_RE = re.compile(r"^[A-Za-z0-9_]{1,15}$")
_SUBREDDIT_RE = re.compile(r"^[A-Za-z0-9_]{3,21}$")
_GITHUB_REPO_RE = re.compile(r"^[\w.-]+/[\w.-]+$", re.ASCII)
_BILI_UID_RE = re.compile(r"^[0-9]{1,32}$")
_WECHAT_CHANNEL_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")

_SUPPORTED_PLATFORMS = {
    "wechat_mp",
    "x_user",
    "rss",
    "reddit",
    "github_repo",
    "bilibili_up",
}
_CREATE_STATUSES = {"active", "paused", "pending", "broken", "not_fetched"}
_PATCH_STATUSES = {"active", "paused"}

_HTTP_HEADERS = {"User-Agent": "info2action/1.0 (+https://github.com)"}
_X_VALIDATION_DEFERRED_REASON = (
    "X validation requires the local twitter CLI session, which is unavailable in this environment."
)
_X_VALIDATION_WARNING = "校验超时，可先入库"

_ALGO_PARAM_SPECS = {
    "hackernews_count": ("hackernews", "count", 1, 500),
    "github_trending_count": ("github_trending", "count", 1, 500),
    "bilibili_hot_count": ("bilibili", "hot_count", 1, 500),
    "bilibili_rank_count": ("bilibili", "rank_count", 1, 500),
    "bilibili_videos_per_up": ("bilibili", "videos_per_up", 1, 100),
}


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _json_error(message, status_code=400):
    return JSONResponse({"error": message}, status_code=status_code)


async def _optional_json_body(request):
    try:
        body = await request.json()
    except Exception:
        return {}
    return body if isinstance(body, dict) else {}


async def _json_body(request):
    try:
        body = await request.json()
    except Exception:
        return None, _json_error("Invalid JSON body")
    if not isinstance(body, dict):
        return None, _json_error("JSON body must be an object")
    return body, None


def _parse_config_json(value):
    if value in (None, ""):
        return None
    if isinstance(value, (dict, list, int, float, bool)):
        return value
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (TypeError, ValueError):
            return value
    return value


def _serialize_config_json(value):
    if value is None:
        return None
    if isinstance(value, str):
        try:
            json.loads(value)
        except (TypeError, ValueError):
            return json.dumps(value, ensure_ascii=False)
        return value
    return json.dumps(value, ensure_ascii=False)


def _is_http_feed_url(value):
    parsed = urlparse(value)
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


def _wechat_backend(source_key):
    return "rss" if _is_http_feed_url(source_key) else "lingowhale"


def _config_with_backend(value, backend):
    parsed = _parse_config_json(value)
    if not isinstance(parsed, dict):
        parsed = {}
    parsed["backend"] = backend
    return parsed


def _source_key_error(platform, source_key):
    if platform not in _SUPPORTED_PLATFORMS:
        return "platform is not supported"
    if not isinstance(source_key, str) or not source_key.strip():
        return "source_key is required"
    source_key = source_key.strip()
    if platform == "x_user" and not _X_HANDLE_RE.fullmatch(source_key):
        return "source_key must be a valid X handle"
    if platform == "reddit" and not _SUBREDDIT_RE.fullmatch(source_key):
        return "source_key must be a valid subreddit name"
    if platform == "github_repo" and not _GITHUB_REPO_RE.fullmatch(source_key):
        return "source_key must be a valid github owner/repo"
    if platform == "rss":
        if not _is_http_feed_url(source_key):
            return "source_key must be a valid http(s) feed URL"
    if platform == "wechat_mp":
        if _is_http_feed_url(source_key):
            return None
        if not _WECHAT_CHANNEL_ID_RE.fullmatch(source_key):
            return "source_key must be a valid wechat feed URL or Lingowhale channel_id"
    if platform == "bilibili_up" and not _BILI_UID_RE.fullmatch(source_key):
        return "source_key must be a valid bilibili uid"
    return None


def _preview_entry(title=None, url=None, published_at=None, summary=None):
    return {
        "title": title,
        "url": url,
        "published_at": published_at,
        "summary": summary,
    }


def _validate_rss(source_key):
    try:
        resp = requests.get(source_key, timeout=10, headers=_HTTP_HEADERS)
        resp.raise_for_status()
    except requests.RequestException as exc:
        return _json_error(f"RSS feed is not reachable: {exc}", 400)

    parsed = feedparser.parse(resp.content)
    entries = list(parsed.entries or [])
    if not entries:
        return {
            "status": "empty",
            "platform": "rss",
            "source_key": source_key,
            "preview": [],
            "warning": "Feed is reachable but has no entries",
        }
    preview = [
        _preview_entry(
            title=e.get("title"),
            url=e.get("link"),
            published_at=e.get("published") or e.get("updated"),
            summary=e.get("summary"),
        )
        for e in entries[:3]
    ]
    return {"status": "ok", "platform": "rss", "source_key": source_key, "preview": preview}


def _validate_reddit(source_key):
    about_url = f"https://www.reddit.com/r/{source_key}/about.json"
    try:
        about = requests.get(about_url, timeout=10, headers=_HTTP_HEADERS)
        if about.status_code == 404:
            return _json_error("Subreddit does not exist or is not accessible", 400)
        about.raise_for_status()
        hot = requests.get(
            f"https://www.reddit.com/r/{source_key}/hot.json?limit=3",
            timeout=10,
            headers=_HTTP_HEADERS,
        )
        hot.raise_for_status()
    except requests.RequestException as exc:
        return _json_error(f"Reddit validation failed: {exc}", 400)

    children = ((hot.json() or {}).get("data") or {}).get("children") or []
    preview = []
    for child in children[:3]:
        data = child.get("data") or {}
        preview.append(_preview_entry(
            title=data.get("title"),
            url=("https://www.reddit.com" + data.get("permalink")) if data.get("permalink") else data.get("url"),
            published_at=data.get("created_utc"),
            summary=data.get("selftext") or data.get("subreddit_name_prefixed"),
        ))
    about_data = ((about.json() or {}).get("data") or {})
    return {
        "status": "ok",
        "platform": "reddit",
        "source_key": source_key,
        "display_name": about_data.get("display_name_prefixed") or f"r/{source_key}",
        "preview": preview,
    }


def _validate_github_repo(source_key):
    try:
        repo = requests.get(
            f"https://api.github.com/repos/{source_key}",
            timeout=10,
            headers=_HTTP_HEADERS,
        )
        if repo.status_code == 404:
            return _json_error("GitHub repository does not exist or is not accessible", 400)
        repo.raise_for_status()
    except requests.RequestException as exc:
        return _json_error(f"GitHub validation failed: {exc}", 400)

    data = repo.json() or {}
    preview = [_preview_entry(
        title=data.get("full_name") or source_key,
        url=data.get("html_url"),
        published_at=data.get("updated_at"),
        summary=data.get("description"),
    )]
    return {
        "status": "ok",
        "platform": "github_repo",
        "source_key": source_key,
        "display_name": data.get("full_name") or source_key,
        "preview": preview,
    }


def _x_validation_deferred(source_key):
    return {
        "status": "deferred",
        "platform": "x_user",
        "source_key": source_key,
        "reason": _X_VALIDATION_DEFERRED_REASON,
        "preview": [],
    }


def _x_validation_empty(source_key, warning=None):
    result = {
        "status": "empty",
        "platform": "x_user",
        "source_key": source_key,
        "preview": [],
    }
    if warning:
        result["warning"] = warning
    return result


def _twitter_cli_login_error(message):
    text = str(message or "").lower()
    markers = (
        "not_authenticated",
        "not authenticated",
        "not logged",
        "log in",
        "login",
        "unauthorized",
        "cookie",
        "session",
        "expired",
        "credentials",
    )
    return any(marker in text for marker in markers)


def _x_tweet_field(item, keys):
    if not isinstance(item, dict):
        return None
    for key in keys:
        value = item.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return None


def _x_tweet_text(item):
    if isinstance(item, str):
        return item.strip() or None
    return _first_string(item, ("text", "full_text", "fullText", "content", "body", "title"))


def _x_tweet_url(handle, item):
    tweet_id = _x_tweet_field(item, ("id", "id_str", "idStr", "tweet_id", "tweetId", "rest_id"))
    if tweet_id:
        return f"https://x.com/{handle}/status/{tweet_id}"
    return None


def _x_preview_from_tweets(handle, tweets):
    preview = []
    for item in tweets[:3]:
        text = _x_tweet_text(item)
        preview.append(_preview_entry(
            title=text,
            url=_x_tweet_url(handle, item),
            published_at=_x_tweet_field(item, ("time", "createdAt", "created_at", "date")),
            summary=text,
        ))
    return preview


def _x_display_name_from_tweets(handle, tweets):
    for item in tweets:
        item_handle = _x_handle_from_item(item)
        if item_handle:
            return item_handle
    return handle


def _validate_x_user(source_key):
    handle = _normalize_x_handle(source_key)
    if not _X_HANDLE_RE.fullmatch(handle):
        return _json_error("source_key must be a valid X handle", 400)

    try:
        result = subprocess.run(
            ["twitter", "--compact", "user-posts", handle, "-n", "3", "--json"],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except FileNotFoundError:
        return _x_validation_deferred(handle)
    except subprocess.TimeoutExpired:
        return _x_validation_empty(handle, warning=_X_VALIDATION_WARNING)
    except Exception:  # noqa: BLE001 — X validate should not block manual source creation.
        return _x_validation_empty(handle, warning=_X_VALIDATION_WARNING)

    if result.returncode != 0:
        message = (result.stderr or result.stdout or f"exit {result.returncode}").strip()
        if _twitter_cli_login_error(message):
            return _x_validation_deferred(handle)
        return _x_validation_empty(handle, warning=_X_VALIDATION_WARNING)

    try:
        tweets = json.loads(result.stdout or "[]")
    except ValueError:
        return _x_validation_empty(handle, warning=_X_VALIDATION_WARNING)
    if not isinstance(tweets, list):
        return _x_validation_empty(handle, warning=_X_VALIDATION_WARNING)
    if not tweets:
        return _x_validation_empty(handle)

    return {
        "status": "ok",
        "platform": "x_user",
        "source_key": handle,
        "display_name": _x_display_name_from_tweets(handle, tweets),
        "preview": _x_preview_from_tweets(handle, tweets),
    }


def _validate_deferred(platform, source_key):
    return None


def _run_source_validation(platform, source_key):
    if platform == "x_user":
        return _validate_x_user(source_key)
    deferred = _validate_deferred(platform, source_key)
    if deferred:
        return deferred
    if platform == "wechat_mp":
        backend = _wechat_backend(source_key)
        if backend == "lingowhale":
            return {
                "status": "ok",
                "platform": platform,
                "source_key": source_key,
                "backend": backend,
                "preview": [],
            }
        result = _validate_rss(source_key)
        if isinstance(result, dict):
            result = dict(result)
            result["platform"] = platform
            result["backend"] = backend
        return result
    if platform == "rss":
        result = _validate_rss(source_key)
        if isinstance(result, dict):
            result = dict(result)
            result["platform"] = platform
        return result
    if platform == "reddit":
        return _validate_reddit(source_key)
    if platform == "github_repo":
        return _validate_github_repo(source_key)
    if platform == "bilibili_up":
        return {
            "status": "deferred",
            "platform": platform,
            "source_key": source_key,
            "reason": "Bilibili UP validation is not wired to a registry-backed fetcher in this wave.",
            "preview": [],
        }
    return _json_error("platform is not supported", 400)


def _source_platform_alias(platform):
    return {
        "github_repo": "github",
        "wechat_mp": "lingowhale",
        "bilibili_up": "bilibili",
    }.get(platform, platform)


def _source_fetch_aliases(row):
    platform = row["platform"]
    source_key = row["source_key"]
    aliases = {source_key}
    config = _parse_config_json(row["config_json"])
    if platform == "rss" and isinstance(config, dict) and config.get("slug"):
        aliases.add(f"feed:{config['slug']}")
    elif platform == "reddit":
        aliases.add(f"r/{source_key}")
    elif platform == "github_repo":
        aliases.add(f"awesome:{source_key}")
    elif platform == "wechat_mp":
        backend = config.get("backend") if isinstance(config, dict) else None
        if backend == "lingowhale" or not _is_http_feed_url(source_key):
            aliases.add(f"lingowhale:{source_key}")
        else:
            aliases.add(f"wechat:{source_key}")
    return sorted(aliases)


def _source_health(conn, row):
    source_id = row["id"]
    platform = _source_platform_alias(row["platform"])
    aliases = _source_fetch_aliases(row)
    since = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ")

    conditions = ["i.source_id = ?"]
    params = [source_id]
    if aliases:
        placeholders = ",".join(["?"] * len(aliases))
        conditions.append(f"(fri.platform = ? AND fri.source IN ({placeholders}))")
        params.extend([platform, *aliases])
    where = " OR ".join(f"({c})" for c in conditions)

    last = conn.execute(
        f"""SELECT COALESCE(fr.finished_at, fri.recorded_at, i.fetched_at) AS fetched_at
              FROM fetch_run_items fri
              JOIN items i ON i.id = fri.item_id
              LEFT JOIN fetch_runs fr ON fr.id = fri.run_id
             WHERE {where}
             ORDER BY COALESCE(fr.finished_at, fri.recorded_at, i.fetched_at) DESC
             LIMIT 1""",
        params,
    ).fetchone()

    last_item_at = last["fetched_at"] if last else None
    last_success_at = row["last_success_at"]
    last_fetched_at = max(
        (value for value in (last_item_at, last_success_at) if value),
        default=None,
    )

    if not last_fetched_at:
        return {
            "last_fetched_at": None,
            "inserted_7d": None,
            "consecutive_failures": row["consecutive_failures"],
        }

    count_params = [*params, since]
    count = conn.execute(
        f"""SELECT COUNT(*) AS c
              FROM fetch_run_items fri
              JOIN items i ON i.id = fri.item_id
              LEFT JOIN fetch_runs fr ON fr.id = fri.run_id
             WHERE ({where})
               AND fri.was_inserted = 1
               AND COALESCE(fr.finished_at, fri.recorded_at, i.fetched_at) >= ?""",
        count_params,
    ).fetchone()["c"]
    return {
        "last_fetched_at": last_fetched_at,
        "inserted_7d": int(count),
        "consecutive_failures": row["consecutive_failures"],
    }


def _source_dict(row, health=None):
    data = {
        "id": row["id"],
        "platform": row["platform"],
        "source_key": row["source_key"],
        "display_name": row["display_name"],
        "status": row["status"],
        "config_json": _parse_config_json(row["config_json"]),
        "origin": row["origin"],
        "validated_at": row["validated_at"],
        "consecutive_failures": row["consecutive_failures"],
        "last_success_at": row["last_success_at"],
        "last_error": row["last_error"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }
    if health is not None:
        data["health"] = health
    return data


def _remote_row(row):
    if row is None:
        return None
    data = dict(row)
    for key, value in list(data.items()):
        if isinstance(value, datetime):
            data[key] = value.isoformat()
    return data


def _remote_get_source(conn, source_id):
    schema = remote_db.remote_schema()
    return _remote_row(conn.execute(
        f"SELECT * FROM {schema}.sources WHERE id = %s",
        (source_id,),
    ).fetchone())


def _remote_inserted_7d_by_source(conn):
    schema = remote_db.remote_schema()
    rows = conn.execute(
        f"""SELECT source_id, count(*) AS c
              FROM {schema}.items
             WHERE source_id IS NOT NULL
               AND fetched_at >= now()-interval '7 days'
             GROUP BY source_id"""
    ).fetchall()
    return {row["source_id"]: int(row["c"]) for row in rows}


def _remote_source_health(row, inserted_7d_by_source):
    return {
        "last_fetched_at": row["last_success_at"],
        "inserted_7d": inserted_7d_by_source.get(row["id"], 0),
        "consecutive_failures": row["consecutive_failures"],
    }


def _json_object(value):
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return None
        return parsed if isinstance(parsed, dict) else None
    return None


def _iso_value(value):
    return value.isoformat() if isinstance(value, datetime) else value


def _latest_x_attempts_from_rows(rows):
    for row in rows:
        stats = _json_object(row["stats_json"])
        summary = stats.get("_x_source_attempts") if stats else None
        if not isinstance(summary, dict):
            continue

        run_id = row["id"]
        attempts = {}
        results = summary.get("results")
        if isinstance(results, list):
            for raw in results:
                if not isinstance(raw, dict) or raw.get("source_id") is None:
                    continue
                result = dict(raw)
                result["run_id"] = run_id
                attempts[result["source_id"]] = result
        missed_ids = summary.get("missed_source_ids")
        if isinstance(missed_ids, list):
            for source_id in missed_ids:
                attempts[source_id] = {
                    "run_id": run_id,
                    "source_id": source_id,
                    "outcome": "missed",
                    "attempts": attempts.get(source_id, {}).get("attempts", 0),
                    "new_count": 0,
                }

        run_summary = {
            "run_id": run_id,
            "started_at": _iso_value(row["started_at"]),
            "finished_at": _iso_value(row["finished_at"]),
        }
        for key in ("planned", "attempted", "succeeded", "no_new", "failed", "missed"):
            try:
                run_summary[key] = int(summary.get(key) or 0)
            except (TypeError, ValueError):
                run_summary[key] = 0
        for key in ("mode", "list_id", "unmatched_posts"):
            if key in summary:
                run_summary[key] = summary[key]
        return run_summary, attempts
    return None, {}


def _latest_x_attempts_local(conn):
    rows = conn.execute(
        """SELECT id, started_at, finished_at, stats_json
             FROM fetch_runs
            WHERE stats_json IS NOT NULL
            ORDER BY id DESC
            LIMIT 20"""
    ).fetchall()
    return _latest_x_attempts_from_rows(rows)


def _latest_x_attempts_remote(conn):
    try:
        schema = remote_db.remote_schema()
        rows = conn.execute(
            f"""SELECT id, started_at, finished_at, stats_json
                  FROM {schema}.fetch_runs
                 WHERE stats_json IS NOT NULL
                 ORDER BY id DESC
                 LIMIT 20"""
        ).fetchall()
    except Exception:
        return None, {}
    return _latest_x_attempts_from_rows(rows)


def _inserted_source_id(cur):
    row = cur.fetchone()
    return row["id"] if isinstance(row, dict) else row[0]


def _create_source_remote(
    platform,
    source_key,
    display_name,
    status,
    config_json,
    validated_at,
    now,
):
    with remote_db.connect() as conn:
        schema = remote_db.remote_schema()
        existing = _remote_row(conn.execute(
            f"SELECT * FROM {schema}.sources WHERE platform = %s AND source_key = %s",
            (platform, source_key),
        ).fetchone())
        if existing and existing["status"] != "deleted":
            return JSONResponse(
                {"error": "source already exists", "source": _source_dict(existing)},
                status_code=409,
            )
        if existing:
            conn.execute(
                f"""UPDATE {schema}.sources
                      SET display_name = %s, status = %s, config_json = %s,
                          origin = %s, validated_at = COALESCE(%s, validated_at),
                          updated_at = %s
                    WHERE id = %s""",
                (display_name, status, config_json, "admin_add", validated_at, now, existing["id"]),
            )
            conn.commit()
            row = _remote_get_source(conn, existing["id"])
            return {"ok": True, "source": _source_dict(row)}

        cur = conn.execute(
            f"""INSERT INTO {schema}.sources(platform, source_key, display_name, status,
                                             config_json, origin, validated_at, created_at, updated_at)
                VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)
                RETURNING id""",
            (platform, source_key, display_name, status, config_json, "admin_add",
             validated_at, now, now),
        )
        source_id = _inserted_source_id(cur)
        conn.commit()
        row = _remote_get_source(conn, source_id)
        return {"ok": True, "source": _source_dict(row)}


def _patch_source_remote(source_id, body):
    updates = []
    params = []
    if "status" in body:
        status = str(body.get("status") or "").strip()
        if status not in _PATCH_STATUSES:
            return _json_error("status must be active or paused", 400)
        updates.append("status = %s")
        params.append(status)
    if "config_json" in body:
        updates.append("config_json = %s")
        params.append(_serialize_config_json(body.get("config_json")))
    if not updates:
        return _json_error("No supported fields to update", 400)
    updates.append("updated_at = %s")
    params.append(_now())
    params.append(source_id)

    with remote_db.connect() as conn:
        row = _remote_get_source(conn, source_id)
        if not row:
            return _json_error("Source not found", 404)
        schema = remote_db.remote_schema()
        conn.execute(f"UPDATE {schema}.sources SET {', '.join(updates)} WHERE id = %s", params)
        conn.commit()
        return {"ok": True, "source": _source_dict(_remote_get_source(conn, source_id))}


def _delete_source_remote(source_id):
    with remote_db.connect() as conn:
        row = _remote_get_source(conn, source_id)
        if not row:
            return _json_error("Source not found", 404)
        schema = remote_db.remote_schema()
        conn.execute(
            f"UPDATE {schema}.sources SET status = %s, updated_at = %s WHERE id = %s",
            ("deleted", _now(), source_id),
        )
        conn.commit()
        return {"ok": True, "source": _source_dict(_remote_get_source(conn, source_id))}


def _sync_result(imported=0, existing=0, total=0, note=None):
    data = {"imported": imported, "existing": existing, "total": total}
    if note:
        data["note"] = note
    return data


def _source_row_status(row):
    try:
        return row["status"]
    except (KeyError, TypeError):
        return None


def _source_row_id(row):
    try:
        return row["id"]
    except (KeyError, TypeError):
        return None


def _normalize_sync_source(platform, source_key, display_name=None, config_json=None):
    source_key = str(source_key or "").strip()
    if platform == "x_user":
        source_key = source_key.lstrip("@").strip()
    if not source_key or _source_key_error(platform, source_key):
        return None
    return {
        "platform": platform,
        "source_key": source_key,
        "display_name": str(display_name or source_key).strip() or source_key,
        "config_json": _serialize_config_json(config_json),
    }


def _sync_source_records_remote(records, default_status="active"):
    imported = 0
    existing = 0
    now = _now()
    with remote_db.connect() as conn:
        schema = remote_db.remote_schema()
        for record in records:
            platform = record["platform"]
            source_key = record["source_key"]
            display_name = record["display_name"]
            config_json = record["config_json"]
            row = _remote_row(conn.execute(
                f"SELECT * FROM {schema}.sources WHERE platform = %s AND source_key = %s",
                (platform, source_key),
            ).fetchone())
            if row and _source_row_status(row) != "deleted":
                conn.execute(
                    f"UPDATE {schema}.sources SET display_name = %s, updated_at = %s WHERE id = %s",
                    (display_name, now, _source_row_id(row)),
                )
                existing += 1
            elif row:
                conn.execute(
                    f"""UPDATE {schema}.sources
                          SET display_name = %s, status = %s, config_json = %s,
                              origin = %s, updated_at = %s
                        WHERE id = %s""",
                    (display_name, default_status, config_json, "reconcile_import", now, _source_row_id(row)),
                )
                imported += 1
            else:
                conn.execute(
                    f"""INSERT INTO {schema}.sources(platform, source_key, display_name, status,
                                                     config_json, origin, created_at, updated_at)
                        VALUES(%s,%s,%s,%s,%s,%s,%s,%s)
                        RETURNING id""",
                    (platform, source_key, display_name, default_status, config_json,
                     "reconcile_import", now, now),
                )
                imported += 1
        conn.commit()
    return _sync_result(imported, existing, len(records))


def _sync_source_records_local(records, default_status="active"):
    imported = 0
    existing = 0
    now = _now()
    conn = db.get_conn()
    try:
        for record in records:
            platform = record["platform"]
            source_key = record["source_key"]
            display_name = record["display_name"]
            config_json = record["config_json"]
            row = conn.execute(
                "SELECT * FROM sources WHERE platform = ? AND source_key = ?",
                (platform, source_key),
            ).fetchone()
            if row and _source_row_status(row) != "deleted":
                conn.execute(
                    "UPDATE sources SET display_name = ?, updated_at = ? WHERE id = ?",
                    (display_name, now, _source_row_id(row)),
                )
                existing += 1
            elif row:
                conn.execute(
                    """UPDATE sources
                          SET display_name = ?, status = ?, config_json = ?,
                              origin = 'reconcile_import', updated_at = ?
                        WHERE id = ?""",
                    (display_name, default_status, config_json, now, _source_row_id(row)),
                )
                imported += 1
            else:
                conn.execute(
                    """INSERT INTO sources(platform, source_key, display_name, status,
                                           config_json, origin, created_at, updated_at)
                       VALUES(?,?,?,?,?,?,?,?)""",
                    (platform, source_key, display_name, default_status, config_json,
                     "reconcile_import", now, now),
                )
                imported += 1
        conn.commit()
        return _sync_result(imported, existing, len(records))
    finally:
        conn.close()


def _sync_source_records(records, default_status="active"):
    if remote_db.fetch_write_to_remote():
        return _sync_source_records_remote(records, default_status=default_status)
    return _sync_source_records_local(records, default_status=default_status)


def _reconcile_lingowhale_remote(channels, requested):
    with remote_db.connect() as conn:
        schema = remote_db.remote_schema()
        missing = []
        imported = []
        now = _now()
        for ch in channels:
            source_key = str(ch.get("channel_id") or "").strip()
            if not source_key or _source_key_error("wechat_mp", source_key):
                continue
            display_name = str(ch.get("name") or source_key)
            row = _remote_row(conn.execute(
                f"SELECT * FROM {schema}.sources WHERE platform = %s AND source_key = %s",
                ("wechat_mp", source_key),
            ).fetchone())
            is_missing = row is None or row["status"] == "deleted"
            if is_missing:
                item = {"platform": "wechat_mp", "source_key": source_key, "display_name": display_name}
                missing.append(item)
                if source_key in requested:
                    config_json = _serialize_config_json({"backend": "lingowhale"})
                    if row:
                        conn.execute(
                            f"""UPDATE {schema}.sources
                                  SET display_name = %s, status = %s, config_json = %s,
                                      origin = %s, updated_at = %s
                                WHERE id = %s""",
                            (display_name, "active", config_json, "reconcile_import", now, row["id"]),
                        )
                        source_id = row["id"]
                    else:
                        cur = conn.execute(
                            f"""INSERT INTO {schema}.sources(platform, source_key, display_name, status,
                                                             config_json, origin, created_at, updated_at)
                                VALUES(%s,%s,%s,%s,%s,%s,%s,%s)
                                RETURNING id""",
                            ("wechat_mp", source_key, display_name, "active",
                             config_json, "reconcile_import", now, now),
                        )
                        source_id = _inserted_source_id(cur)
                    imported.append({**item, "id": source_id})
        conn.commit()
        return {
            "missing": missing,
            "imported": imported,
            "note": None,
        }


def _get_source(conn, source_id):
    return conn.execute("SELECT * FROM sources WHERE id = ?", (source_id,)).fetchone()


def _parse_limit(value, default=20):
    try:
        limit = int(value)
    except (TypeError, ValueError):
        return default
    return limit if limit > 0 else default


def _existing_wechat_source_keys(channel_ids):
    channel_ids = [cid for cid in dict.fromkeys(channel_ids) if cid]
    if not channel_ids:
        return set()

    if remote_db.fetch_write_to_remote():
        with remote_db.connect() as conn:
            schema = remote_db.remote_schema()
            rows = conn.execute(
                f"""SELECT source_key FROM {schema}.sources
                     WHERE platform = %s
                       AND source_key = ANY(%s)
                       AND status != %s""",
                ("wechat_mp", channel_ids, "deleted"),
            ).fetchall()
            return {row["source_key"] for row in rows}

    conn = db.get_conn()
    try:
        placeholders = ",".join(["?"] * len(channel_ids))
        rows = conn.execute(
            f"""SELECT source_key FROM sources
                 WHERE platform = ?
                   AND source_key IN ({placeholders})
                   AND status != ?""",
            ("wechat_mp", *channel_ids, "deleted"),
        ).fetchall()
        return {row["source_key"] for row in rows}
    finally:
        conn.close()


def _lingowhale_failure_note(reason):
    reason = str(reason or "empty result").strip() or "empty result"
    note = f"语鲸订阅拉取失败或为空: {reason}"
    lower = reason.lower()
    if "10010" in reason or "token" in lower:
        note += "；语鲸 token 失效，需刷新"
    return note


def _lingowhale_groups_payload(fetch_result):
    if (
        isinstance(fetch_result, tuple)
        and len(fetch_result) >= 2
        and isinstance(fetch_result[1], list)
    ):
        return fetch_result[1]
    return fetch_result


def _lingowhale_sync_channels(fetch_result):
    payload = _lingowhale_groups_payload(fetch_result)
    channels = []
    seen = set()

    def add(channel):
        if not isinstance(channel, dict):
            return
        channel_id = str(channel.get("channel_id") or "").strip()
        if not channel_id or channel_id in seen:
            return
        seen.add(channel_id)
        channels.append({
            "channel_id": channel_id,
            "name": channel.get("name") or channel_id,
        })

    def walk(obj):
        if isinstance(obj, dict):
            channel_list = obj.get("channels")
            if isinstance(channel_list, list):
                for channel in channel_list:
                    add(channel)
            elif obj.get("channel_id"):
                add(obj)
            for value in obj.values():
                walk(value)
        elif isinstance(obj, (list, tuple)):
            for value in obj:
                walk(value)

    walk(payload)
    return channels


def _lingowhale_sync_sources():
    try:
        fetched = fetch_lingowhale.fetch_groups()
    except Exception as exc:
        return [], _lingowhale_failure_note(exc)

    channels = _lingowhale_sync_channels(fetched) if fetched else []
    if not channels:
        return [], _lingowhale_failure_note("empty result")

    records = []
    seen = set()
    for ch in channels:
        source_key = str((ch or {}).get("channel_id") or "").strip()
        if source_key in seen:
            continue
        record = _normalize_sync_source(
            "wechat_mp",
            source_key,
            (ch or {}).get("name") or source_key,
            {"backend": "lingowhale"},
        )
        if not record:
            continue
        seen.add(record["source_key"])
        records.append(record)

    if not records:
        return [], _lingowhale_failure_note("empty result")
    return records, None


def _normalize_x_handle(value):
    return str(value or "").strip().lstrip("@").strip()


def _first_string(obj, keys):
    if not isinstance(obj, dict):
        return None
    for key in keys:
        value = obj.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    for nested_key in ("user", "author", "legacy"):
        nested = obj.get(nested_key)
        if isinstance(nested, dict):
            found = _first_string(nested, keys)
            if found:
                return found
    return None


def _x_handle_from_item(item):
    if isinstance(item, str):
        return _normalize_x_handle(item)
    return _normalize_x_handle(_first_string(
        item,
        ("screen_name", "screenName", "username", "handle"),
    ))


def _sync_lingowhale_sources_impl():
    records, note = _lingowhale_sync_sources()
    if note:
        return _sync_result(note=note)
    return _sync_source_records(records, default_status="active")


def _x_list_sources(rows):
    return [
        row
        for row in rows
        if row["platform"] == "x_user"
        and row["status"] in {"active", "broken", "not_fetched"}
    ]


def _sync_x_list_impl(full=False):
    sources = fetch_x_users._active_x_sources()
    return x_list_registry.sync_registry_members(sources, full=bool(full))


@router.get("/api/admin/sources")
def list_sources(request: Request):
    err = require_admin(request)
    if err:
        return err

    if remote_db.fetch_write_to_remote():
        with remote_db.connect() as conn:
            schema = remote_db.remote_schema()
            rows = [
                _remote_row(row)
                for row in conn.execute(
                    f"""SELECT * FROM {schema}.sources
                         WHERE status != %s
                         ORDER BY platform, id""",
                    ("deleted",),
                ).fetchall()
            ]
            inserted_7d_by_source = _remote_inserted_7d_by_source(conn)
            latest_x_run, latest_x_attempts = _latest_x_attempts_remote(conn)
            groups_by_platform = {}
            for row in rows:
                health = _remote_source_health(row, inserted_7d_by_source)
                if row["platform"] == "x_user" and row["id"] in latest_x_attempts:
                    health["latest_attempt"] = latest_x_attempts[row["id"]]
                groups_by_platform.setdefault(row["platform"], []).append(
                    _source_dict(row, health)
                )
            return {
                "groups": [
                    {"platform": platform, "sources": sources}
                    for platform, sources in groups_by_platform.items()
                ],
                "total": len(rows),
                "latest_x_run": latest_x_run,
                "x_list": x_list_registry.status_for_sources(_x_list_sources(rows)),
            }

    conn = db.get_conn()
    try:
        rows = conn.execute(
            """SELECT * FROM sources
               WHERE status != 'deleted'
               ORDER BY platform ASC, id ASC"""
        ).fetchall()
        latest_x_run, latest_x_attempts = _latest_x_attempts_local(conn)
        groups_by_platform = {}
        for row in rows:
            health = _source_health(conn, row)
            if row["platform"] == "x_user" and row["id"] in latest_x_attempts:
                health["latest_attempt"] = latest_x_attempts[row["id"]]
            groups_by_platform.setdefault(row["platform"], []).append(
                _source_dict(row, health)
            )
        return {
            "groups": [
                {"platform": platform, "sources": sources}
                for platform, sources in groups_by_platform.items()
            ],
            "total": len(rows),
            "latest_x_run": latest_x_run,
            "x_list": x_list_registry.status_for_sources(_x_list_sources(rows)),
        }
    finally:
        conn.close()


@router.get("/api/admin/sources/search-wechat")
def search_wechat_sources(request: Request):
    err = require_admin(request)
    if err:
        return err

    query = str(request.query_params.get("q") or "").strip()
    if not query:
        return _json_error("q is required", 400)
    limit = _parse_limit(request.query_params.get("limit"), 20)

    try:
        channels = fetch_lingowhale.search_channels(query, limit=limit)
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=502)

    existing = _existing_wechat_source_keys([
        str(ch.get("channel_id") or "").strip()
        for ch in channels
    ])
    return {
        "channels": [
            {
                **ch,
                "already_in_registry": str(ch.get("channel_id") or "").strip() in existing,
            }
            for ch in channels
        ]
    }


@router.post("/api/admin/sources/sync-lingowhale")
async def sync_lingowhale_sources(request: Request):
    err = require_admin(request)
    if err:
        return err
    await _optional_json_body(request)
    return await run_in_threadpool(_sync_lingowhale_sources_impl)


@router.post("/api/admin/sources/sync-twitter-following")
async def sync_twitter_following(request: Request):
    err = require_admin(request)
    if err:
        return err
    await _optional_json_body(request)
    return _json_error(
        "personal X Following sync is disabled; sources registry and X List are authoritative",
        410,
    )


@router.post("/api/admin/sources/x-list/sync")
async def sync_x_list(request: Request):
    err = require_admin(request)
    if err:
        return err
    body = await _optional_json_body(request)
    return await run_in_threadpool(_sync_x_list_impl, bool(body.get("full")))


@router.post("/api/admin/sources/validate")
async def validate_source(request: Request):
    err = require_admin(request)
    if err:
        return err
    body, err = await _json_body(request)
    if err:
        return err
    platform = str(body.get("platform") or "").strip()
    source_key = str(body.get("source_key") or "").strip()
    if platform == "x_user":
        source_key = _normalize_x_handle(source_key)
    key_error = _source_key_error(platform, source_key)
    if key_error:
        return _json_error(key_error, 400)
    return await run_in_threadpool(_run_source_validation, platform, source_key)


@router.post("/api/admin/sources")
async def create_source(request: Request):
    err = require_admin(request)
    if err:
        return err
    body, err = await _json_body(request)
    if err:
        return err

    platform = str(body.get("platform") or "").strip()
    source_key = str(body.get("source_key") or "").strip()
    key_error = _source_key_error(platform, source_key)
    if key_error:
        return _json_error(key_error, 400)
    status = str(body.get("status") or "active").strip()
    if status not in _CREATE_STATUSES:
        return _json_error("status is invalid", 400)
    display_name = body.get("display_name") or source_key
    config_value = body.get("config_json")
    if platform == "wechat_mp":
        config_value = _config_with_backend(config_value, _wechat_backend(source_key))
    config_json = _serialize_config_json(config_value)
    validated_at = body.get("validated_at") if isinstance(body.get("validated_at"), str) else None
    now = _now()

    if remote_db.fetch_write_to_remote():
        return _create_source_remote(
            platform,
            source_key,
            display_name,
            status,
            config_json,
            validated_at,
            now,
        )

    conn = db.get_conn()
    try:
        existing = conn.execute(
            "SELECT * FROM sources WHERE platform = ? AND source_key = ?",
            (platform, source_key),
        ).fetchone()
        if existing and existing["status"] != "deleted":
            return JSONResponse(
                {"error": "source already exists", "source": _source_dict(existing)},
                status_code=409,
            )
        if existing:
            conn.execute(
                """UPDATE sources
                      SET display_name = ?, status = ?, config_json = ?,
                          origin = 'admin_add', validated_at = COALESCE(?, validated_at),
                          updated_at = ?
                    WHERE id = ?""",
                (display_name, status, config_json, validated_at, now, existing["id"]),
            )
            conn.commit()
            row = _get_source(conn, existing["id"])
            return {"ok": True, "source": _source_dict(row)}

        cur = conn.execute(
            """INSERT INTO sources(platform, source_key, display_name, status,
                                   config_json, origin, validated_at, created_at, updated_at)
               VALUES(?,?,?,?,?,?,?,?,?)""",
            (platform, source_key, display_name, status, config_json, "admin_add",
             validated_at, now, now),
        )
        conn.commit()
        row = _get_source(conn, cur.lastrowid)
        return {"ok": True, "source": _source_dict(row)}
    finally:
        conn.close()


@router.post("/api/admin/sources/lingowhale/reconcile")
async def reconcile_lingowhale(request: Request):
    err = require_admin(request)
    if err:
        return err
    body, err = await _json_body(request)
    if err:
        return err

    groups_path = os.path.join(BASE, "data", "lingowhale", "groups.json")
    if not os.path.exists(groups_path):
        return {
            "missing": [],
            "imported": [],
            "note": "data/lingowhale/groups.json does not exist; local Lingowhale snapshot is unavailable.",
        }

    try:
        with open(groups_path, encoding="utf-8") as f:
            groups = json.load(f)
    except (OSError, ValueError) as exc:
        return _json_error(f"Failed to read lingowhale groups snapshot: {exc}", 400)

    requested = body.get("import_keys")
    if requested is None:
        requested = body.get("source_keys") or []
    requested = {str(k) for k in requested if k}

    channels = _iter_lingowhale_channels(groups)
    if remote_db.fetch_write_to_remote():
        return _reconcile_lingowhale_remote(channels, requested)

    conn = db.get_conn()
    try:
        missing = []
        imported = []
        now = _now()
        for ch in channels:
            source_key = str(ch.get("channel_id") or "").strip()
            if not source_key or _source_key_error("wechat_mp", source_key):
                continue
            display_name = str(ch.get("name") or source_key)
            row = conn.execute(
                "SELECT * FROM sources WHERE platform = 'wechat_mp' AND source_key = ?",
                (source_key,),
            ).fetchone()
            is_missing = row is None or row["status"] == "deleted"
            if is_missing:
                item = {"platform": "wechat_mp", "source_key": source_key, "display_name": display_name}
                missing.append(item)
                if source_key in requested:
                    config_json = _serialize_config_json({"backend": "lingowhale"})
                    if row:
                        conn.execute(
                            """UPDATE sources
                                  SET display_name = ?, status = 'active', config_json = ?,
                                      origin = 'reconcile_import', updated_at = ?
                                WHERE id = ?""",
                            (display_name, config_json, now, row["id"]),
                        )
                        source_id = row["id"]
                    else:
                        cur = conn.execute(
                            """INSERT INTO sources(platform, source_key, display_name, status,
                                                   config_json, origin, created_at, updated_at)
                               VALUES('wechat_mp', ?, ?, 'active', ?, 'reconcile_import', ?, ?)""",
                            (source_key, display_name, config_json, now, now),
                        )
                        source_id = cur.lastrowid
                    imported.append({**item, "id": source_id})
        conn.commit()
        return {
            "missing": missing,
            "imported": imported,
            "note": None,
        }
    finally:
        conn.close()


def _iter_lingowhale_channels(groups):
    seen = set()

    def walk(obj):
        if isinstance(obj, dict):
            channel_id = obj.get("channel_id")
            if channel_id and channel_id not in seen:
                seen.add(channel_id)
                yield obj
            for value in obj.values():
                yield from walk(value)
        elif isinstance(obj, list):
            for value in obj:
                yield from walk(value)

    return list(walk(groups))


def _config_path():
    return os.path.join(BASE, "config", "config.json")


def _load_config():
    try:
        with open(_config_path(), encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}


def _save_config(config):
    os.makedirs(os.path.dirname(_config_path()), exist_ok=True)
    with open(_config_path(), "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)
        f.write("\n")


def _get_nested(config, section, key):
    section_obj = config.get(section)
    if not isinstance(section_obj, dict):
        return None
    return section_obj.get(key)


def _set_nested(config, section, key, value):
    if not isinstance(config.get(section), dict):
        config[section] = {}
    config[section][key] = value


def _algo_params(config):
    return {
        name: _get_nested(config, section, key)
        for name, (section, key, _min_value, _max_value) in _ALGO_PARAM_SPECS.items()
    }


@router.get("/api/admin/sources/algo-params")
def get_algo_params(request: Request):
    err = require_admin(request)
    if err:
        return err
    return {"params": _algo_params(_load_config())}


@router.patch("/api/admin/sources/algo-params")
async def patch_algo_params(request: Request):
    err = require_admin(request)
    if err:
        return err
    body, err = await _json_body(request)
    if err:
        return err
    payload = body.get("params") if isinstance(body.get("params"), dict) else body

    config = _load_config()
    for name, raw_value in payload.items():
        if name not in _ALGO_PARAM_SPECS:
            continue
        section, key, min_value, max_value = _ALGO_PARAM_SPECS[name]
        try:
            value = int(raw_value)
        except (TypeError, ValueError):
            return _json_error(f"{name} must be an integer", 400)
        if value < min_value or value > max_value:
            return _json_error(f"{name} must be between {min_value} and {max_value}", 400)
        _set_nested(config, section, key, value)

    _save_config(config)
    return {"ok": True, "params": _algo_params(config)}


@router.patch("/api/admin/sources/{source_id}")
async def patch_source(source_id: int, request: Request):
    err = require_admin(request)
    if err:
        return err
    body, err = await _json_body(request)
    if err:
        return err
    if remote_db.fetch_write_to_remote():
        return _patch_source_remote(source_id, body)

    updates = []
    params = []
    if "status" in body:
        status = str(body.get("status") or "").strip()
        if status not in _PATCH_STATUSES:
            return _json_error("status must be active or paused", 400)
        updates.append("status = ?")
        params.append(status)
    if "config_json" in body:
        updates.append("config_json = ?")
        params.append(_serialize_config_json(body.get("config_json")))
    if not updates:
        return _json_error("No supported fields to update", 400)
    updates.append("updated_at = ?")
    params.append(_now())
    params.append(source_id)

    conn = db.get_conn()
    try:
        row = _get_source(conn, source_id)
        if not row:
            return _json_error("Source not found", 404)
        conn.execute(f"UPDATE sources SET {', '.join(updates)} WHERE id = ?", params)
        conn.commit()
        return {"ok": True, "source": _source_dict(_get_source(conn, source_id))}
    finally:
        conn.close()


@router.delete("/api/admin/sources/{source_id}")
def delete_source(source_id: int, request: Request):
    err = require_admin(request)
    if err:
        return err
    if remote_db.fetch_write_to_remote():
        return _delete_source_remote(source_id)

    conn = db.get_conn()
    try:
        row = _get_source(conn, source_id)
        if not row:
            return _json_error("Source not found", 404)
        conn.execute(
            "UPDATE sources SET status = 'deleted', updated_at = ? WHERE id = ?",
            (_now(), source_id),
        )
        conn.commit()
        return {"ok": True, "source": _source_dict(_get_source(conn, source_id))}
    finally:
        conn.close()
