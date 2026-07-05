"""v12.2 F50 Round 2 — Twitter 视频媒体代理 + 首帧封面.

路由:
  GET /api/media/twitter-mp4/{item_id}           - mp4 反向代理,Range 透传,不落盘
  GET /api/media/twitter-poster/{item_id}.jpg    - ffmpeg 抽首帧,落盘永久缓存

设计动机:
  Twitter CDN (video.twimg.com) 对 localhost Referer 403;
  <video referrerPolicy="no-referrer"> 在 Chrome 实际未生效,
  <video preload="metadata"> 也不显示首帧.方案 B 走服务端代理:
  1) 后端 urllib 请求时不带 Referer (Twitter CDN 即放行)
  2) 首帧由 ffmpeg 直接读 HTTP URL 抽一帧,落盘缓存
"""
from __future__ import annotations

import hashlib
import ipaddress
import json
import logging
import os
import socket
import subprocess
import tempfile
import threading
import time
import urllib.request
from collections import OrderedDict
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlparse

from fastapi import APIRouter, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, Response, StreamingResponse


# BF-0515-twitter-image-perf: in-memory LRU cache for Twitter photo/poster
# bytes. Sits between the ETag/304 check and the Supabase Storage download.
#
# Why: even warm Storage hits cost ~1.2s/img because backend → Storage region
# is a cross-network roundtrip. With 12 photos per info-tab and browser
# same-origin concurrency=6, first paint is ~3s. Local memory hit is <50ms.
#
# Trade-offs:
# - Per-process cache (not shared across uvicorn workers). info-feed runs
#   1 worker today, so this is effectively process-wide. If we scale workers
#   later, we accept partial cache duplication; Storage stays SoT.
# - Bytes (≤200 entries × ~150KB ≈ 30MB) bound by env TWITTER_IMAGE_LRU_SIZE.
# - We do NOT cache failures or 502s — only successful (bytes, content_type)
#   tuples.
# - Thread-safe via a single Lock — call sites already run in threadpool.


class _TwitterImageLRU:
    """Bounded LRU keyed by an arbitrary hashable tuple.

    Stores (bytes, content_type). OrderedDict.move_to_end gives O(1) MRU
    promotion; popitem(last=False) gives O(1) eviction.
    """

    __slots__ = ("_store", "_lock", "maxsize")

    def __init__(self, maxsize: int):
        self.maxsize = max(1, int(maxsize))
        self._store: OrderedDict[tuple, tuple[bytes, str]] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key: tuple) -> tuple[bytes, str] | None:
        with self._lock:
            val = self._store.get(key)
            if val is None:
                return None
            self._store.move_to_end(key)
            return val

    def put(self, key: tuple, data: bytes, content_type: str) -> None:
        with self._lock:
            if key in self._store:
                self._store.move_to_end(key)
                self._store[key] = (data, content_type)
                return
            self._store[key] = (data, content_type)
            while len(self._store) > self.maxsize:
                self._store.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._store.clear()

    def __len__(self) -> int:
        return len(self._store)


def _lru_size_from_env() -> int:
    """Resolve LRU maxsize from env, default 200 entries (~30MB)."""
    raw = os.environ.get("TWITTER_IMAGE_LRU_SIZE", "").strip()
    if not raw:
        return 200
    try:
        n = int(raw)
        if n <= 0:
            return 200
        return n
    except (ValueError, TypeError):
        return 200


_twitter_image_lru = _TwitterImageLRU(maxsize=_lru_size_from_env())


def _media_cold_path_concurrency_from_env() -> int:
    raw = os.environ.get("INFO2ACTION_MEDIA_COLD_PATH_MAX_CONCURRENCY", "").strip()
    if not raw:
        return 2
    try:
        n = int(raw)
        if n <= 0:
            return 2
        return n
    except (ValueError, TypeError):
        return 2


