#!/usr/bin/env python3
"""Fetch active X user timelines from the sources registry."""
import json
import os
import subprocess
import time

import db

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

try:
    with open(os.path.join(BASE, 'config', 'config.json')) as f:
        CONFIG = json.load(f)
except (OSError, json.JSONDecodeError):
    CONFIG = {}


def data_dir():
    return os.environ.get('INFO2ACTION_DATA_DIR') or os.path.join(BASE, 'data')


def source_dir(*parts):
    return os.path.join(data_dir(), 'sources', *parts)


def _active_x_handles(conn=None):
    return [source['source_key'] for source in _active_x_sources(conn)]


def _active_x_sources(conn=None):
    import remote_db

    if remote_db.fetch_write_to_remote():
        return remote_db.list_active_sources_remote('x_user')

    own_conn = conn is None
    if conn is None:
        conn = db.get_conn()
    try:
        return db.list_active_sources(conn, 'x_user')
    finally:
        if own_conn:
            conn.close()


def _twitter_config():
    cfg = CONFIG.get('twitter', {})
    return cfg if isinstance(cfg, dict) else {}


def _user_posts_count(count=None):
    if count is None:
        count = _twitter_config().get('user_posts_count', 20)
    try:
        return int(count)
    except (TypeError, ValueError):
        return 20


def _x_user_batch_size(batch_size=None):
    if batch_size is None:
        batch_size = _twitter_config().get('x_user_batch_size', 20)
    try:
        batch_size = int(batch_size)
    except (TypeError, ValueError):
        return 20
    return batch_size if batch_size > 0 else 20


def _x_user_filename(handle):
    safe = ''.join(c if c.isalnum() or c == '_' else '_' for c in str(handle))
    return f"x-user-{safe or 'unknown'}.json"


def _normalize_compact_tweet(tweet, handle):
    item = dict(tweet)
    raw_author = item.get('author')
    if isinstance(raw_author, dict):
        author = dict(raw_author)
    else:
        author = {'name': raw_author or handle}
    author.setdefault('screenName', handle)
    author.setdefault('name', author.get('screenName') or handle)
    item['author'] = author

    metrics = item.get('metrics')
    if not isinstance(metrics, dict):
        metrics = {}
    metrics = dict(metrics)
    metrics.setdefault('likes', item.get('likes', 0))
    metrics.setdefault('retweets', item.get('rts', 0))
    metrics.setdefault('views', item.get('views', 0))
    metrics.setdefault('bookmarks', item.get('bookmarks', 0))
    metrics.setdefault('replies', item.get('replies', 0))
    item['metrics'] = metrics

    if 'createdAt' not in item and item.get('time'):
        item['createdAt'] = item.get('time')
    return item


def _retryable_fetch_message(message):
    text = str(message or '').lower()
    return any(marker in text for marker in ('rate limit', '429', 'timeout', 'timed out'))


def _fetch_user_posts(handle, count):
    delays = (1, 2, 4)
    last_error = None
    for attempt in range(len(delays) + 1):
        try:
            result = subprocess.run(
                ["twitter", "--compact", "user-posts", handle, "-n", str(count)],
                capture_output=True,
                text=True,
                timeout=60,
            )
        except subprocess.TimeoutExpired as e:
            last_error = f"timeout after {e.timeout}s"
            retryable = True
        else:
            msg = (result.stderr or result.stdout or f"exit {result.returncode}").strip()
            retryable = result.returncode != 0 or _retryable_fetch_message(result.stderr)
            if not retryable:
                data = json.loads(result.stdout or "[]")
                if not isinstance(data, list):
                    raise ValueError("twitter user-posts did not return a JSON array")
                return data
            last_error = msg

        if attempt < len(delays):
            time.sleep(delays[attempt])

    raise RuntimeError(last_error or "twitter user-posts failed")


def _cursor_path(out_dir):
    return os.path.join(out_dir, '.x_user_cursor.json')


def _load_x_user_cursor(out_dir):
    try:
        with open(_cursor_path(out_dir)) as f:
            data = json.load(f)
        return int(data.get('next_index', 0))
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return 0


def _save_x_user_cursor(out_dir, next_index):
    with open(_cursor_path(out_dir), 'w') as f:
        json.dump({'next_index': next_index}, f, ensure_ascii=False, indent=2)


