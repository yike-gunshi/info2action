#!/usr/bin/env python3
"""Info Radar — Ingest JSON data into SQLite.
Usage: python3 ingest.py [--run-id N] [--only-twitter-timeline]
Called by fetch_all.sh after data collection."""
import argparse
import json, math, os, sys, re, glob, hashlib, urllib.request, urllib.parse
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
import db
import remote_db

# Load config
with open(os.path.join(BASE, 'config', 'config.json')) as f:
    CONFIG = json.load(f)

XHS_FEED_FILTER = CONFIG["xiaohongshu"]["filter"]["feed"]
XHS_SEARCH_FILTER = CONFIG["xiaohongshu"]["filter"]["search"]
CURRENT_RUN_ID = None


def data_path(*parts):
    data_dir = os.environ.get('INFO2ACTION_DATA_DIR') or os.path.join(BASE, 'data')
    return os.path.join(data_dir, *parts)


def source_path(*parts):
    return data_path('sources', *parts)


def lingowhale_path(*parts):
    return data_path('lingowhale', *parts)


def batch_upsert_current_run(conn, items):
    """Upsert items and tag them with the active fetch run when present."""
    if remote_db.fetch_write_to_remote():
        return remote_db.batch_upsert_items_remote(None, items, fetch_run_id=CURRENT_RUN_ID)
    return db.batch_upsert(conn, items, fetch_run_id=CURRENT_RUN_ID)


def start_current_fetch_run(conn):
    """Create the active fetch run in the configured write backend."""
    if remote_db.fetch_write_to_remote():
        return remote_db.start_fetch_run_remote(None)
    return db.start_fetch_run(conn)


def finish_current_fetch_run(conn, run_id, stats, error=None):
    """Finish the active fetch run in the configured write backend."""
    if remote_db.fetch_write_to_remote():
        return remote_db.finish_fetch_run_remote(None, run_id, stats, error)
    return db.finish_fetch_run(conn, run_id, stats, error)


def upsert_item_current_backend(conn, item):
    """Upsert a single item through the configured ingest write backend."""
    if remote_db.fetch_write_to_remote():
        return remote_db.upsert_item_remote(None, item, fetch_run_id=CURRENT_RUN_ID)
    return db.upsert_item(conn, item)


def update_asr_fields_current_backend(conn, item_id: str, **fields):
    """Update ASR fields through the configured app-state backend."""
    if remote_db.app_state_to_remote():
        return remote_db.update_item_asr_fields_remote(item_id, **fields)
    allowed = {
        "asr_text",
        "asr_status",
        "asr_duration_sec",
        "asr_cost_yuan",
        "asr_attempted_at",
        "asr_failed_reason",
        "asr_provider",
        "asr_segments",
        "asr_text_cn",
        "asr_segments_cn",
    }
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return
    sets = ", ".join(f"{k}=?" for k in updates)
    conn.execute(f"UPDATE items SET {sets} WHERE id=?", list(updates.values()) + [item_id])
    conn.commit()


def open_current_backend_conn():
    """Open SQLite only when ingest is actually writing to SQLite."""
    if remote_db.fetch_write_to_remote():
        return None
    return db.get_conn()


def now_ts():
    """Return current timestamp so each item gets a unique fetched_at.

    Returns a tz-aware UTC ISO string (e.g. '2026-05-18T01:48:00.123+00:00').
    Naive datetime.now() previously returned the server's local time and
    broke after the host migrated from Beijing (UTC+8) to Tokyo (UTC+9):
    time_utils.parse_datetime treats naive timestamps as UTC+8
    (LOCAL_NAIVE_TZ), which shifted every fetched_at +1 hour into the
    future on Tokyo.
    """
    return datetime.now(timezone.utc).isoformat()


def safe_load_json(path):
    """Load JSON file, return None if empty or invalid."""
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        return None
    try:
        with open(path) as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        return None


def parse_count(s):
    """Parse Chinese number strings like '1.2万' → 12000."""
    s = str(s or '0').strip()
    m = re.match(r'([\d.]+)\s*万', s)
    if m:
        return int(float(m.group(1)) * 10000)
    try:
        return int(s)
    except (ValueError, TypeError):
        return 0


def xhs_feed_passes_filter(title, desc):
    cfg = XHS_FEED_FILTER
    if not cfg["enabled"]:
        return True
    text = (title + " " + desc).lower()
    for kw in cfg["keywords_exclude"]:
        if kw.lower() in text:
            return False
    for kw in cfg["keywords_include"]:
        if kw.lower() in text:
            return True
    return False


def xhs_search_passes_filter(interact):
    cfg = XHS_SEARCH_FILTER
    if not cfg["enabled"]:
        return True
    likes = parse_count(interact.get('liked_count', '0'))
    collects = parse_count(interact.get('collected_count', '0'))
    comments_str = interact.get('comment_count', '0') or '0'
    comments_val = int(comments_str) if str(comments_str).isdigit() else 0
    total = likes + collects + comments_val
    return likes >= cfg["min_likes"] and total >= cfg["min_total_engagement"]


def calc_relevance(title, content, metrics, platform):
    """Calculate a simple relevance score based on keyword hits + engagement."""
    text = ((title or '') + ' ' + (content or '')).lower()
    keywords = CONFIG['global']['search_keywords'] + XHS_FEED_FILTER.get('keywords_include', [])
    keyword_hits = sum(1 for kw in keywords if kw.lower() in text)

    if isinstance(metrics, str):
        try:
            metrics = json.loads(metrics)
        except (json.JSONDecodeError, TypeError):
            metrics = {}
    if not isinstance(metrics, dict):
        metrics = {}

    engagement = 0
    for key in ['likes', 'liked_count', 'like', 'views', 'view', 'retweets', 'bookmarks']:
        engagement += parse_count(metrics.get(key, 0))

    # Normalize: keyword_hits * 10 + log-scale engagement
    score = keyword_hits * 10 + math.log1p(engagement) * 2
    return round(score, 2)


# ============================================================
# TWITTER
# ============================================================
_TWITTER_CLI = os.environ.get('TWITTER_CLI', os.path.expanduser('~/.local/bin/twitter'))


def _twitter_cover_url(tid, media):
    # BF-0420-20: video/animated_gif 帖必须有非空 cover_url，否则前端 fallback 乱套
    # 走后端 ffmpeg 懒抽帧路由（/api/media/twitter-poster/<tid>.jpg，cache 永久）
    if not media or not isinstance(media, list):
        return None
    first = media[0]
    if not isinstance(first, dict):
        return None
    t = first.get('type')
    if t == 'photo':
        return first.get('url') or None
    if t in ('video', 'animated_gif'):
        return f'/api/media/twitter-poster/{tid}.jpg'
    return None


def _is_x_article_tweet(text, urls):
    """X article 推文：text 仅包含一个 t.co 短链，urls 含 /i/article/。"""
    if not urls:
        return False
    text = (text or '').strip()
    if not text.startswith('https://t.co/') or ' ' in text or '\n' in text:
        return False
    return any('/i/article/' in u for u in urls)


def _expand_x_article(tweet_id):
    """调用 twitter CLI 抓 article 正文。返回 (title, text) 或 (None, None)。"""
    if not os.path.exists(_TWITTER_CLI):
        return None, None
    try:
        import subprocess
        result = subprocess.run(
            [_TWITTER_CLI, 'tweet', tweet_id, '--json'],
            capture_output=True, text=True, timeout=20,
        )
        if result.returncode != 0:
            return None, None
        data = json.loads(result.stdout)
        if not data.get('ok'):
            return None, None
        items = data.get('data', [])
        if not items:
            return None, None
        first = items[0] if isinstance(items, list) else items
        title = first.get('articleTitle')
        text = first.get('articleText')
        if title or text:
            return title, text
    except Exception as e:
        print(f"  ⚠️  X article expand failed for {tweet_id}: {e}")
    return None, None