_media_cold_path_slots = threading.BoundedSemaphore(_media_cold_path_concurrency_from_env())
_MEDIA_COLD_PATH_INFLIGHT_LOCK = threading.Lock()
_MEDIA_COLD_PATH_INFLIGHT: dict[tuple, dict] = {}
_MEDIA_COLD_PATH_SINGLEFLIGHT_TIMEOUT_SEC = 150


# BF-0515-image-etag: utility for ETag + 304 Not Modified handling on image
# endpoints. Saves ~1s × N images per soft-refresh (browser already has bytes,
# server just needs to confirm "still valid" via If-None-Match).
#
# We use a DETERMINISTIC ETag derived from the resource identifier (e.g.
# item_id) — NOT a hash of the bytes. This lets us check If-None-Match
# BEFORE doing the expensive Supabase Storage download. Trade-off: if
# the underlying Twitter image ever changes (rare, since pbs.twimg.com
# URLs are content-addressed), the user keeps the old version until they
# hard-refresh. Acceptable for image display.

_ETAG_VERSION = "v1"  # bump if poster generation algorithm changes


def _make_etag(*parts: str | int) -> str:
    """Build a quoted ETag like '"v1-poster-2048846575272840"' deterministically."""
    return '"' + _ETAG_VERSION + "-" + "-".join(str(p) for p in parts) + '"'


def _check_if_none_match(request: Request, etag: str) -> bool:
    """True if client's If-None-Match header includes our ETag (or *)."""
    inm = request.headers.get("if-none-match")
    if not inm:
        return False
    candidates = [v.strip() for v in inm.split(",")]
    return etag in candidates or "*" in candidates


def _not_modified_response(etag: str) -> Response:
    return Response(
        status_code=304,
        headers={
            "ETag": etag,
            "Cache-Control": "public, max-age=86400",
        },
    )


def _image_response_with_etag(data: bytes, etag: str, media_type: str = "image/jpeg") -> Response:
    """Build a 200 response with the given ETag."""
    return Response(
        content=data,
        media_type=media_type,
        headers={
            "ETag": etag,
            "Cache-Control": "public, max-age=86400",
        },
    )

import db
import asset_cache
import remote_db

router = APIRouter()
logger = logging.getLogger(__name__)

_BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
POSTER_DIR = os.path.join(_BASE, "data", "images", "video_posters")
if not remote_db.asset_storage_to_remote():
    os.makedirs(POSTER_DIR, exist_ok=True)

_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
_FFMPEG = os.environ.get("FFMPEG_BIN", "ffmpeg")
_CHUNK = 64 * 1024
_UPSTREAM_TIMEOUT = 30
_FFMPEG_TIMEOUT = 20
_IMAGE_PROXY_MAX_BYTES = 12 * 1024 * 1024


def _run_media_cold_path(key: tuple, func, *args):
    """Coalesce and bound cold media work before it reaches Supabase.

    Same-key requests share one cold fetch; different keys queue behind a
    small semaphore so media misses cannot consume the whole DB pool.
    """
    singleflight_key = ("media_cold_path", *key)
    with _MEDIA_COLD_PATH_INFLIGHT_LOCK:
        holder = _MEDIA_COLD_PATH_INFLIGHT.get(singleflight_key)
        if holder is None:
            holder = {"event": threading.Event(), "result": None, "error": None}
            _MEDIA_COLD_PATH_INFLIGHT[singleflight_key] = holder
            should_compute = True
        else:
            should_compute = False

    if should_compute:
        try:
            with _media_cold_path_slots:
                holder["result"] = func(*args)
        except BaseException as exc:
            holder["error"] = exc
        finally:
            with _MEDIA_COLD_PATH_INFLIGHT_LOCK:
                _MEDIA_COLD_PATH_INFLIGHT.pop(singleflight_key, None)
            holder["event"].set()
    else:
        if not holder["event"].wait(timeout=_MEDIA_COLD_PATH_SINGLEFLIGHT_TIMEOUT_SEC):
            with _media_cold_path_slots:
                return func(*args)

    if holder["error"] is not None:
        raise holder["error"]
    return holder["result"]


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_NO_REDIRECT_OPENER = urllib.request.build_opener(_NoRedirectHandler)