def _select_x_user_batch(handles, batch_size, out_dir):
    if len(handles) <= batch_size:
        return handles, None
    start = _load_x_user_cursor(out_dir) % len(handles)
    selected = []
    for offset in range(batch_size):
        selected.append(handles[(start + offset) % len(handles)])
    return selected, (start + batch_size) % len(handles)


def _external_tweet_id(item_id):
    value = str(item_id or '').strip()
    if value.startswith('tw_'):
        value = value[3:]
    return value or None


def _tweet_id_is_newer(tweet_id, watermark):
    current = _external_tweet_id(tweet_id)
    watermark = _external_tweet_id(watermark)
    if not current or not watermark:
        return True
    try:
        return int(current) > int(watermark)
    except ValueError:
        return current > watermark


def _latest_x_user_watermark(conn, handle):
    source = conn.execute(
        "SELECT id FROM sources WHERE platform = 'x_user' AND source_key = ?",
        (handle,),
    ).fetchone()
    if source is None:
        return None
    row = conn.execute(
        """SELECT id
           FROM items
           WHERE source_id = ?
             AND platform = 'twitter'
             AND NULLIF(published_at, '') IS NOT NULL
           ORDER BY datetime(NULLIF(published_at, '')) DESC, published_at DESC
           LIMIT 1""",
        (source['id'],),
    ).fetchone()
    if row is None:
        return None
    return _external_tweet_id(row['id'])


def _filter_new_posts(posts, watermark):
    if not watermark:
        return posts
    return [post for post in posts if _tweet_id_is_newer(post.get('id'), watermark)]


def fetch_x_users(conn=None, *, count=None, batch_size=None):
    own_conn = conn is None
    if conn is None:
        conn = db.get_conn()
    try:
        sources = _active_x_sources(conn)
        handles = [source['source_key'] for source in sources]
        if not handles:
            print("  无 active x_user，跳过")
            return {"handles": 0, "ok": 0, "failed": 0}

        n = _user_posts_count(count)
        out_dir = source_dir('twitter')
        os.makedirs(out_dir, exist_ok=True)
        selected_sources, next_index = _select_x_user_batch(
            sources,
            _x_user_batch_size(batch_size),
            out_dir,
        )
        selected_handles = [source['source_key'] for source in selected_sources]
        if next_index is None:
            print(f"  x_user 通道：抓取 {len(handles)} 个账号")
        else:
            print(f"  x_user 通道：抓取 {len(selected_handles)}/{len(handles)} 个账号")
        stats = {"handles": len(handles), "ok": 0, "failed": 0}
        if next_index is not None:
            stats["selected"] = len(selected_handles)

        # R10: 独立低频节奏由 cron 调度；本脚本只控制每轮 batch、游标和单账号 count。
        broken_after = db._broken_after_threshold()
        import ingest

        for source in selected_sources:
            handle = source['source_key']
            source_id = source.get('id')
            try:
                watermark = _latest_x_user_watermark(conn, handle)
                raw_posts = _fetch_user_posts(handle, n)
                new_posts = _filter_new_posts(raw_posts, watermark)
                payload = {
                    "ok": True,
                    "source": "x_user",
                    "handle": handle,
                    "data": [_normalize_compact_tweet(t, handle) for t in new_posts],
                }
                out_path = os.path.join(out_dir, _x_user_filename(handle))
                with open(out_path, 'w') as f:
                    json.dump(payload, f, ensure_ascii=False, indent=2)
                ingest.record_source_fetch_result_current_backend(
                    source_id, ok=True, broken_after=broken_after
                )
                stats["ok"] += 1
                if watermark:
                    print(f"  ✅ @{handle}: {len(new_posts)}/{len(raw_posts)} new tweets")
                else:
                    print(f"  ✅ @{handle}: {len(new_posts)} tweets")
            except Exception as e:
                ingest.record_source_fetch_result_current_backend(
                    source_id, ok=False, error=e, broken_after=broken_after
                )
                stats["failed"] += 1
                print(f"  ❌ @{handle}: {e}")
        if next_index is not None:
            _save_x_user_cursor(out_dir, next_index)
        return stats
    finally:
        if own_conn:
            conn.close()


def main():
    fetch_x_users()


if __name__ == '__main__':
    main()