def ingest_twitter(conn, *, timeline_only=False):
    """Ingest all Twitter JSON files."""
    mapping = {
        '1-following-feed.json': 'following',
        '2-for-you-feed.json': 'for_you',
    }
    if not timeline_only:
        mapping.update({
            '4-bookmarks.json': 'bookmarks',
        })
        # v16.0: keyword search is retired. Keep historical search files on disk,
        # but do not ingest them into new fetch runs.
    total = 0
    video_tasks: list[tuple[str, str]] = []  # v12.3 N4: (tid, mp4_url) for inline ffmpeg poster
    for fname, source in mapping.items():
        path = source_path('twitter', fname)
        data = safe_load_json(path)
        if data is None:
            continue
        tweets = data if isinstance(data, list) else data.get('data', [])

        items = []
        for t in tweets:
            tid = t.get('id', '')
            if not tid:
                continue
            author = t.get('author', {})
            metrics = t.get('metrics', {})
            text = t.get('text', '')
            media = t.get('media', [])
            lang = t.get('lang', '')

            url = f"https://x.com/{author.get('screenName', '_')}/status/{tid}"
            metrics_dict = {
                'likes': metrics.get('likes', 0),
                'retweets': metrics.get('retweets', 0),
                'views': metrics.get('views', 0),
                'bookmarks': metrics.get('bookmarks', 0),
                'replies': metrics.get('replies', 0),
            }
            metrics_json = json.dumps(metrics_dict, ensure_ascii=False)

            # Build detail with quoted tweet and retweet info
            detail = {}
            if t.get('quotedTweet'):
                detail['quotedTweet'] = t['quotedTweet']
            if t.get('isRetweet'):
                detail['isRetweet'] = True
                detail['retweetedBy'] = t.get('retweetedBy', '')
            urls = t.get('urls') or []
            if urls:
                detail['urls'] = urls

            article_title = t.get('articleTitle')
            article_text = t.get('articleText')
            if not article_text and _is_x_article_tweet(text, urls):
                # text 只是 t.co 短链，调用 CLI 展开 article 正文
                article_title, article_text = _expand_x_article(tid)
            if article_text:
                # 使用 article 正文作为 content；title 用 articleTitle 优先
                content_for_db = article_text
                title_for_db = article_title or (text[:80] if text else None)
                detail['articleTitle'] = article_title
                detail['isXArticle'] = True
            else:
                content_for_db = text
                title_for_db = text[:80] if text else None

            items.append({
                'id': tid,
                'platform': 'twitter',
                'source': source,
                'title': title_for_db,
                'content': content_for_db,
                'author_name': author.get('name', ''),
                'author_id': author.get('id', ''),
                'author_avatar': author.get('profileImageUrl', ''),
                'url': url,
                'cover_url': _twitter_cover_url(tid, media),
                'media_json': json.dumps(media, ensure_ascii=False) if media else None,
                'metrics_json': metrics_json,
                'tags_json': None,
                'lang': lang,
                'detail_json': json.dumps(detail, ensure_ascii=False) if detail else None,
                'comments_json': None,
                'ai_summary': None,
                'relevance_score': calc_relevance(content_for_db, '', metrics_dict, 'twitter'),
                'fetched_at': now_ts(),
                # Prefer tz-aware createdAt over naive createdAtLocal.
                # createdAtLocal is generated by twitter-cli using the server's
                # local time (JST on Tokyo), but time_utils.parse_datetime
                # treats naive strings as UTC+8 (LOCAL_NAIVE_TZ) — so
                # createdAtLocal would shift published_at +1h on Tokyo and
                # break cluster.first_doc_at (which is MIN over published_at).
                'published_at': t.get('createdAt') or t.get('createdAtLocal') or '',
            })
            video_mp4 = next((m.get('url') for m in media
                              if isinstance(m, dict) and m.get('type') == 'video' and m.get('url')),
                             None)
            if video_mp4:
                video_tasks.append((tid, video_mp4))
        if items:
            batch_upsert_current_run(conn, items)
            total += len(items)
            print(f"  ✅ Twitter {source}: {len(items)} items")

    _extract_twitter_posters_inline(video_tasks)
    # v13.0 F52: ingest 时预跑 ASR(仅对新视频;已 ASR 过的 item 跳过)
    _run_asr_for_twitter_videos_inline(conn, [tid for tid, _ in video_tasks])
    return total


def _run_asr_for_twitter_videos_inline(conn, tweet_ids: list[str]) -> None:
    """v13.0 F52:ingest 结尾对含视频的 tweet 并发跑 ASR。

    - 只对 `asr_status IS NULL`(未跑过 / 新入库)的 item 触发,避免重复消费配额
    - 并发池 N = env `ASR_INGEST_CONCURRENCY`(默认 5)
    - 单任务 ThreadPoolExecutor timeout = 900s 硬上限(运行期内部按 duration 自适应)
    - 失败不阻塞其他任务(future 各自独立);timeout → 任务仍在跑但我们不等,不写 DB 状态
      (worker 会把最后状态写入 items 表,下一次 ingest 会看到 asr_status 非空就 skip)
    - 环境缺 DOUBAO_ASR_API_KEY → 静默跳过整批(本地开发 / CI 无 ASR 密钥场景)

    注意: 不 re-raise,保持 cron 稳定;所有错误仅 log。
    """
    if not tweet_ids:
        return
    # 2026-04-28: 永久关闭 ingest 期 ASR(用户决策;MiniMax/豆包 API key 持续 401 卡死 ingest)
    # 恢复时改成 INGEST_SKIP_ASR='0' 或删除本块。日志里要看到 SKIP 才算闭环。
    if os.environ.get('INGEST_SKIP_ASR', '1') == '1':
        print(f"  🎙️  ASR ingest: SKIP {len(tweet_ids)} videos (INGEST_SKIP_ASR=1)",
              flush=True)
        return
    if not os.environ.get('DOUBAO_ASR_API_KEY'):
        print(f"  🎙️  ASR ingest: SKIP {len(tweet_ids)} videos (DOUBAO_ASR_API_KEY not set)",
              flush=True)
        return
    if conn is None:
        print(f"  🎙️  ASR ingest: SKIP {len(tweet_ids)} videos (remote fetch writer)",
              flush=True)
        return

    # 过滤出"需要跑"的 tweet(asr_status IS NULL)
    placeholders = ','.join('?' * len(tweet_ids))
    rows = conn.execute(
        f"SELECT id FROM items WHERE id IN ({placeholders}) AND asr_status IS NULL",
        tweet_ids,
    ).fetchall()
    pending = [r['id'] for r in rows]
    if not pending:
        print(f"  🎙️  ASR ingest: all {len(tweet_ids)} videos already processed, skip", flush=True)
        return

    concurrency = int(os.environ.get('ASR_INGEST_CONCURRENCY', '5'))
    # 单任务硬上限 15min(ingest 场景不允许单任务拖 30min)
    # 豆包 poll 自己也有 900s 超时;再套 ThreadPoolExecutor future.result(timeout)
    single_task_max_sec = 900

    print(f"  🎙️  ASR ingest: running {len(pending)} videos, concurrency={concurrency}",
          flush=True)

    import asr_worker
    succ = failed = timeout_ct = skipped = 0

    def _one(tid: str) -> tuple[str, str]:
        # 每个 future 自己的 DB connection(SQLite thread-safe 不共享 conn)
        c = db.get_conn()
        try:
            r = asr_worker.run_asr_inline(tid, bypass_quota=False, conn=c,
                                          max_wait_sec=single_task_max_sec)
            return (tid, r.status)
        except Exception as e:  # noqa: BLE001
            return (tid, f"exception:{type(e).__name__}:{str(e)[:80]}")
        finally:
            try: c.close()
            except Exception: pass

    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = {pool.submit(_one): None for _ in []}  # placeholder
        futures = {pool.submit(_one, tid): tid for tid in pending}
        for fu in as_completed(futures, timeout=None):
            tid = futures[fu]
            try:
                _tid, status = fu.result(timeout=single_task_max_sec + 30)
            except Exception as e:  # noqa: BLE001
                timeout_ct += 1
                print(f"    ⏱️  ASR {tid}: timeout / exception:{type(e).__name__}",
                      flush=True)
                # 超时的 item 在 DB 中可能卡在 running,写 failed_asr 兜底
                try:
                    conn.execute(
                        "UPDATE items SET asr_status='failed_asr', "
                        "asr_failed_reason='ingest_timeout_900s' "
                        "WHERE id=? AND asr_status='running'",
                        (tid,),
                    )
                    conn.commit()
                except Exception:
                    pass
                continue
            if status == "success":
                succ += 1
            elif status == "skipped_quota":
                skipped += 1
            else:
                failed += 1
                print(f"    ⚠️  ASR {tid}: {status}", flush=True)
    print(f"  🎙️  ASR ingest done: +{succ} 成功 / {skipped} 配额跳过 / "
          f"{failed} 失败 / {timeout_ct} 超时 (并发 {concurrency})",
          flush=True)


# ============================================================
# v13.0 F52: YOUTUBE 手动上传 — 字幕优先 + yt-dlp 音频 fallback
# ============================================================

# v13.0 常量
YT_PROXY = 'http://127.0.0.1:7890'  # Clash 代理(国内访问 YouTube 必需)
YT_MP3_DIR = '/tmp'


def _yt_proxy_if_available():
    """BF-0419-16 rev2: 代理端口可探测就用 HTTP 代理,否则 None(走系统路由 / TUN)。

    Clash Verge TUN 模式不监听 7890,走虚拟网卡接管所有流量 → 不应强制 proxy。
    """
    import socket
    try:
        s = socket.create_connection(('127.0.0.1', 7890), timeout=0.8)
        s.close()
        return YT_PROXY
    except (OSError, socket.timeout):
        return None
_YT_ZH_LANG_PREFIX = 'zh'           # zh / zh-CN / zh-Hans / zh-TW 都判中文