def _safe_item_id(item_id: str) -> str:
    # 防路径注入: Twitter id 为纯数字; 宽容允许字母数字下划线
    if not item_id or not all(c.isalnum() or c == "_" for c in item_id):
        raise HTTPException(status_code=400, detail="invalid item_id")
    return item_id


def _validate_public_image_url(raw_url: str) -> str:
    """Validate generic image proxy target without tying UI to one CDN host."""
    if not raw_url or len(raw_url) > 4096:
        raise HTTPException(status_code=400, detail="invalid image url")
    parsed = urlparse(raw_url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise HTTPException(status_code=400, detail="invalid image url")

    host = parsed.hostname
    if host in {"localhost", "metadata.google.internal"} or host.endswith(".local"):
        raise HTTPException(status_code=400, detail="private image host")

    try:
        infos = socket.getaddrinfo(
            host,
            parsed.port or (443 if parsed.scheme == "https" else 80),
            type=socket.SOCK_STREAM,
        )
    except socket.gaierror:
        raise HTTPException(status_code=400, detail="image host not resolvable")

    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_multicast
            or ip.is_reserved
            or ip.is_unspecified
        ):
            raise HTTPException(status_code=400, detail="private image host")
    return raw_url


def _fetch_external_image(raw_url: str) -> tuple[bytes, str]:
    image_url = raw_url
    cache_key = ("external-image", hashlib.sha256(image_url.encode("utf-8")).hexdigest())
    cached = _twitter_image_lru.get(cache_key)
    if cached is not None:
        return cached

    for _ in range(4):
        image_url = _validate_public_image_url(image_url)
        req = urllib.request.Request(
            image_url,
            headers={
                "User-Agent": _UA,
                "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
            },
        )
        try:
            with _NO_REDIRECT_OPENER.open(req, timeout=_UPSTREAM_TIMEOUT) as resp:
                content_type = resp.headers.get("Content-Type") or "application/octet-stream"
                if not content_type.lower().startswith("image/"):
                    raise HTTPException(status_code=415, detail="upstream is not image")
                chunks: list[bytes] = []
                total = 0
                while True:
                    chunk = resp.read(_CHUNK)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > _IMAGE_PROXY_MAX_BYTES:
                        raise HTTPException(status_code=413, detail="image too large")
                    chunks.append(chunk)
                data = b"".join(chunks)
                _twitter_image_lru.put(cache_key, data, content_type)
                return data, content_type
        except HTTPException:
            raise
        except HTTPError as exc:
            location = exc.headers.get("Location")
            if 300 <= exc.code < 400 and location:
                image_url = urljoin(image_url, location)
                continue
            logger.warning("image-proxy upstream err=%s url=%s", exc.code, image_url[:160])
            raise HTTPException(status_code=502, detail=f"upstream {exc.code}")
        except URLError as exc:
            logger.warning("image-proxy upstream unreachable url=%s err=%s", image_url[:160], str(exc.reason)[:120])
            raise HTTPException(status_code=502, detail="upstream unreachable")
    raise HTTPException(status_code=508, detail="too many image redirects")


@router.get("/api/media/image-proxy")
async def image_proxy(url: str, request: Request):
    """Same-origin proxy for external static images used by event/cluster covers."""
    etag = _make_etag("image", hashlib.sha256(url.encode("utf-8")).hexdigest()[:24])
    if _check_if_none_match(request, etag):
        return _not_modified_response(etag)
    data, content_type = await run_in_threadpool(_fetch_external_image, url)
    return _image_response_with_etag(data, etag, media_type=content_type)


