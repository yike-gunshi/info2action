"""Admin sources registry endpoints for subscription configuration."""
import json
import os
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

import feedparser
import requests
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

import db
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

_ALGO_PARAM_SPECS = {
    "hackernews_count": ("hackernews", "count", 1, 500),
    "github_trending_count": ("github_trending", "count", 1, 500),
    "twitter_following_count": ("twitter", "following_count", 1, 500),
    "twitter_for_you_count": ("twitter", "for_you_count", 1, 500),
    "bilibili_hot_count": ("bilibili", "hot_count", 1, 500),
    "bilibili_rank_count": ("bilibili", "rank_count", 1, 500),
    "bilibili_videos_per_up": ("bilibili", "videos_per_up", 1, 100),
}


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _json_error(message, status_code=400):
    return JSONResponse({"error": message}, status_code=status_code)


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


def _validate_deferred(platform, source_key):
    if platform == "x_user":
        return {
            "status": "deferred",
            "platform": platform,
            "source_key": source_key,
            "reason": "X validation requires the local twitter CLI session, which is unavailable in this environment.",
            "preview": [],
        }
    return None


def _run_source_validation(platform, source_key):
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

    if not last:
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
        "last_fetched_at": last["fetched_at"],
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


def _get_source(conn, source_id):
    return conn.execute("SELECT * FROM sources WHERE id = ?", (source_id,)).fetchone()


def _active_x_user_count(conn):
    return int(conn.execute(
        "SELECT COUNT(*) AS c FROM sources WHERE platform = 'x_user' AND status = 'active'"
    ).fetchone()["c"])


def _gray_gated_create_message(limit):
    return f"已达 X 灰度上限 {limit}，置为 pending，放量后再激活"


def _gray_gated_patch_message(limit):
    return f"已达 X 灰度上限 {limit}，保持原状态，放量后再激活"


@router.get("/api/admin/sources")
def list_sources(request: Request):
    err = require_admin(request)
    if err:
        return err

    conn = db.get_conn()
    try:
        rows = conn.execute(
            """SELECT * FROM sources
               WHERE status != 'deleted'
               ORDER BY platform ASC, id ASC"""
        ).fetchall()
        groups_by_platform = {}
        for row in rows:
            groups_by_platform.setdefault(row["platform"], []).append(
                _source_dict(row, _source_health(conn, row))
            )
        return {
            "groups": [
                {"platform": platform, "sources": sources}
                for platform, sources in groups_by_platform.items()
            ],
            "total": len(rows),
        }
    finally:
        conn.close()


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

    conn = db.get_conn()
    try:
        gray_gated = False
        limit = None
        if platform == "x_user" and status == "active":
            limit = _x_user_gray_limit()
            if _active_x_user_count(conn) >= limit:
                status = "pending"
                gray_gated = True

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
            resp = {"ok": True, "source": _source_dict(row)}
            if gray_gated:
                resp.update({
                    "gray_gated": True,
                    "message": _gray_gated_create_message(limit),
                    "limit": limit,
                })
            return resp

        cur = conn.execute(
            """INSERT INTO sources(platform, source_key, display_name, status,
                                   config_json, origin, validated_at, created_at, updated_at)
               VALUES(?,?,?,?,?,?,?,?,?)""",
            (platform, source_key, display_name, status, config_json, "admin_add",
             validated_at, now, now),
        )
        conn.commit()
        row = _get_source(conn, cur.lastrowid)
        resp = {"ok": True, "source": _source_dict(row)}
        if gray_gated:
            resp.update({
                "gray_gated": True,
                "message": _gray_gated_create_message(limit),
                "limit": limit,
            })
        return resp
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


def _x_user_gray_limit():
    raw = _get_nested(_load_config(), "twitter", "x_user_gray_limit")
    try:
        limit = int(raw)
    except (TypeError, ValueError):
        return 30
    return limit if limit >= 0 else 30


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
    updates = []
    params = []
    requested_status = None
    if "status" in body:
        status = str(body.get("status") or "").strip()
        if status not in _PATCH_STATUSES:
            return _json_error("status must be active or paused", 400)
        requested_status = status
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
        if row["platform"] == "x_user" and row["status"] != "active" and requested_status == "active":
            limit = _x_user_gray_limit()
            if _active_x_user_count(conn) >= limit:
                message = _gray_gated_patch_message(limit)
                return JSONResponse(
                    {
                        "error": message,
                        "gray_gated": True,
                        "message": message,
                        "limit": limit,
                        "source": _source_dict(row),
                    },
                    status_code=409,
                )
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