def _youtube_build_segments_from_cues(cues: list) -> list:
    """youtube_transcript_api 的 [{text, start, duration}] → segments[{start_ms, end_ms, text}]"""
    segs = []
    for c in cues or []:
        # v1.x/v0.6 兼容:字典 / 对象 / dataclass 都可能出现
        if isinstance(c, dict):
            text = (c.get('text') or '').strip()
            start = c.get('start') or 0.0
            dur = c.get('duration') or 0.0
        else:
            text = (getattr(c, 'text', '') or '').strip()
            start = getattr(c, 'start', 0.0) or 0.0
            dur = getattr(c, 'duration', 0.0) or 0.0
        if not text:
            continue
        segs.append({
            'start_ms': int(start * 1000),
            'end_ms': int((start + dur) * 1000),
            'text': text,
        })
    return segs


def _youtube_fetch_metadata(url: str) -> dict:
    """yt-dlp extract_info(download=False) — 拿 title/duration/uploader/thumbnail。"""
    import yt_dlp
    ydl_opts = {
        'quiet': True,
        'no_warnings': True,
        'skip_download': True,
        'extract_flat': False,
        'socket_timeout': 30,
    }
    # BF-0419-16 rev2: 代理可选 — TUN 模式(无 7890 监听)走系统路由
    proxy = _yt_proxy_if_available()
    if proxy:
        ydl_opts['proxy'] = proxy
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        return ydl.extract_info(url, download=False) or {}


def _youtube_download_audio(url: str, video_id: str) -> str:
    """用 yt-dlp 下载音频为 mp3,返回本地路径。抛异常时由 caller 处理降级。"""
    import yt_dlp
    out_tmpl = f"{YT_MP3_DIR}/yt_{video_id}.%(ext)s"
    ydl_opts = {
        'quiet': True,
        'no_warnings': True,
        'format': 'bestaudio/best',
        'outtmpl': out_tmpl,
        'socket_timeout': 60,
        'retries': 1,
        'postprocessors': [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': '128',
        }],
    }
    proxy = _yt_proxy_if_available()
    if proxy:
        ydl_opts['proxy'] = proxy
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.download([url])
    expected = f"{YT_MP3_DIR}/yt_{video_id}.mp3"
    if not os.path.exists(expected):
        raise RuntimeError(f"yt-dlp mp3 not found at {expected}")
    return expected


def _youtube_try_transcript(video_id: str) -> tuple[list, str] | tuple[None, None]:
    """尝试抓 YouTube 字幕。返回 (segments, lang_code) 或 (None, None)。

    优先级:英文 → 其他语言 → None(caller 降级 ASR fallback)。
    字幕语言决定后续是否走 MiniMax 翻译(中文不翻,英文和其他语言翻)。
    """
    try:
        from youtube_transcript_api import YouTubeTranscriptApi
        from youtube_transcript_api._errors import TranscriptsDisabled, NoTranscriptFound
    except ImportError:
        print("[yt] youtube-transcript-api not installed", flush=True)
        return None, None

    # BF-0419-16 rev2: 代理可选(TUN 模式 proxies={} 让 requests 走系统路由)
    _proxy = _yt_proxy_if_available()
    proxies = {'http': _proxy, 'https': _proxy} if _proxy else {}
    try:
        # 试图拿英文字幕
        try:
            # v1.x API: YouTubeTranscriptApi().fetch(video_id, languages=[...])
            api = YouTubeTranscriptApi()
            try:
                fetched = api.fetch(video_id, languages=['en', 'en-US', 'en-GB'])
                cues = list(fetched)
                lang = 'en'
            except Exception:
                # 拿不到英文 → 试默认(auto-detect)
                fetched = api.fetch(video_id)
                cues = list(fetched)
                lang = getattr(fetched, 'language_code', '') or 'en'
        except AttributeError:
            # v0.6 API: 兼容旧路径
            try:
                cues = YouTubeTranscriptApi.get_transcript(
                    video_id, languages=['en', 'en-US', 'en-GB'], proxies=proxies,
                )
                lang = 'en'
            except Exception:
                transcripts = YouTubeTranscriptApi.list_transcripts(video_id, proxies=proxies)
                # 遍历可用语言选第一个
                for t in transcripts:
                    cues = t.fetch()
                    lang = t.language_code
                    break
                else:
                    return None, None
        segs = _youtube_build_segments_from_cues(cues)
        if not segs:
            return None, None
        return segs, lang
    except TranscriptsDisabled:
        print(f"[yt] transcripts disabled for {video_id}", flush=True)
        return None, None
    except NoTranscriptFound:
        print(f"[yt] no transcript found for {video_id}", flush=True)
        return None, None
    except Exception as e:
        print(f"[yt] transcript fetch failed: {type(e).__name__}: {str(e)[:120]}",
              flush=True)
        return None, None


def ingest_youtube_url(conn, item_id: str, url: str) -> dict:
    """v13.0 F52: YouTube 手动上传管线(字幕优先 + ASR fallback)。

    - item_id 形如 `yt_{video_id}`(由 url_normalize 生成)
    - url 是 canonical_url `https://www.youtube.com/watch?v={video_id}`
    - 返回 {status, item_id, title, asr_status, asr_provider, ...}
    - 失败时 status='error',item **不入库**(避免脏数据)

    字幕语言判定规则:
      - lang.startswith('zh') → 中文原声,`asr_segments_cn=NULL` 不翻译
      - 其他语言 → 调 translate_segments_cn 翻中文
      - 空字幕 → yt-dlp 下载音频 + run_asr_inline 降级
    """
    import asr_worker

    video_id = item_id.replace('yt_', '', 1)

    # 1. metadata(失败即中止,不建 item)
    try:
        meta = _youtube_fetch_metadata(url)
    except Exception as e:  # noqa: BLE001
        # BF-0419-16: 代理失败替换为用户可读的提示
        err_str = str(e)
        err_lower = err_str.lower()
        if 'unable to connect to proxy' in err_lower or 'proxyerror' in err_lower or 'newconnectionerror' in err_lower:
            friendly = 'YouTube 访问需要代理:请开启 Clash (127.0.0.1:7890) 后重试'
        elif 'sign in' in err_lower or 'age-restricted' in err_lower:
            friendly = '该 YouTube 视频需要登录或有年龄限制,暂不支持'
        elif 'video unavailable' in err_lower or 'private video' in err_lower:
            friendly = '该 YouTube 视频不可用或已设为私有'
        else:
            friendly = f'YouTube 元数据抓取失败:{type(e).__name__}({err_str[:150]})'
        return {'status': 'error', 'error': friendly}

    title = meta.get('title') or meta.get('fulltitle') or url
    duration = int(meta.get('duration') or 0)
    uploader = meta.get('uploader') or meta.get('channel') or ''
    thumbnail = meta.get('thumbnail') or ''

    # 2. 字幕优先
    segs, lang = _youtube_try_transcript(video_id)
    asr_text = None
    asr_segments_json = None
    asr_segments_cn = None
    asr_provider = None
    asr_status = None
    asr_duration_sec = duration

    if segs:
        # 组装整段 asr_text
        asr_text = '\n'.join(s['text'] for s in segs)
        asr_segments_json = json.dumps(segs, ensure_ascii=False)
        if (lang or '').lower().startswith(_YT_ZH_LANG_PREFIX):
            # 中文原声:不翻译
            asr_provider = 'youtube_transcript_api'
            asr_status = 'success'
        else:
            # 英文/其他语言 → MiniMax 段级翻译
            try:
                cn_list = asr_worker.translate_segments_cn(segs)
            except Exception as e:  # noqa: BLE001
                print(f"[yt] translate_segments_cn failed: {e}", flush=True)
                cn_list = None
            if cn_list and len(cn_list) == len(segs):
                asr_segments_cn = json.dumps(cn_list, ensure_ascii=False)
            asr_provider = 'youtube_transcript_api+minimax'
            asr_status = 'success'

    # 3. 无字幕 → yt-dlp 下载音频 + run_asr_inline fallback
    if not segs:
        # 先把 item 落库(metadata 已有),占位 asr_status='running',
        # 再调 run_asr_inline(后者会再更新 asr_status=success / failed_*)
        upsert_payload = {
            'id': item_id,
            'platform': 'youtube',
            'source': 'user-submit',
            'title': title,
            'content': title,  # 只有 metadata,content 留空会影响老后端 fallback
            'author_name': uploader,
            'url': url,
            'cover_url': thumbnail,
            'media_json': None,
            'fetched_at': now_ts(),
            'published_at': now_ts(),
        }
        upsert_item_current_backend(conn, upsert_payload)
        # 填 duration(provider 让 run_asr_inline 决定)
        if duration:
            update_asr_fields_current_backend(conn, item_id, asr_duration_sec=duration)

        # yt-dlp 下载音频
        local_mp3 = None
        try:
            local_mp3 = _youtube_download_audio(url, video_id)
        except Exception as e:  # noqa: BLE001
            reason = f'yt-dlp download failed: {type(e).__name__}: {str(e)[:200]}'
            update_asr_fields_current_backend(
                conn,
                item_id,
                asr_status='failed_download',
                asr_failed_reason=reason[:200],
            )
            return {'status': 'error', 'error': reason, 'item_id': item_id}

        try:
            r = asr_worker.run_asr_inline(
                item_id,
                bypass_quota=False,  # 配额检查(ingest 路径)
                conn=conn,
                audio_source={'local_mp3': local_mp3},
                max_wait_sec=min(max(duration * 3 + 60, 120), 900) if duration else 900,
            )
            asr_status = r.status
            asr_provider = 'doubao-seedasr-bigmodel'
            asr_duration_sec = r.duration_sec or duration
        finally:
            try:
                if local_mp3 and os.path.exists(local_mp3):
                    os.remove(local_mp3)
            except OSError:
                pass

        return {
            'status': 'ok' if asr_status == 'success' else 'partial',
            'item_id': item_id,
            'title': title,
            'asr_status': asr_status,
            'asr_provider': asr_provider,
            'duration_sec': asr_duration_sec,
        }

    # 字幕路径:一次性写所有字段
    upsert_item_current_backend(conn, {
        'id': item_id,
        'platform': 'youtube',
        'source': 'user-submit',
        'title': title,
        'content': asr_text,  # 有字幕的话,content 就是 transcript(后续 generate_summaries 用)
        'author_name': uploader,
        'url': url,
        'cover_url': thumbnail,
        'media_json': None,
        'fetched_at': now_ts(),
        'published_at': now_ts(),
    })
    update_asr_fields_current_backend(
        conn,
        item_id,
        asr_text=asr_text,
        asr_segments=asr_segments_json,
        asr_segments_cn=asr_segments_cn,
        asr_duration_sec=asr_duration_sec,
        asr_provider=asr_provider,
        asr_status=asr_status,
        asr_cost_yuan=0,
    )
    return {
        'status': 'ok',
        'item_id': item_id,
        'title': title,
        'asr_status': asr_status,
        'asr_provider': asr_provider,
        'duration_sec': asr_duration_sec,
        'lang': lang,
    }