# BE-8: item_id→mp4_url 小缓存——video 播放/拖动的每个 Range 请求都会调用
# 本函数,原实现每次查一遍 items 表;URL 内容寻址,天然可缓存。
_mp4_url_lru = _TwitterImageLRU(maxsize=500)


def _get_twitter_mp4_url(item_id: str) -> str:
    hit = _mp4_url_lru.get(("mp4url", item_id))
    if hit is not None:
        return hit[0].decode("utf-8")
    url = _get_twitter_mp4_url_uncached(item_id)
    _mp4_url_lru.put(("mp4url", item_id), url.encode("utf-8"), "text/plain")
    return url


def _get_twitter_mp4_url_uncached(item_id: str) -> str:
    if remote_db.app_state_to_remote() or remote_db.feed_read_from_remote():
        item = remote_db.get_media_item_remote(item_id)
        if not item:
            raise HTTPException(status_code=404, detail="item not found")
        if item.get("platform") != "twitter":
            raise HTTPException(status_code=400, detail="not a twitter item")
        media = item.get("media_json") or []
        if isinstance(media, str):
            try:
                media = json.loads(media or "[]")
            except (ValueError, TypeError):
                raise HTTPException(status_code=404, detail="invalid media_json")
        if not isinstance(media, list):
            raise HTTPException(status_code=404, detail="invalid media_json")
        for m in media:
            if isinstance(m, dict) and m.get("type") == "video" and m.get("url"):
                return m["url"]
        raise HTTPException(status_code=404, detail="no video in item")

    conn = db.get_conn()
    try:
        row = conn.execute(
            "SELECT media_json, platform FROM items WHERE id = ?", (item_id,)
        ).fetchone()
    finally:
        conn.close()
    if not row:
        raise HTTPException(status_code=404, detail="item not found")
    if row["platform"] != "twitter":
        raise HTTPException(status_code=400, detail="not a twitter item")
    try:
        media = json.loads(row["media_json"] or "[]")
    except (ValueError, TypeError):
        raise HTTPException(status_code=404, detail="invalid media_json")
    for m in media:
        if isinstance(m, dict) and m.get("type") == "video" and m.get("url"):
            return m["url"]
    raise HTTPException(status_code=404, detail="no video in item")


# ── GET /api/media/twitter-mp4/{item_id} ──────────────────────────────

@router.get("/api/media/twitter-mp4/{item_id}")
async def twitter_mp4_proxy(request: Request, item_id: str):
    """mp4 反向代理,Range 请求透传,不落盘."""
    item_id = _safe_item_id(item_id)

    headers = {"User-Agent": _UA}
    range_header = request.headers.get("range") or request.headers.get("Range")
    if range_header:
        headers["Range"] = range_header

    upstream = await run_in_threadpool(_open_twitter_mp4_upstream, item_id, headers)
    status_code = upstream.status  # 200 或 206

    def iter_upstream():
        try:
            while True:
                chunk = upstream.read(_CHUNK)
                if not chunk:
                    break
                yield chunk
        finally:
            upstream.close()

    resp_headers = {
        "Accept-Ranges": "bytes",
        "Cache-Control": "public, max-age=3600",
    }
    for key in ("Content-Length", "Content-Range"):
        val = upstream.headers.get(key)
        if val:
            resp_headers[key] = val

    return StreamingResponse(
        iter_upstream(),
        status_code=status_code,
        media_type="video/mp4",
        headers=resp_headers,
    )