_POSTER_DIR = os.path.join(BASE, 'data', 'images', 'video_posters')
_POSTER_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
              "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")


def _extract_one_poster(task: tuple[str, str]) -> tuple[str, str]:
    """v12.3 N4 rev2: subprocess ffmpeg 抽 mp4 首帧. 返回 (tid, status)."""
    tid, mp4_url = task
    cache_path = os.path.join(_POSTER_DIR, f'{tid}.jpg')
    if os.path.exists(cache_path) and os.path.getsize(cache_path) > 0:
        return (tid, 'cached')
    import subprocess
    os.makedirs(_POSTER_DIR, exist_ok=True)
    try:
        r = subprocess.run(
            ['ffmpeg', '-hide_banner', '-loglevel', 'error',
             '-user_agent', _POSTER_UA,
             '-ss', '0', '-i', mp4_url,
             '-frames:v', '1', '-q:v', '4',
             '-y', cache_path],
            capture_output=True, timeout=20,
        )
        if r.returncode == 0 and os.path.exists(cache_path) and os.path.getsize(cache_path) > 0:
            return (tid, 'warmed')
        try: os.remove(cache_path)
        except OSError: pass
        return (tid, f'rc={r.returncode}')
    except subprocess.TimeoutExpired:
        try: os.remove(cache_path)
        except OSError: pass
        return (tid, 'timeout')
    except Exception as e:
        return (tid, f'err:{type(e).__name__}')


def _extract_twitter_posters_inline(tasks: list[tuple[str, str]], workers: int = 3) -> None:
    """v12.3 N4 rev2: ingest 结尾并发抽 poster, 串行等完. 失败仅打日志, 不阻断 ingest.

    相比原 fire-and-forget 方案(调 /api/media/twitter-poster HTTP):
      - 不压 uvicorn event loop(后端 ASR/搜索/弹窗不受影响)
      - 入库完成瞬间所有新卡片都有封面,前端永不黑屏
      - cache miss 兜底仍保留在 routes/media.py, 极少触发
    """
    if not tasks:
        return
    cached = warmed = failed = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for tid, status in pool.map(_extract_one_poster, tasks):
            if status == 'cached': cached += 1
            elif status == 'warmed': warmed += 1
            else:
                failed += 1
                if failed <= 3:
                    print(f"    ⚠️  poster {tid}: {status}", flush=True)
    print(f"  🎞️  Twitter posters: +{warmed} 抽帧 / {cached} 已缓存 / {failed} 失败 (并发 {workers})",
          flush=True)


# ============================================================
# XIAOHONGSHU
# ============================================================
def ingest_xiaohongshu(conn):
    """Ingest all XHS JSON files."""
    if not CONFIG.get('xiaohongshu', {}).get('enabled', False):
        return 0
    total = 0

    # 1. Recommend feed (combined format with details + comments)
    path = source_path('xiaohongshu', '1-recommend-feed.json')
    data = safe_load_json(path)
    if data is not None:
        raw_items = data.get('items', []) if isinstance(data, dict) else data
        items = []
        for item in raw_items:
            row = _xhs_item_to_row(item, 'recommend')
            if row is None:
                continue
            # Apply feed filter
            if not xhs_feed_passes_filter(row['title'] or '', row['content'] or ''):
                continue
            items.append(row)
        if items:
            batch_upsert_current_run(conn, items)
            total += len(items)
            print(f"  ✅ XHS recommend: {len(items)} items (filtered from {len(raw_items)})")

    # v16.0: keyword search is retired for all platforms. Historical
    # xiaohongshu/search-*.json files are intentionally ignored.

    # 3. Hot
    path = source_path('xiaohongshu', '3-hot.json')
    if os.path.exists(path) and os.path.getsize(path) > 0:
        with open(path) as f:
            data = json.load(f)
        raw_items = data.get('data', {}).get('items', []) if isinstance(data, dict) else data
        items = [r for r in (_xhs_item_to_row(i, 'hot') for i in raw_items) if r]
        if items:
            batch_upsert_current_run(conn, items)
            total += len(items)
            print(f"  ✅ XHS hot: {len(items)} items")

    # 4. Favorites
    path = source_path('xiaohongshu', 'favorites.json')
    if os.path.exists(path) and os.path.getsize(path) > 0:
        with open(path) as f:
            data = json.load(f)
        raw_items = data.get('data', {}).get('items', []) if isinstance(data, dict) else (data if isinstance(data, list) else [])
        items = [r for r in (_xhs_item_to_row(i, 'favorites') for i in raw_items) if r]
        if items:
            batch_upsert_current_run(conn, items)
            total += len(items)
            print(f"  ✅ XHS favorites: {len(items)} items")

    return total


def _xhs_item_to_row(item, source):
    """Convert a single XHS item (any format) to a db row dict."""
    note_id = item.get('id', '')
    if not note_id:
        return None

    # Prefer detail over feed_card
    nc = item.get('detail', {}) or item.get('note_card', {}) or item.get('feed_card', {})
    feed_card = item.get('feed_card', {})
    title = nc.get('title', '') or nc.get('display_title', '') or feed_card.get('display_title', '')
    desc = nc.get('desc', '')
    user = nc.get('user', {})
    interact = nc.get('interact_info', {})

    # Cover image
    cover = nc.get('cover', {})
    cover_url = cover.get('url_default', '') or cover.get('url_pre', '')
    if not cover_url:
        for info in cover.get('info_list', []):
            if info.get('image_scene') in ('FD_WM_WEBP', 'WB_DFT'):
                cover_url = info['url']
                break

    # All images
    image_list = nc.get('image_list', [])
    image_urls = []
    for img in image_list:
        img_url = img.get('url_default', '')
        if not img_url:
            for info in img.get('info_list', []):
                if info.get('image_scene') in ('WB_DFT', 'FD_WM_WEBP'):
                    img_url = info['url']
                    break
        if img_url:
            image_urls.append(img_url)

    # Tags
    tag_list = nc.get('tag_list', [])
    tags = [t.get('name', '') for t in tag_list if t.get('name')]

    # Metrics
    metrics = {
        'likes': parse_count(interact.get('liked_count', '0')),
        'collects': parse_count(interact.get('collected_count', '0')),
        'comments': parse_count(interact.get('comment_count', '0')),
    }

    # Comments
    comments = item.get('comments', [])

    url = f"https://www.xiaohongshu.com/explore/{note_id}"
    metrics_json = json.dumps(metrics, ensure_ascii=False)

    return {
        'id': note_id,
        'platform': 'xiaohongshu',
        'source': source,
        'title': title or None,
        'content': desc or None,
        'author_name': user.get('nickname', '') or user.get('nick_name', ''),
        'author_id': user.get('user_id', ''),
        'author_avatar': user.get('avatar', ''),
        'url': url,
        'cover_url': cover_url or (image_urls[0] if image_urls else None),
        'media_json': json.dumps(image_urls, ensure_ascii=False) if image_urls else None,
        'metrics_json': metrics_json,
        'tags_json': json.dumps(tags, ensure_ascii=False) if tags else None,
        'lang': 'zh',
        'detail_json': json.dumps(nc, ensure_ascii=False, default=str) if nc else None,
        'comments_json': json.dumps(comments, ensure_ascii=False) if comments else None,
        'ai_summary': None,
        'relevance_score': calc_relevance(title, desc, metrics, 'xiaohongshu'),
        'fetched_at': now_ts(),
        'published_at': datetime.fromtimestamp(int(nc['time']) / 1000, tz=timezone.utc).isoformat() if nc.get('time') else None,
    }


# ============================================================
# BILIBILI
# ============================================================
def ingest_bilibili(conn):
    """Ingest all Bilibili JSON files."""
    total = 0

    # 1. Feed (dynamics)
    path = source_path('bilibili', '1-feed.json')
    data = safe_load_json(path)
    if data is not None:
        raw_items = data.get('data', {}).get('items', []) if isinstance(data, dict) else data
        items = [r for r in (_bili_item_to_row(i, 'feed') for i in raw_items) if r]
        if items:
            batch_upsert_current_run(conn, items)
            total += len(items)
            print(f"  ✅ Bili feed: {len(items)} items")

    # 2. UP主 videos
    for f_path in sorted(glob.glob(source_path('bilibili', 'up-*.json'))):
        name = os.path.basename(f_path).replace('up-', '').replace('.json', '')
        data = safe_load_json(f_path)
        if data is None:
            continue
        raw_items = _extract_bili_items(data)
        items = [r for r in (_bili_item_to_row(i, f'up:{name}') for i in raw_items) if r]
        if items:
            batch_upsert_current_run(conn, items)
            total += len(items)

    up_count = len(glob.glob(source_path('bilibili', 'up-*.json')))
    print(f"  ✅ Bili UP主: {up_count} creators")

    # 3. Hot + Rank
    for fname, source in [('3-hot.json', 'hot'), ('4-rank.json', 'rank')]:
        path = source_path('bilibili', fname)
        data = safe_load_json(path)
        if data is None:
            continue
        raw_items = _extract_bili_items(data)
        items = [r for r in (_bili_item_to_row(i, source) for i in raw_items) if r]
        if items:
            batch_upsert_current_run(conn, items)
            total += len(items)
            print(f"  ✅ Bili {source}: {len(items)} items")

    # v16.0: keyword search is retired for all platforms. Historical
    # bilibili/search-*.json files are intentionally ignored.

    # 5. Watch later + History
    for fname, source in [('watch-later.json', 'watch_later'), ('history.json', 'history')]:
        path = source_path('bilibili', fname)
        data = safe_load_json(path)
        if data is None:
            continue
        raw_items = _extract_bili_items(data)
        items = [r for r in (_bili_item_to_row(i, source) for i in raw_items) if r]
        if items:
            batch_upsert_current_run(conn, items)
            total += len(items)
            print(f"  ✅ Bili {source}: {len(items)} items")

    return total


def _extract_bili_items(data):
    """Extract items list from various Bilibili JSON formats."""
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        d = data.get('data', data)
        if isinstance(d, list):
            return d
        if isinstance(d, dict):
            return d.get('items', d.get('result', []))
    return []


def _bili_item_to_row(item, source):
    """Convert a single Bilibili item to a db row dict."""
    bvid = item.get('bvid', '') or str(item.get('id', ''))
    if not bvid:
        return None

    title = item.get('title', '')
    # Clean HTML tags from search results
    title = re.sub(r'<[^>]+>', '', title)

    # URL
    url = item.get('url', '') or item.get('arcurl', '')
    if not url and bvid and str(bvid).startswith('BV'):
        url = f"https://www.bilibili.com/video/{bvid}"

    # Author
    owner = item.get('owner', {})
    author = item.get('author', {})
    if isinstance(owner, dict):
        author_name = owner.get('name', '')
        author_id = str(owner.get('mid', ''))
        author_avatar = owner.get('face', '')
    elif isinstance(author, dict):
        author_name = author.get('name', '')
        author_id = str(author.get('mid', ''))
        author_avatar = author.get('face', '')
    else:
        author_name = str(owner) if owner else (str(author) if author else '')
        author_id = ''
        author_avatar = ''

    # Cover
    pic = item.get('pic', '')
    if pic and not pic.startswith('http'):
        pic = 'https:' + pic

    # Stats
    stats = item.get('stats', {}) or {}
    view = item.get('play', 0) or (stats.get('view', 0) if isinstance(stats, dict) else 0)
    metrics = {
        'views': view,
        'likes': stats.get('like', 0) if isinstance(stats, dict) else 0,
        'coins': stats.get('coin', 0) if isinstance(stats, dict) else 0,
        'favorites': stats.get('favorite', 0) if isinstance(stats, dict) else 0,
        'danmaku': stats.get('danmaku', 0) if isinstance(stats, dict) else 0,
        'comments': stats.get('comment', item.get('comment', 0)) if isinstance(stats, dict) else 0,
    }

    # Duration
    duration = item.get('duration', '')
    desc = (item.get('description', '') or item.get('text', '') or '')[:500]

    detail = {}
    if duration:
        detail['duration'] = duration
    detail['duration_seconds'] = item.get('duration_seconds', 0)

    metrics_json = json.dumps(metrics, ensure_ascii=False)
    # B 站 API 的 pubdate 是 unix 秒整数；published_label 是"X天前"类人类可读
    # 前端 relativeTime 吃不下 unix 秒字符串（会 NaN/NaN），在此转 ISO
    published_label = item.get('published_label', '')
    pubdate_raw = item.get('pubdate', '')
    if published_label:
        published = published_label
    elif isinstance(pubdate_raw, (int, float)) and pubdate_raw > 0:
        from datetime import datetime, timezone
        try:
            published = datetime.fromtimestamp(pubdate_raw, tz=timezone.utc).isoformat()
        except (ValueError, OSError):
            published = ''
    else:
        published = str(pubdate_raw) if pubdate_raw else ''

    return {
        'id': bvid,
        'platform': 'bilibili',
        'source': source,
        'title': title or None,
        'content': desc or None,
        'author_name': author_name or (source.replace('up:', '') if source.startswith('up:') else ''),
        'author_id': author_id,
        'author_avatar': author_avatar,
        'url': url or None,
        'cover_url': pic or None,
        'media_json': None,
        'metrics_json': metrics_json,
        'tags_json': None,
        'lang': 'zh',
        'detail_json': json.dumps(detail, ensure_ascii=False) if detail else None,
        'comments_json': None,
        'ai_summary': None,
        'relevance_score': calc_relevance(title, desc, metrics, 'bilibili'),
        # watch_later 带 _add_at_iso（加入稍后再看的时间），让前端按加入时间倒序
        'fetched_at': item.get('_add_at_iso') or now_ts(),
        'published_at': published or None,
    }


# ============================================================
# RSS
# ============================================================
def ingest_rss(conn):
    """Ingest all RSS JSON files."""
    total = 0
    rss_dir = source_path('rss')
    if not os.path.isdir(rss_dir):
        return 0

    for f_path in sorted(glob.glob(os.path.join(rss_dir, '*.json'))):
        fname = os.path.basename(f_path).replace('.json', '')
        data = safe_load_json(f_path)
        if data is None:
            continue

        feed_title = data.get('feed_title', fname) if isinstance(data, dict) else fname
        raw_items = data.get('items', []) if isinstance(data, dict) else data

        items = []
        for entry in raw_items:
            entry_id = entry.get('id', '') or entry.get('link', '')
            if not entry_id:
                continue
            item_id = 'rss_' + hashlib.md5(entry_id.encode()).hexdigest()[:12]

            title = entry.get('title', '')
            content = entry.get('content', '') or entry.get('summary', '')
            content = re.sub(r'<[^>]+>', '', content)[:2000]
            link = entry.get('link', '')
            author = entry.get('author', feed_title)
            tags = entry.get('tags', [])

            items.append({
                'id': item_id,
                'platform': 'rss',
                'source': f'feed:{fname}',
                'title': title or None,
                'content': content or None,
                'author_name': author,
                'author_id': '',
                'author_avatar': '',
                'url': link,
                'cover_url': None,
                'media_json': None,
                'metrics_json': None,
                'tags_json': json.dumps(tags, ensure_ascii=False) if tags else None,
                'lang': 'en',
                'detail_json': json.dumps({'feed': feed_title, 'entry_id': entry_id}, ensure_ascii=False),
                'comments_json': None,
                'ai_summary': None,
                'relevance_score': calc_relevance(title, content, {}, 'rss'),
                'fetched_at': now_ts(),
                'published_at': entry.get('published', '') or None,
            })

        if items:
            batch_upsert_current_run(conn, items)
            total += len(items)
            print(f"  ✅ RSS {fname}: {len(items)} items")

    return total