def _open_twitter_mp4_upstream(item_id: str, headers: dict[str, str]):
    mp4_url = _get_twitter_mp4_url(item_id)
    req = urllib.request.Request(mp4_url, headers=headers)
    upstream = None
    last_err: Exception | None = None
    for attempt in range(2):
        try:
            upstream = urllib.request.urlopen(req, timeout=_UPSTREAM_TIMEOUT)
            break
        except HTTPError as e:
            # 4xx 业务错误不重试,直接透传状态码
            if 400 <= e.code < 500:
                raise HTTPException(status_code=e.code, detail=f"upstream {e.reason}")
            last_err = e
        except URLError as e:
            last_err = e
        if attempt == 0:
            time.sleep(0.4)
    if upstream is None:
        logger.warning("twitter-mp4 upstream failed item=%s err=%s", item_id, last_err)
        raise HTTPException(status_code=502, detail=f"upstream unreachable: {last_err}")
    return upstream


# ── GET /api/media/twitter-poster/{item_id}.jpg ───────────────────────

def ensure_twitter_poster_cached(item_id: str) -> bytes | None:
    """BF-0515-twitter-poster-prewarm: shared helper for both the request
    handler and background prewarm. Returns the poster bytes (also caches
    to Supabase Storage). Returns None on any failure (caller decides).

    Idempotent — safe to call repeatedly for already-cached items (storage
    hit returns immediately without ffmpeg)."""
    item_id_safe = _safe_item_id(item_id)
    if not remote_db.asset_storage_to_remote():
        return None  # local mode: no prewarm

    object_path = f"video_posters/{item_id_safe}.jpg"
    # B1: 磁盘缓存 L2(跨重启),Storage 下载只在真 miss 时发生
    cached = asset_cache.get_or_fetch(
        object_path, lambda: remote_db.download_asset_bytes_remote(object_path))
    if cached:
        return cached

    try:
        mp4_url = _get_twitter_mp4_url(item_id_safe)
    except HTTPException:
        return None

    tmp_dir = os.path.join(tempfile.gettempdir(), "info2action-video-posters")
    os.makedirs(tmp_dir, exist_ok=True)
    cache_path = os.path.join(tmp_dir, f"{item_id_safe}.jpg")
    try:
        _generate_poster_file(item_id_safe, mp4_url, cache_path)
        with open(cache_path, "rb") as f:
            data = f.read()
        remote_db.upload_asset_bytes_remote(
            object_path, data, content_type="image/jpeg",
            source_item_id=item_id_safe, kind="video_poster",
        )
        asset_cache.put(object_path, data)  # B1: 清负缓存并落盘
        return data
    except HTTPException:
        return None
    finally:
        try:
            os.remove(cache_path)
        except OSError:
            pass


@router.get("/api/media/twitter-poster/{item_id}.jpg")
async def twitter_poster(item_id: str, request: Request):
    """Twitter 视频首帧封面. remote assets go to Supabase Storage.

    BF-0515-twitter-poster-prewarm: ensure_twitter_poster_cached() blocks on
    urllib (Supabase Storage download + ffmpeg subprocess). Run in threadpool
    so 20+ concurrent image requests don't serialize behind one event loop.

    BF-0515-image-etag: returns ETag + handles If-None-Match → 304.
    ETag is deterministic per item_id, checked BEFORE storage download,
    so a 304 response costs ~0ms (no network, no ffmpeg)."""
    item_id = _safe_item_id(item_id)
    etag = _make_etag("poster", item_id)
    if _check_if_none_match(request, etag):
        return _not_modified_response(etag)

    # BF-0515-twitter-image-perf: in-memory LRU hit avoids Storage roundtrip
    lru_key = ("poster", item_id)
    cached = _twitter_image_lru.get(lru_key)
    if cached is not None:
        data, ctype = cached
        return _image_response_with_etag(data, etag, media_type=ctype)

    if remote_db.asset_storage_to_remote():
        data = await run_in_threadpool(
            _run_media_cold_path,
            ("poster", item_id),
            ensure_twitter_poster_cached,
            item_id,
        )
        if data is not None:
            _twitter_image_lru.put(lru_key, data, "image/jpeg")
            return _image_response_with_etag(data, etag)
        raise HTTPException(status_code=502, detail="poster generation failed")

    cache_path = os.path.join(POSTER_DIR, f"{item_id}.jpg")

    if os.path.exists(cache_path) and os.path.getsize(cache_path) > 0:
        return FileResponse(
            cache_path,
            media_type="image/jpeg",
            headers={"Cache-Control": "public, max-age=86400"},
        )

    mp4_url = _get_twitter_mp4_url(item_id)
    _generate_poster_file(item_id, mp4_url, cache_path)

    return FileResponse(
        cache_path,
        media_type="image/jpeg",
        headers={"Cache-Control": "public, max-age=86400"},
    )