# ============================================================
# HACKER NEWS
# ============================================================
def ingest_hackernews(conn):
    """Ingest Hacker News JSON files."""
    total = 0
    hn_dir = source_path('hackernews')
    if not os.path.isdir(hn_dir):
        return 0

    path = os.path.join(hn_dir, 'top.json')
    data = safe_load_json(path)
    if data is None:
        return 0

    stories = data if isinstance(data, list) else data.get('items', [])
    items = []
    for story in stories:
        sid = story.get('id', '')
        if not sid:
            continue

        title = story.get('title', '')
        text = story.get('text', '')  # Ask HN / Show HN body
        url = story.get('url', f'https://news.ycombinator.com/item?id={sid}')
        author = story.get('by', '')
        score = story.get('score', 0)
        descendants = story.get('descendants', 0)
        time_val = story.get('time', 0)
        published = datetime.fromtimestamp(time_val, tz=timezone.utc).isoformat() if time_val else ''

        metrics = {'score': score, 'comments': descendants}
        hn_url = f'https://news.ycombinator.com/item?id={sid}'

        items.append({
            'id': f'hn_{sid}',
            'platform': 'hackernews',
            'source': 'top',
            'title': title or None,
            'content': re.sub(r'<[^>]+>', '', text)[:2000] if text else None,
            'author_name': author,
            'author_id': author,
            'author_avatar': '',
            'url': url,
            'cover_url': None,
            'media_json': None,
            'metrics_json': json.dumps(metrics, ensure_ascii=False),
            'tags_json': None,
            'lang': 'en',
            'detail_json': json.dumps({'hn_url': hn_url, 'type': story.get('type', 'story')}, ensure_ascii=False),
            'comments_json': None,
            'ai_summary': None,
            'relevance_score': calc_relevance(title, text or '', metrics, 'hackernews'),
            'fetched_at': now_ts(),
            'published_at': published or None,
        })

    if items:
        batch_upsert_current_run(conn, items)
        total += len(items)
        print(f"  ✅ HN top: {len(items)} stories")

    return total


# ============================================================
# REDDIT
# ============================================================
def ingest_reddit(conn):
    """Ingest all Reddit JSON files."""
    total = 0
    reddit_dir = source_path('reddit')
    if not os.path.isdir(reddit_dir):
        return 0

    for f_path in sorted(glob.glob(os.path.join(reddit_dir, '*.json'))):
        sub = os.path.basename(f_path).replace('.json', '')
        data = safe_load_json(f_path)
        if data is None:
            continue

        posts = data if isinstance(data, list) else data.get('posts', [])
        items = []
        for post in posts:
            pid = post.get('id', '')
            if not pid:
                continue

            title = post.get('title', '')
            selftext = post.get('selftext', '')
            permalink = post.get('permalink', '')
            url = f'https://www.reddit.com{permalink}' if permalink else post.get('url', '')
            external_url = post.get('url', '') if not post.get('is_self', True) else ''

            score = post.get('score', 0)
            num_comments = post.get('num_comments', 0)
            created = post.get('created_utc', 0)
            published = datetime.fromtimestamp(created, tz=timezone.utc).isoformat() if created else ''

            thumbnail = post.get('thumbnail', '')
            if thumbnail in ('self', 'default', 'nsfw', 'spoiler', ''):
                thumbnail = ''
            flair = post.get('link_flair_text', '')

            metrics = {
                'score': score,
                'upvote_ratio': post.get('upvote_ratio', 0),
                'comments': num_comments,
            }
            detail = {}
            if external_url and external_url != url:
                detail['external_url'] = external_url
            if flair:
                detail['flair'] = flair

            items.append({
                'id': f'reddit_{pid}',
                'platform': 'reddit',
                'source': f'r/{sub}',
                'title': title or None,
                'content': selftext[:2000] or None,
                'author_name': post.get('author', ''),
                'author_id': post.get('author', ''),
                'author_avatar': '',
                'url': url,
                'cover_url': thumbnail or None,
                'media_json': None,
                'metrics_json': json.dumps(metrics, ensure_ascii=False),
                'tags_json': json.dumps([flair], ensure_ascii=False) if flair else None,
                'lang': 'en',
                'detail_json': json.dumps(detail, ensure_ascii=False) if detail else None,
                'comments_json': None,
                'ai_summary': None,
                'relevance_score': calc_relevance(title, selftext, metrics, 'reddit'),
                'fetched_at': now_ts(),
                'published_at': published or None,
            })

        if items:
            batch_upsert_current_run(conn, items)
            total += len(items)
            print(f"  ✅ Reddit r/{sub}: {len(items)} posts")

    return total


# ============================================================
# GITHUB (TRENDING + AWESOME, v16.0)
# ============================================================
# v16.0 改动:
# - 读取 trending.json 含 readme/readme_error 字段（W1.T3-fix 已保证）
# - 新增读取 awesome.json（fetch_github_awesome_repos 产出）
# - detail_json 加 readme + readme_error 字段（enrich 阶段拼接送 MiniMax）
# - source 区分: trending 用 `trending:{spoken_language}`；awesome 用 `awesome:{full_name}`
def _build_github_item(repo: dict, source: str, source_type: str) -> dict | None:
    """共用的 GitHub repo → DB item 转换。

    source_type ∈ {'trending', 'awesome'}：决定 detail_json 元数据布局。
    """
    full_name = repo.get('full_name', '')
    if not full_name:
        return None

    owner = full_name.split('/')[0] if '/' in full_name else ''
    stars = repo.get('stars', 0)
    forks = repo.get('forks', 0)
    stars_today = repo.get('stars_today', 0)
    lang = repo.get('language', '')

    metrics = {'stars': stars, 'forks': forks, 'stars_today': stars_today}

    detail: dict = {
        'language': lang,
        'source_type': source_type,
    }
    # v16.0: trending 维度
    if source_type == 'trending':
        detail['spoken_language'] = repo.get('spoken_language', 'global')
        detail['since'] = repo.get('since', 'daily')
    elif source_type == 'awesome':
        detail['pushed_at'] = repo.get('pushed_at', '')
    # v16.0: README 字段（缺失/失败时分别为 '' 和 error 字符串）
    detail['readme'] = repo.get('readme', '') or ''
    detail['readme_error'] = repo.get('readme_error')

    tags = [lang] if lang else []

    return {
        'id': f'gh_{full_name.replace("/", "_")}',
        'platform': 'github',
        'source': source,
        'title': full_name,
        'content': repo.get('description', '') or None,
        'author_name': owner,
        'author_id': owner,
        'author_avatar': f'https://github.com/{owner}.png',
        'url': repo.get('url', f'https://github.com/{full_name}'),
        'cover_url': None,
        'media_json': None,
        'metrics_json': json.dumps(metrics, ensure_ascii=False),
        'tags_json': json.dumps(tags, ensure_ascii=False) if tags else None,
        'lang': 'en',
        'detail_json': json.dumps(detail, ensure_ascii=False),
        'comments_json': None,
        'ai_summary': None,
        'relevance_score': calc_relevance(full_name, repo.get('description', ''), metrics, 'github'),
        'fetched_at': now_ts(),
        'published_at': None,
    }


def ingest_github_trending(conn):
    """Ingest GitHub Trending + Awesome JSON files (v16.0).

    - data/sources/github/trending.json — fetch_github_trending() 产出
    - data/sources/github/awesome.json  — fetch_github_awesome_repos() 产出（v16.0 新增）
    """
    total = 0
    gh_dir = source_path('github')
    if not os.path.isdir(gh_dir):
        return 0

    items: list[dict] = []
    seen_ids: set[str] = set()  # 跨 trending/awesome 去重（同 repo 同时出现取首次）

    # 1. trending.json
    trending_path = os.path.join(gh_dir, 'trending.json')
    trending_data = safe_load_json(trending_path)
    if trending_data is not None:
        repos = trending_data if isinstance(trending_data, list) else trending_data.get('repos', [])
        for repo in repos:
            spoken = repo.get('spoken_language', 'global')
            source = f'trending:{spoken}'
            item = _build_github_item(repo, source=source, source_type='trending')
            if item and item['id'] not in seen_ids:
                seen_ids.add(item['id'])
                items.append(item)

    # 2. awesome.json (v16.0 新增)
    awesome_path = os.path.join(gh_dir, 'awesome.json')
    awesome_data = safe_load_json(awesome_path)
    if awesome_data is not None:
        repos = awesome_data if isinstance(awesome_data, list) else awesome_data.get('repos', [])
        for repo in repos:
            full_name = repo.get('full_name', '')
            source = f'awesome:{full_name}' if full_name else 'awesome:unknown'
            item = _build_github_item(repo, source=source, source_type='awesome')
            if item and item['id'] not in seen_ids:
                seen_ids.add(item['id'])
                items.append(item)

    if items:
        batch_upsert_current_run(conn, items)
        total += len(items)
        trending_n = sum(1 for it in items if it['source'].startswith('trending:'))
        awesome_n = sum(1 for it in items if it['source'].startswith('awesome:'))
        print(f"  ✅ GitHub: trending={trending_n} awesome={awesome_n} total={len(items)} repos")

    return total


# ============================================================
# LINK ENRICHMENT (F45)
# ============================================================
_URL_RE = re.compile(r'https?://[^\s<>"\')\]]+')
_GITHUB_REPO_RE = re.compile(r'https?://github\.com/([^/\s]+)/([^/\s#?]+)')