# ── GET /api/media/twitter-photo/{item_id}/{idx}.jpg ──────────────────
# BF-0515-twitter-image-proxy:
# Twitter pbs.twimg.com images fail in many user environments due to local
# proxy/MITM TLS interception (ERR_CERT_COMMON_NAME_INVALID) or network
# blocking. Backend proxy bypasses this — server-side urllib reaches Twitter
# CDN reliably and serves bytes back to browser without HTTPS roundtrip to
# pbs.twimg.com on the user's network path.

def _get_twitter_photo_url(item_id: str, idx: int) -> str:
    if remote_db.app_state_to_remote() or remote_db.feed_read_from_remote():
        item = remote_db.get_media_item_remote(item_id)
        if not item:
            raise HTTPException(status_code=404, detail="item not found")
        if item.get("platform") != "twitter":
            raise HTTPException(status_code=400, detail="not a twitter item")
        media = item.get("media_json") or []
        if isinstance(media, str):
            try:
                media = json.loads(media or "[]")
            except (ValueError, TypeError):
                raise HTTPException(status_code=404, detail="invalid media_json")
    else:
        conn = db.get_conn()
        try:
            row = conn.execute(
                "SELECT media_json, platform FROM items WHERE id = ?", (item_id,)
            ).fetchone()
        finally:
            conn.close()
        if not row:
            raise HTTPException(status_code=404, detail="item not found")
        if row["platform"] != "twitter":
            raise HTTPException(status_code=400, detail="not a twitter item")
        try:
            media = json.loads(row["media_json"] or "[]")
        except (ValueError, TypeError):
            raise HTTPException(status_code=404, detail="invalid media_json")

    if not isinstance(media, list):
        raise HTTPException(status_code=404, detail="invalid media_json")
    photos = [m for m in media if isinstance(m, dict) and m.get("type") == "photo" and m.get("url")]
    if not photos:
        raise HTTPException(status_code=404, detail="no photo in item")
    if idx < 0 or idx >= len(photos):
        raise HTTPException(status_code=404, detail="photo index out of range")
    url = photos[idx]["url"]
    if not url.startswith("https://pbs.twimg.com/"):
        raise HTTPException(status_code=400, detail="not a twitter cdn url")
    return url


def prewarm_recent_twitter_video_posters(limit: int = 10, days: int = 3) -> dict:
    """BF-0515-twitter-poster-prewarm: walk recent Twitter video items and
    pre-generate their video posters (uploaded to Supabase Storage).

    Skips items whose poster is already cached.
    Capped at `limit` per cycle so this can fit in the prewarm interval
    (each ffmpeg call takes 5-30s through clash → 10 items ≈ 1-5 min).

    Called from the periodic prewarm_loop in app.py."""
    if not remote_db.asset_storage_to_remote():
        return {"skipped": "local storage mode"}

    schema = remote_db.remote_schema()
    candidates: list[str] = []
    try:
        with remote_db.connect() as conn:
            rows = conn.execute(f"""
                SELECT i.id, i.media_json
                  FROM {schema}.items i
                 WHERE i.platform = 'twitter'
                   AND i.fetched_at > now() - interval '{int(days)} days'
                   AND i.visible = 1
                 ORDER BY i.fetched_at DESC
                 LIMIT 200
            """).fetchall()
    except Exception as exc:
        return {"error": str(exc)[:200]}

    for row in rows:
        if len(candidates) >= limit:
            break
        try:
            media = row.get("media_json")
            if isinstance(media, str):
                media = json.loads(media or "[]")
            if not isinstance(media, list):
                continue
            has_video = any(
                isinstance(m, dict) and m.get("type") == "video" and m.get("url")
                for m in media
            )
            if not has_video:
                continue
            object_path = f"video_posters/{row['id']}.jpg"
            if asset_cache.get_or_fetch(
                object_path,
                lambda object_path=object_path: remote_db.download_asset_bytes_remote(object_path),
            ):
                continue  # already cached(B1: 顺带暖盘)
            candidates.append(str(row["id"]))
        except Exception:
            pass

    generated = 0
    failed = 0
    t0 = time.time()
    for item_id in candidates:
        try:
            data = ensure_twitter_poster_cached(item_id)
            if data:
                generated += 1
            else:
                failed += 1
        except Exception:
            failed += 1
    return {
        "candidates_scanned": len(rows),
        "uncached_found": len(candidates),
        "generated": generated,
        "failed": failed,
        "elapsed_sec": int(time.time() - t0),
    }


def _twitter_cdn_opener() -> urllib.request.OpenerDirector:
    """Build a urllib opener for Twitter CDN. Twitter is GFW-blocked from
    mainland, so we need an explicit proxy. The dev script does NOT propagate
    HTTP_PROXY into the tmux session, and .env doesn't set it either, so we
    look at INFO2ACTION_PROXY_URL → HTTPS_PROXY → HTTP_PROXY in priority order.

    On dev mac: clash on 127.0.0.1:7897 (set INFO2ACTION_PROXY_URL in .env or
    rely on HTTP_PROXY in shell env and rerun dev-stack.sh).
    On ECS prod: clash on 127.0.0.1:7890 per `[server_clash_proxy]` memory.

    If no proxy resolved: fall back to default opener (will fail in mainland
    networks, used only when the network can reach Twitter directly)."""
    proxy_url = (
        os.environ.get("INFO2ACTION_PROXY_URL")
        or os.environ.get("HTTPS_PROXY")
        or os.environ.get("HTTP_PROXY")
    )
    if proxy_url:
        return urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url})
        )
    return urllib.request.build_opener()


def _fetch_twitter_photo_from_cdn(item_id: str, idx: int) -> tuple[bytes, str]:
    photo_url = _get_twitter_photo_url(item_id, idx)
    req = urllib.request.Request(photo_url, headers={"User-Agent": _UA})
    opener = _twitter_cdn_opener()
    try:
        with opener.open(req, timeout=_UPSTREAM_TIMEOUT) as resp:
            data = resp.read()
            content_type = resp.headers.get("Content-Type") or "image/jpeg"
    except HTTPError as exc:
        logger.warning("twitter-photo upstream %s err=%s", item_id, exc.code)
        raise HTTPException(status_code=502, detail=f"upstream {exc.code}")
    except URLError as exc:
        logger.warning("twitter-photo upstream %s err=%s", item_id, str(exc.reason)[:80])
        raise HTTPException(status_code=502, detail="upstream unreachable")

    # Best-effort Storage upload (failure non-fatal — user gets the image,
    # next request will retry the upload). This is deliberately part of the
    # threadpool cold path so slow Storage writes cannot block the event loop.
    if remote_db.asset_storage_to_remote():
        try:
            remote_db.upload_asset_bytes_remote(
                f"twitter_photos/{item_id}/{idx}.jpg",
                data,
                content_type="image/jpeg",
                source_item_id=item_id,
                kind="twitter_photo",
            )
            asset_cache.put(f"twitter_photos/{item_id}/{idx}.jpg", data)  # B1
        except Exception as exc:
            logger.warning("twitter-photo storage upload failed item=%s idx=%s err=%s", item_id, idx, str(exc)[:120])
    return data, content_type