def _extract_urls_from_item(item_row):
    """Extract URLs from url field, content, detail_json of an item."""
    urls = set()
    # Primary: item's own url field (HN external links, Reddit link posts, etc.)
    item_url = item_row.get('url', '')
    if item_url and _URL_RE.match(item_url):
        urls.add(item_url)
    for field in [item_row.get('content', ''), item_row.get('title', '')]:
        if field:
            urls.update(_URL_RE.findall(field))
    # Also extract from detail_json
    dj = item_row.get('detail_json')
    if dj:
        try:
            detail = json.loads(dj) if isinstance(dj, str) else dj
            # Twitter urls field
            for u in detail.get('urls', []):
                if isinstance(u, dict):
                    urls.add(u.get('expanded_url', '') or u.get('url', ''))
                elif isinstance(u, str):
                    urls.add(u)
            # Quoted tweet URL
            qt = detail.get('quotedTweet', {})
            if qt and isinstance(qt, dict):
                qt_url = qt.get('url', '')
                if qt_url:
                    urls.add(qt_url)
        except (json.JSONDecodeError, TypeError, AttributeError):
            pass
    # Filter: remove empty, platform-internal URLs, images, tracking params
    filtered = []
    for u in urls:
        u = u.rstrip('.,;:!?)')
        if not u or len(u) < 10:
            continue
        # Skip platform-internal links
        parsed = urllib.parse.urlparse(u)
        host = parsed.hostname or ''
        if any(h in host for h in ['xiaohongshu.com', 'bilibili.com', 'twitter.com', 'x.com', 't.co']):
            continue
        # Skip image/video files
        if re.search(r'\.(jpg|jpeg|png|gif|webp|mp4|m3u8)(\?|$)', u, re.I):
            continue
        filtered.append(u)
    return filtered[:5]  # Limit to 5 URLs per item


def _extract_body_text(html):
    """v8.0.3: Extract main body text from HTML, removing nav/footer/sidebar/script/style."""
    # Remove script, style, nav, footer, sidebar, header tags and their content
    for tag in ['script', 'style', 'nav', 'footer', 'header', 'aside', 'noscript']:
        html = re.sub(rf'<{tag}[^>]*>.*?</{tag}>', '', html, flags=re.I | re.S)
    # Remove all remaining HTML tags
    text = re.sub(r'<[^>]+>', ' ', html)
    # Collapse whitespace
    text = re.sub(r'\s+', ' ', text).strip()
    # Remove common boilerplate patterns
    text = re.sub(r'(Cookie|Privacy|Terms of Service|Copyright).*', '', text, flags=re.I)
    return text[:8000]  # Cap at 8000 chars


def _fetch_url_metadata(url, timeout=8):
    """Fetch title, description, and full_text from a URL."""
    try:
        parsed = urllib.parse.urlparse(url)
        host = parsed.hostname or ''

        # GitHub repo special handling
        gh_match = _GITHUB_REPO_RE.match(url)
        if gh_match:
            return _fetch_github_repo(gh_match.group(1), gh_match.group(2))

        # General URL: fetch HTML and extract meta tags + full text
        req = urllib.request.Request(url, headers={
            'User-Agent': 'Mozilla/5.0 (compatible; InfoRadar/1.0)',
            'Accept': 'text/html',
        })
        import ssl as _ssl
        _ctx = _ssl.create_default_context()
        with urllib.request.urlopen(req, timeout=timeout, context=_ctx) as resp:
            # Only read HTML content types
            ct = resp.headers.get('Content-Type', '')
            if 'text/html' not in ct and 'application/xhtml' not in ct:
                return {'url': url, 'title': host, 'description': '', 'full_text': '', 'type': 'link'}
            html = resp.read(200000).decode('utf-8', errors='ignore')

        title = ''
        desc = ''
        # Extract <title>
        m = re.search(r'<title[^>]*>(.*?)</title>', html, re.I | re.S)
        if m:
            title = re.sub(r'\s+', ' ', m.group(1)).strip()[:200]
        # Extract og:title (prefer over <title>)
        m = re.search(r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\'](.*?)["\']', html, re.I)
        if m:
            title = m.group(1).strip()[:200]
        # Extract og:description or meta description
        m = re.search(r'<meta[^>]+property=["\']og:description["\'][^>]+content=["\'](.*?)["\']', html, re.I)
        if not m:
            m = re.search(r'<meta[^>]+name=["\']description["\'][^>]+content=["\'](.*?)["\']', html, re.I)
        if m:
            desc = m.group(1).strip()[:500]

        # v8.0.3: Extract full body text
        full_text = _extract_body_text(html)

        return {'url': url, 'title': title or host, 'description': desc, 'full_text': full_text, 'type': 'link'}
    except Exception:
        return {'url': url, 'title': urllib.parse.urlparse(url).hostname or url[:50], 'description': '', 'full_text': '', 'type': 'link'}


def _fetch_github_repo(owner, repo):
    """Fetch GitHub repo metadata via API."""
    url = f'https://github.com/{owner}/{repo}'
    api_url = f'https://api.github.com/repos/{owner}/{repo}'
    try:
        req = urllib.request.Request(api_url, headers={
            'User-Agent': 'InfoRadar/1.0',
            'Accept': 'application/vnd.github.v3+json',
        })
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read().decode())
        result = {
            'url': url,
            'title': f'{owner}/{repo}',
            'description': (data.get('description', '') or '')[:300],
            'type': 'github',
            'stars': data.get('stargazers_count', 0),
            'language': data.get('language', ''),
            'updated_at': data.get('pushed_at', ''),
        }
        # v8.0.3: Get full README text (up to 8000 chars)
        try:
            readme_url = f'https://api.github.com/repos/{owner}/{repo}/readme'
            req2 = urllib.request.Request(readme_url, headers={
                'User-Agent': 'InfoRadar/1.0',
                'Accept': 'application/vnd.github.v3.raw',
            })
            with urllib.request.urlopen(req2, timeout=10) as resp2:
                readme = resp2.read(30000).decode('utf-8', errors='ignore')
            # Strip markdown headers for excerpt
            readme_stripped = re.sub(r'^#+\s+.*$', '', readme, flags=re.M).strip()
            result['readme_excerpt'] = readme_stripped[:500]
            result['full_text'] = readme[:8000]
        except Exception:
            pass
        return result
    except Exception:
        return {'url': url, 'title': f'{owner}/{repo}', 'description': '', 'type': 'github'}


def enrich_links(conn, limit=200):
    """F45: Extract URLs from recent items and fetch metadata.
    Only processes items that don't already have referenced_urls in detail_json."""
    print("\n🔗 Link Enrichment...")
    # Get recent items without referenced_urls
    if remote_db.fetch_write_to_remote():
        rows = remote_db.query_link_enrichment_items_remote(limit)
    else:
        rows = conn.execute("""
            SELECT id, title, content, detail_json, url FROM items
            WHERE fetched_at > datetime('now', '-2 days')
            AND (detail_json IS NULL OR detail_json NOT LIKE '%referenced_urls%')
            ORDER BY fetched_at DESC LIMIT ?
        """, (limit,)).fetchall()

    if not rows:
        print("  ✅ 无需充实 (所有近期 items 已处理)")
        return

    enriched = 0
    # Collect all (item_id, url) pairs to fetch
    work = []  # [(item_id, url, detail_json_str)]
    for row in rows:
        if isinstance(row, dict):
            item = {
                'title': row.get('title'),
                'content': row.get('content'),
                'detail_json': row.get('detail_json'),
                'url': row.get('url'),
            }
            row_id = row.get('id')
            detail_raw = row.get('detail_json')
        else:
            item = {'title': row[1], 'content': row[2], 'detail_json': row[3], 'url': row[4]}
            row_id = row[0]
            detail_raw = row[3]
        urls = _extract_urls_from_item(item)
        if urls:
            work.append((row_id, urls, detail_raw))

    if not work:
        print("  ✅ 无外部链接需要充实")
        return

    # Flatten all unique URLs for parallel fetching
    all_urls = set()
    for _, urls, _ in work:
        all_urls.update(urls)

    print(f"  发现 {len(work)} 条 items 含 {len(all_urls)} 个外部链接...")

    # Fetch metadata in parallel (max 5 workers — v8.0.3: reduced for full_text extraction)
    url_meta = {}
    with ThreadPoolExecutor(max_workers=5) as pool:
        futures = {pool.submit(_fetch_url_metadata, u): u for u in all_urls}
        for fut in as_completed(futures, timeout=60):
            u = futures[fut]
            try:
                url_meta[u] = fut.result()
            except Exception:
                url_meta[u] = {'url': u, 'title': '', 'description': '', 'type': 'link'}

    # Update each item's detail_json
    for item_id, urls, dj_str in work:
        try:
            detail = dict(dj_str) if isinstance(dj_str, dict) else (json.loads(dj_str) if dj_str else {})
        except (json.JSONDecodeError, TypeError):
            detail = {}
        ref_urls = [url_meta[u] for u in urls if u in url_meta and url_meta[u].get('title')]
        if not ref_urls:
            continue
        detail['referenced_urls'] = ref_urls
        if remote_db.fetch_write_to_remote():
            remote_db.update_item_detail_json_remote(item_id, detail)
        else:
            conn.execute("UPDATE items SET detail_json=? WHERE id=?",
                          (json.dumps(detail, ensure_ascii=False, default=str), item_id))
        enriched += 1

    if not remote_db.fetch_write_to_remote():
        conn.commit()
    print(f"  ✅ Link Enrichment: {enriched} items 已充实")