@router.get("/api/media/twitter-photo/{item_id}/{idx}.jpg")
async def twitter_photo_proxy(item_id: str, idx: int, request: Request):
    """Server-side proxy for Twitter static photos (pbs.twimg.com).

    Storage cache pattern (same as twitter_poster):
    1. Try Supabase Storage object 'twitter_photos/{item_id}/{idx}.jpg'
    2. If miss: fetch from Twitter CDN via clash proxy, upload to Storage,
       return bytes
    3. Subsequent users hit Storage (~50-100ms vs 8-17s cold via clash)

    Twitter CDN access requires a Chrome UA (Python-urllib UA → 403) and
    in mainland needs a proxy (INFO2ACTION_PROXY_URL / HTTPS_PROXY).

    BF-0515-image-etag: returns ETag + handles If-None-Match → 304."""
    item_id = _safe_item_id(item_id)
    if idx < 0 or idx > 19:
        raise HTTPException(status_code=400, detail="idx out of range")

    # BF-0515-image-etag: check 304 before any expensive work
    etag = _make_etag("photo", item_id, idx)
    if _check_if_none_match(request, etag):
        return _not_modified_response(etag)

    # BF-0515-twitter-image-perf: in-memory LRU hit avoids Storage roundtrip.
    # Saves ~1.2s/img on warm requests (Storage cross-region RTT eliminated).
    lru_key = ("photo", item_id, idx)
    cached_mem = _twitter_image_lru.get(lru_key)
    if cached_mem is not None:
        data_mem, ctype_mem = cached_mem
        return _image_response_with_etag(data_mem, etag, media_type=ctype_mem)

    # Hot path: Supabase Storage cache hit. Run blocking urllib in threadpool
    # so 20+ concurrent image requests don't serialize behind one event loop.
    if remote_db.asset_storage_to_remote():
        object_path = f"twitter_photos/{item_id}/{idx}.jpg"
        # B1: 磁盘缓存 L2
        cached = await run_in_threadpool(
            asset_cache.get_or_fetch,
            object_path,
            lambda: remote_db.download_asset_bytes_remote(object_path),
        )
        if cached:
            _twitter_image_lru.put(lru_key, cached, "image/jpeg")
            return _image_response_with_etag(cached, etag)

    # Cold path: fetch from Twitter, upload to Storage, return. Run the whole
    # blocking path in a threadpool so image misses cannot stall core API routes.
    data, content_type = await run_in_threadpool(
        _run_media_cold_path,
        ("photo", item_id, idx),
        _fetch_twitter_photo_from_cdn,
        item_id,
        idx,
    )

    # Populate LRU so the very next request is a memory hit
    _twitter_image_lru.put(lru_key, data, content_type)
    return _image_response_with_etag(data, etag, media_type=content_type)


def _generate_poster_file(item_id: str, mp4_url: str, cache_path: str) -> None:
    """Generate a poster JPEG from a remote mp4 into `cache_path`."""
    try:
        result = subprocess.run(
            [
                _FFMPEG,
                "-hide_banner", "-loglevel", "error",
                "-user_agent", _UA,
                "-ss", "0",
                "-i", mp4_url,
                "-frames:v", "1",
                "-q:v", "4",
                "-y", cache_path,
            ],
            capture_output=True,
            timeout=_FFMPEG_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        try:
            os.remove(cache_path)
        except OSError:
            pass
        raise HTTPException(status_code=504, detail="ffmpeg timeout")

    if result.returncode != 0 or not os.path.exists(cache_path) or os.path.getsize(cache_path) == 0:
        try:
            os.remove(cache_path)
        except OSError:
            pass
        logger.warning("ffmpeg poster failed item=%s err=%s", item_id, result.stderr[:200])
        raise HTTPException(
            status_code=502,
            detail=f"ffmpeg failed: {result.stderr.decode('utf-8', errors='replace')[:200]}",
        )