# ============================================================
# LINGOWHALE (公众号)
# ============================================================
def ingest_lingowhale(conn):
    """Ingest Lingowhale subscription feed."""
    path = lingowhale_path('feed.json')
    data = safe_load_json(path)
    if data is None:
        print("  ⚠️  data/lingowhale/feed.json 不存在或为空")
        return 0

    entries = data if isinstance(data, list) else data.get('feed_list', [])
    items = []
    for e in entries:
        entry_id = str(e.get('entry_id', ''))
        if not entry_id:
            continue

        title = e.get('title', '')
        content = e.get('content', '')
        abstract = e.get('abstract', '')  # AI summary from detail API
        viewpoint = e.get('viewpoint', [])  # Key points from detail API
        description = e.get('description', '')  # Fallback summary
        surface_url = e.get('surface_url', '')  # cover image
        pub_time = e.get('pub_time', 0)  # unix timestamp

        # Author info
        info_source = e.get('info_source', {})
        author_name = info_source.get('info_source_name', '')
        author_avatar = info_source.get('info_source_profile', '')

        # Channel/source info
        channel = e.get('channel', {})
        source = channel.get('name', 'subscription')
        group_name = e.get('group_name', '未分组')

        # Convert unix timestamp to ISO
        published_at = ''
        if pub_time:
            try:
                from datetime import datetime, timezone
                published_at = datetime.fromtimestamp(pub_time, tz=timezone.utc).isoformat()
            except (ValueError, OSError):
                published_at = ''

        # Priority: wechat original URL > lingowhale reader page
        wechat_url = e.get('wechat_url', '')
        url = wechat_url if wechat_url else f'https://lingowhale.com/reader/web/{entry_id}'

        # BF-0418-NEW2: 公众号自带的 abstract/viewpoint 结构不符合我们的 prompt,
        # 保留到 detail_json.lingowhale_abstract / _viewpoint 备查,
        # ai_summary/ai_key_points 留 None 让 generate_summaries.py 统一用我们的
        # prompt (prompts/02_summary_breakdown.md) 重跑,产出结构化摘要+要点.
        if viewpoint:
            viewpoint = [v.replace('<hl>', '').replace('</hl>', '') if isinstance(v, str) else v for v in viewpoint]
        _raw_abstract = abstract or description or None
        _stripped_abstract = _raw_abstract.replace('<hl>', '').replace('</hl>', '') if _raw_abstract else None

        detail = {'group': group_name}
        if _stripped_abstract:
            detail['lingowhale_abstract'] = _stripped_abstract
        if viewpoint:
            detail['lingowhale_viewpoint'] = viewpoint

        items.append({
            'id': f'lw_{entry_id}',
            'platform': 'lingowhale',
            'source': source,
            'title': title,
            'content': content,
            'author_name': author_name,
            'author_id': '',
            'author_avatar': author_avatar,
            'url': url,
            'cover_url': surface_url or None,
            'media_json': None,
            'metrics_json': None,
            'tags_json': None,
            'lang': 'zh',
            'detail_json': json.dumps(detail, ensure_ascii=False),
            'comments_json': None,
            'ai_summary': None,
            'ai_key_points': None,
            'relevance_score': calc_relevance(title, content, {}, 'lingowhale'),
            'fetched_at': now_ts(),
            'published_at': published_at,
        })

    if items:
        batch_upsert_current_run(conn, items)
        print(f"  ✅ 公众号: {len(items)} items")
    return len(items)


# ============================================================
# WAYTOAGI
# ============================================================
def ingest_waytoagi(conn):
    """Ingest WayToAGI daily updates from Feishu wiki."""
    path = source_path('waytoagi', 'daily.json')
    data = safe_load_json(path)
    if data is None:
        print("  ⚠️  data/sources/waytoagi/daily.json 不存在或为空")
        return 0

    raw_items = data.get('items', []) if isinstance(data, dict) else data
    items = []
    for entry in raw_items:
        token = entry.get('id', '')
        if not token:
            continue
        item_id = 'wtagi_' + hashlib.md5(token.encode()).hexdigest()[:12]

        title = entry.get('title', '')
        content = entry.get('content', '') or entry.get('summary', '')
        cover_url = entry.get('cover_url', '')
        date_str = entry.get('date', '')
        url = entry.get('url', '')

        items.append({
            'id': item_id,
            'platform': 'waytoagi',
            'source': 'waytoagi:daily',
            'title': title or None,
            'content': content or None,
            'author_name': 'WayToAGI',
            'author_id': '',
            'author_avatar': '',
            'url': url,
            'cover_url': cover_url or None,
            'media_json': None,
            'metrics_json': None,
            'tags_json': None,
            'lang': 'zh',
            'detail_json': json.dumps({'wiki_token': token}, ensure_ascii=False),
            'comments_json': None,
            'ai_summary': None,
            'relevance_score': calc_relevance(title, content, {}, 'waytoagi'),
            'fetched_at': now_ts(),
            'published_at': f'{date_str}T00:00:00' if date_str else None,
        })

    if items:
        batch_upsert_current_run(conn, items)
        print(f"  ✅ WayToAGI: {len(items)} items")
    return len(items)


# ============================================================
# MAIN
# ============================================================
def main():
    global CURRENT_RUN_ID
    parser = argparse.ArgumentParser(description='Ingest fetched source JSON into SQLite.')
    parser.add_argument('--run-id', type=int, default=None)
    parser.add_argument(
        '--only-twitter-timeline',
        action='store_true',
        help='only ingest Twitter following and for-you files; skip searches/bookmarks and all other platforms',
    )
    parser.add_argument(
        '--skip-link-enrichment',
        action='store_true',
        help='skip external URL metadata fetches after ingest',
    )
    parser.add_argument(
        '--skip-image-download',
        action='store_true',
        help='skip image download backfill after ingest',
    )
    args = parser.parse_args()
    run_id = args.run_id

    external_run = run_id is not None
    conn = open_current_backend_conn()

    if not run_id:
        run_id = start_current_fetch_run(conn)
    CURRENT_RUN_ID = run_id

    print("=" * 60)
    print(f"  信息雷达 — 数据入库 (run #{run_id})")
    print("=" * 60)

    stats = {}
    error = None
    failed_platforms = []

    # PL-4(B5): 平台级隔离——原实现九个平台包在同一个 try 里,任一平台的
    # 瞬时错误(如 pool checkout timeout)会让后续所有平台本轮 0 入库且缺口静默。
    def _run_platform(name, label, fn):
        nonlocal error
        print(f"\n{label}...")
        try:
            stats[name] = fn()
        except Exception as e:
            failed_platforms.append(name)
            error = f"{name}: {e}"  # 保留最后一个错误供 run 标记 warning
            print(f"\n❌ {name} 失败(不影响其他平台): {e}")
            import traceback
            traceback.print_exc()

    _run_platform('twitter', '📱 Twitter',
                  lambda: ingest_twitter(conn, timeline_only=args.only_twitter_timeline))
    if not args.only_twitter_timeline:
        _run_platform('xiaohongshu', '📕 小红书', lambda: ingest_xiaohongshu(conn))
        _run_platform('bilibili', '📺 B站', lambda: ingest_bilibili(conn))
        _run_platform('rss', '📡 RSS', lambda: ingest_rss(conn))
        _run_platform('hackernews', '🔶 Hacker News', lambda: ingest_hackernews(conn))
        _run_platform('reddit', '🤖 Reddit', lambda: ingest_reddit(conn))
        _run_platform('github', '🐙 GitHub Trending', lambda: ingest_github_trending(conn))
        _run_platform('lingowhale', '🐋 公众号', lambda: ingest_lingowhale(conn))
        _run_platform('waytoagi', '🔖 WayToAGI', lambda: ingest_waytoagi(conn))
        # v16.0: keyword search is retired; do not advance legacy search
        # keyword bookkeeping during normal ingest.
    if failed_platforms:
        print(f"\n⚠️ 本轮失败平台: {failed_platforms}(其余平台已正常入库)")

    # F45: Link Enrichment — enrich items with URL metadata
    if not args.skip_link_enrichment:
        try:
            enrich_links(conn)
        except Exception as e:
            print(f"  ⚠️ Link Enrichment error: {e}")

    if not external_run:
        finish_current_fetch_run(conn, run_id, stats, error)
    if conn is not None:
        conn.close()

    print("\n" + "=" * 60)
    total = sum(stats.values())
    print(f"  入库完成: 共 {total} 条")
    for p, c in stats.items():
        print(f"    {p}: {c}")

    print("=" * 60)

    if not args.skip_image_download:
        # Download all platform images to local storage (CDN signatures expire quickly)
        import subprocess
        try:
            print("\n🖼️  下载所有平台封面图片...")
            subprocess.run([sys.executable, os.path.join(BASE, 'src', 'download_images.py'),
                            '--all'],
                           timeout=600, cwd=BASE)
        except Exception as e:
            print(f"Image download warning: {e}")

    return 0 if not error else 1


if __name__ == '__main__':
    sys.exit(main())
