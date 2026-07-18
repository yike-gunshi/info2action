#!/usr/bin/env python3
"""Fetch active X user timelines from the sources registry."""
import json
import os
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

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


def _active_x_sources(conn=None, *, write_remote=None):
    import remote_db

    if write_remote is None:
        write_remote = remote_db.fetch_write_to_remote()
    if write_remote:
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


def _bounded_int(value, default, *, minimum, maximum):
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))


def _user_posts_workers(workers=None):
    if workers is None:
        workers = _twitter_config().get('user_posts_workers', 4)
    return _bounded_int(workers, 4, minimum=1, maximum=8)


def _user_posts_retry_rounds(retry_rounds=None):
    if retry_rounds is None:
        retry_rounds = _twitter_config().get('user_posts_retry_rounds', 2)
    return _bounded_int(retry_rounds, 2, minimum=0, maximum=3)


def _x_fetch_mode():
    value = os.environ.get('INFO2ACTION_X_FETCH_MODE') or _twitter_config().get(
        'fetch_mode', 'list'
    )
    return 'per_user' if str(value).strip().lower() == 'per_user' else 'list'


def _x_list_id():
    definitions = _x_list_definitions()
    return definitions[0]['list_id'] if len(definitions) == 1 else None


def _x_list_definitions():
    raw = os.environ.get('INFO2ACTION_X_LISTS_JSON')
    if raw:
        try:
            values = json.loads(raw)
        except (TypeError, ValueError):
            values = []
    else:
        values = _twitter_config().get('x_lists')
    definitions = []
    seen_keys = set()
    seen_ids = set()
    for item in values if isinstance(values, list) else []:
        if not isinstance(item, dict):
            continue
        key = str(item.get('key') or '').strip()
        list_id = str(item.get('list_id') or '').strip()
        if not key or not list_id.isdigit() or key in seen_keys or list_id in seen_ids:
            continue
        seen_keys.add(key)
        seen_ids.add(list_id)
        definitions.append({'key': key, 'list_id': list_id})
    if definitions:
        return definitions
    value = os.environ.get('INFO2ACTION_X_LIST_ID') or _twitter_config().get('x_list_id')
    list_id = str(value or '').strip()
    return [{'key': 'default', 'list_id': list_id}] if list_id.isdigit() else []


def _x_list_fetch_count():
    value = os.environ.get('INFO2ACTION_X_LIST_FETCH_COUNT') or _twitter_config().get(
        'list_fetch_count', 500
    )
    return _bounded_int(value, 500, minimum=1, maximum=500)


def _pending_search_batch_size():
    value = _twitter_config().get('pending_search_batch_size', 8)
    return _bounded_int(value, 8, minimum=1, maximum=12)


def _pending_search_workers():
    value = _twitter_config().get('pending_search_workers', 3)
    return _bounded_int(value, 3, minimum=1, maximum=4)


def _x_user_filename(handle):
    safe = ''.join(c if c.isalnum() or c == '_' else '_' for c in str(handle))
    return f"x-user-{safe or 'unknown'}.json"


def _normalize_tweet(tweet, handle):
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


class XFetchError(RuntimeError):
    def __init__(self, message, *, code, retryable):
        super().__init__(message)
        self.code = code
        self.retryable = retryable


def _fetch_user_posts(handle, count):
    try:
        result = subprocess.run(
            ["twitter", "user-posts", handle, "-n", str(count), "--json"],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except subprocess.TimeoutExpired as exc:
        raise XFetchError(
            f"timeout after {exc.timeout}s", code="timeout", retryable=True
        ) from exc

    if result.returncode != 0:
        message = (result.stderr or result.stdout or f"exit {result.returncode}").strip()
        retryable = _retryable_fetch_message(message)
        code = "rate_limited" if any(
            marker in message.lower() for marker in ("rate limit", "429")
        ) else "timeout" if retryable else "request_failed"
        raise XFetchError(message, code=code, retryable=retryable)

    try:
        data = json.loads(result.stdout or "[]")
    except json.JSONDecodeError as exc:
        raise XFetchError(
            "twitter user-posts returned invalid JSON",
            code="invalid_response",
            retryable=False,
        ) from exc
    if isinstance(data, dict):
        data = data.get("data")
    if not isinstance(data, list):
        raise XFetchError(
            "twitter user-posts did not return a JSON array",
            code="invalid_response",
            retryable=False,
        )
    return data


def _fetch_list_posts(list_id, count):
    try:
        result = subprocess.run(
            ["twitter", "list", str(list_id), "-n", str(count), "--json"],
            capture_output=True,
            text=True,
            timeout=180,
        )
    except subprocess.TimeoutExpired as exc:
        raise XFetchError(
            f"timeout after {exc.timeout}s", code="timeout", retryable=True
        ) from exc

    if result.returncode != 0:
        message = (result.stderr or result.stdout or f"exit {result.returncode}").strip()
        retryable = _retryable_fetch_message(message)
        code = "rate_limited" if any(
            marker in message.lower() for marker in ("rate limit", "429")
        ) else "timeout" if retryable else "request_failed"
        raise XFetchError(message, code=code, retryable=retryable)

    try:
        data = json.loads(result.stdout or "[]")
    except json.JSONDecodeError as exc:
        raise XFetchError(
            "twitter list returned invalid JSON",
            code="invalid_response",
            retryable=False,
        ) from exc
    if isinstance(data, dict):
        data = data.get("data")
    if not isinstance(data, list):
        raise XFetchError(
            "twitter list did not return a JSON array",
            code="invalid_response",
            retryable=False,
        )
    return data


def _fetch_search_posts(handles, count):
    if any(
        not handle
        or not handle.isascii()
        or any(not (char.isalnum() or char == '_') for char in handle)
        for handle in handles
    ):
        raise XFetchError(
            "configured X handle is invalid",
            code="invalid_handle",
            retryable=False,
        )
    query = "(" + " OR ".join(f"from:{handle}" for handle in handles) + ")"
    try:
        result = subprocess.run(
            [
                "twitter", "search", query,
                "-t", "latest", "-n", str(count), "--json",
            ],
            capture_output=True,
            text=True,
            timeout=180,
        )
    except subprocess.TimeoutExpired as exc:
        raise XFetchError(
            f"timeout after {exc.timeout}s", code="timeout", retryable=True
        ) from exc

    if result.returncode != 0:
        message = (result.stderr or result.stdout or f"exit {result.returncode}").strip()
        retryable = _retryable_fetch_message(message)
        code = "rate_limited" if any(
            marker in message.lower() for marker in ("rate limit", "429")
        ) else "timeout" if retryable else "request_failed"
        raise XFetchError(message, code=code, retryable=retryable)

    try:
        data = json.loads(result.stdout or "[]")
    except json.JSONDecodeError as exc:
        raise XFetchError(
            "twitter search returned invalid JSON",
            code="invalid_response",
            retryable=False,
        ) from exc
    if isinstance(data, dict):
        data = data.get("data")
    if not isinstance(data, list):
        raise XFetchError(
            "twitter search did not return a JSON array",
            code="invalid_response",
            retryable=False,
        )
    return data


def _tweet_author_handle(tweet):
    author = tweet.get('author') if isinstance(tweet, dict) else None
    if isinstance(author, dict):
        value = (
            author.get('screenName')
            or author.get('screen_name')
            or author.get('username')
        )
    else:
        value = author
    return str(value or '').strip().lstrip('@') or None


def _ensure_x_list_members(sources):
    import x_list_registry

    return x_list_registry.sync_registry_members_for_fetch(sources)


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


def _latest_x_user_watermark(conn, handle, *, source_id=None, write_remote=None):
    import remote_db

    if write_remote is None:
        write_remote = remote_db.fetch_write_to_remote()
    if write_remote:
        return _external_tweet_id(
            remote_db.latest_x_user_watermark_remote(source_id)
        )

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


def _utc_now():
    return datetime.now(timezone.utc).isoformat()


def _attempt_key(source):
    source_id = source.get('id')
    return str(source_id) if source_id is not None else f"handle:{source['source_key']}"


def _attempt_summary_path():
    return os.path.join(data_dir(), 'x_user_attempts.json')


def _attempt_summary_payload(state, *, finished=False):
    planned = state['planned_sources']
    results_by_key = state['results_by_key']
    results = [
        results_by_key[_attempt_key(source)]
        for source in planned
        if _attempt_key(source) in results_by_key
    ]
    terminal = [r for r in results if r['outcome'] in {'success', 'no_new', 'failed'}]
    succeeded = sum(r['outcome'] in {'success', 'no_new'} for r in terminal)
    failed = sum(r['outcome'] == 'failed' for r in terminal)
    payload = {
        'schema_version': 1,
        'started_at': state['started_at'],
        'finished_at': _utc_now() if finished else None,
        'planned_sources': [
            {'source_id': source.get('id'), 'handle': source['source_key']}
            for source in planned
        ],
        'planned_source_ids': [source.get('id') for source in planned],
        'planned': len(planned),
        'attempted': sum(int(result.get('attempts') or 0) > 0 for result in results),
        'succeeded': succeeded,
        'no_new': sum(r['outcome'] == 'no_new' for r in terminal),
        'failed': failed,
        'missed': max(0, len(planned) - len(terminal)),
        'results': results,
    }
    for key in (
        'mode', 'list_id', 'list_ids', 'unmatched_posts', 'membership',
        'fallback_sources', 'fallback_search_batches',
    ):
        if key in state:
            payload[key] = state[key]
    return payload


def _write_attempt_summary(state, *, finished=False):
    path = _attempt_summary_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp_path = f"{path}.{os.getpid()}.tmp"
    with open(tmp_path, 'w') as handle:
        json.dump(
            _attempt_summary_payload(state, finished=finished),
            handle,
            ensure_ascii=False,
            indent=2,
        )
    os.replace(tmp_path, path)


def _fetch_wave(jobs, count, workers, attempt_counts):
    with ThreadPoolExecutor(max_workers=min(workers, len(jobs))) as executor:
        futures = {}
        for job in jobs:
            key = _attempt_key(job['source'])
            attempt_counts[key] = attempt_counts.get(key, 0) + 1
            started = time.monotonic()
            future = executor.submit(_fetch_user_posts, job['source']['source_key'], count)
            futures[future] = (job, attempt_counts[key], started)

        for future in as_completed(futures):
            job, attempt_no, started = futures[future]
            duration_ms = max(0, int((time.monotonic() - started) * 1000))
            try:
                posts = future.result()
            except Exception as exc:  # one source must not abort the wave
                retryable = bool(getattr(exc, 'retryable', False))
                code = getattr(exc, 'code', 'unexpected_error')
                yield {
                    'job': job,
                    'ok': False,
                    'error': exc,
                    'error_code': code,
                    'retryable': retryable,
                    'attempt_no': attempt_no,
                    'duration_ms': duration_ms,
                }
            else:
                yield {
                    'job': job,
                    'ok': True,
                    'posts': posts,
                    'attempt_no': attempt_no,
                    'duration_ms': duration_ms,
                }


def _source_jobs(conn, sources, *, write_remote):
    jobs = []
    for source in sources:
        try:
            watermark = _latest_x_user_watermark(
                conn,
                source['source_key'],
                source_id=source.get('id'),
                write_remote=write_remote,
            )
        except Exception as exc:
            watermark = None
            print(
                f"  ⚠️  @{source['source_key']}: watermark unavailable; "
                f"fetching full window ({exc})"
            )
        jobs.append({'source': source, 'watermark': watermark, 'duration_ms': 0})
    return jobs


def _write_x_user_payload(out_dir, handle, posts):
    payload = {
        "ok": True,
        "source": "x_user",
        "handle": handle,
        "data": [_normalize_tweet(tweet, handle) for tweet in posts],
    }
    out_path = os.path.join(out_dir, _x_user_filename(handle))
    with open(out_path, 'w') as output:
        json.dump(payload, output, ensure_ascii=False, indent=2)


def _list_failure_result(source, error, *, duration_ms, error_code=None):
    return {
        'source_id': source.get('id'),
        'handle': source['source_key'],
        'outcome': 'failed',
        'attempts': 1,
        'duration_ms': duration_ms,
        'new_count': 0,
        'raw_count': 0,
        'error_code': error_code or getattr(error, 'code', 'unexpected_error'),
        'error': str(error)[:500],
        'finished_at': _utc_now(),
    }


def _record_batch_failure(
    jobs, error, *, duration_ms, state, stats, ingest, broken_after
):
    for job in jobs:
        source = job['source']
        ingest.record_source_fetch_result_current_backend(
            source.get('id'), ok=False, error=error, broken_after=broken_after
        )
        state['results_by_key'][_attempt_key(source)] = _list_failure_result(
            source, error, duration_ms=duration_ms
        )
        stats['failed'] += 1
        _write_attempt_summary(state)


def _record_posts_for_jobs(
    jobs, raw_posts, *, duration_ms, out_dir, state, stats, ingest, broken_after
):
    posts_by_handle = {
        job['source']['source_key'].casefold(): []
        for job in jobs
    }
    for post in raw_posts:
        handle = _tweet_author_handle(post)
        key = handle.casefold() if handle else ''
        if key in posts_by_handle:
            posts_by_handle[key].append(post)
        else:
            state['unmatched_posts'] += 1

    for job in jobs:
        source = job['source']
        handle = source['source_key']
        matched = posts_by_handle.get(handle.casefold(), [])
        new_posts = _filter_new_posts(matched, job['watermark'])
        _write_x_user_payload(out_dir, handle, new_posts)
        ingest.record_source_fetch_result_current_backend(
            source.get('id'), ok=True, broken_after=broken_after
        )
        outcome = 'success' if new_posts else 'no_new'
        state['results_by_key'][_attempt_key(source)] = {
            'source_id': source.get('id'),
            'handle': handle,
            'outcome': outcome,
            'attempts': 1,
            'duration_ms': duration_ms,
            'new_count': len(new_posts),
            'raw_count': len(matched),
            'error_code': None,
            'error': None,
            'finished_at': _utc_now(),
        }
        stats['ok'] += 1
        _write_attempt_summary(state)


def _fetch_x_users_from_list(conn, sources, *, write_remote):
    import ingest

    list_definitions = _x_list_definitions()
    list_ids = [item['list_id'] for item in list_definitions]
    list_id = list_ids[0] if len(list_ids) == 1 else None
    out_dir = source_dir('twitter')
    os.makedirs(out_dir, exist_ok=True)
    state = {
        'started_at': _utc_now(),
        'planned_sources': list(sources),
        'results_by_key': {},
        'mode': 'list',
        'list_id': list_id,
        'list_ids': list_ids,
        'unmatched_posts': 0,
        'fallback_sources': 0,
        'fallback_search_batches': 0,
    }
    stats = {'handles': len(sources), 'ok': 0, 'failed': 0}
    _write_attempt_summary(state)
    broken_after = db._broken_after_threshold()
    jobs = _source_jobs(conn, sources, write_remote=write_remote)

    if not list_definitions:
        membership = {
            'configured': False,
            'list_id': None,
            'lists': [],
            'synced_handles': [],
            'pending_handles': [source['source_key'] for source in sources],
            'failed': [],
            'last_error': 'twitter.x_lists is not configured',
        }
    else:
        try:
            membership = _ensure_x_list_members(sources)
        except Exception as exc:
            membership = {
                'configured': True,
                'list_id': list_id,
                'lists': [],
                'synced_handles': [],
                'pending_handles': [source['source_key'] for source in sources],
                'failed': [],
                'last_error': str(exc)[:500],
            }

    state['membership'] = {
        key: membership.get(key)
        for key in (
            'configured', 'list_id', 'lists', 'synced_count', 'pending_count',
            'last_synced_at', 'last_error', 'sync_skipped_reason',
        )
        if key in membership
    }
    list_id_by_handle = {}
    membership_lists = membership.get('lists')
    if isinstance(membership_lists, list) and membership_lists:
        for item in membership_lists:
            if not isinstance(item, dict):
                continue
            configured_id = str(item.get('list_id') or '')
            if configured_id not in list_ids:
                continue
            for handle in item.get('synced_handles') or []:
                list_id_by_handle[str(handle).casefold()] = configured_id
    elif list_id:
        for handle in membership.get('synced_handles') or []:
            list_id_by_handle[str(handle).casefold()] = list_id

    fetch_jobs_by_list = {configured_id: [] for configured_id in list_ids}
    fallback_jobs = []
    for job in jobs:
        source = job['source']
        handle_key = source['source_key'].casefold()
        configured_id = list_id_by_handle.get(handle_key)
        if configured_id in fetch_jobs_by_list:
            fetch_jobs_by_list[configured_id].append(job)
        else:
            fallback_jobs.append(job)

    state['fallback_sources'] = len(fallback_jobs)

    list_batches = {
        configured_id: batch
        for configured_id, batch in fetch_jobs_by_list.items()
        if batch
    }
    if list_batches:
        with ThreadPoolExecutor(max_workers=min(5, len(list_batches))) as executor:
            futures = {
                executor.submit(
                    _fetch_list_posts, configured_id, _x_list_fetch_count()
                ): (batch, time.monotonic())
                for configured_id, batch in list_batches.items()
            }
            for future in as_completed(futures):
                batch, started = futures[future]
                duration_ms = max(0, int((time.monotonic() - started) * 1000))
                try:
                    raw_posts = future.result()
                except Exception as exc:
                    _record_batch_failure(
                        batch,
                        exc,
                        duration_ms=duration_ms,
                        state=state,
                        stats=stats,
                        ingest=ingest,
                        broken_after=broken_after,
                    )
                else:
                    _record_posts_for_jobs(
                        batch,
                        raw_posts,
                        duration_ms=duration_ms,
                        out_dir=out_dir,
                        state=state,
                        stats=stats,
                        ingest=ingest,
                        broken_after=broken_after,
                    )

    if fallback_jobs:
        batch_size = _pending_search_batch_size()
        batches = [
            fallback_jobs[index:index + batch_size]
            for index in range(0, len(fallback_jobs), batch_size)
        ]
        state['fallback_search_batches'] = len(batches)
        with ThreadPoolExecutor(
            max_workers=min(_pending_search_workers(), len(batches))
        ) as executor:
            futures = {
                executor.submit(
                    _fetch_search_posts,
                    [job['source']['source_key'] for job in batch],
                    _x_list_fetch_count(),
                ): (batch, time.monotonic())
                for batch in batches
            }
            for future in as_completed(futures):
                batch, started = futures[future]
                duration_ms = max(0, int((time.monotonic() - started) * 1000))
                try:
                    raw_posts = future.result()
                except Exception as exc:
                    _record_batch_failure(
                        batch,
                        exc,
                        duration_ms=duration_ms,
                        state=state,
                        stats=stats,
                        ingest=ingest,
                        broken_after=broken_after,
                    )
                else:
                    _record_posts_for_jobs(
                        batch,
                        raw_posts,
                        duration_ms=duration_ms,
                        out_dir=out_dir,
                        state=state,
                        stats=stats,
                        ingest=ingest,
                        broken_after=broken_after,
                    )

    _write_attempt_summary(state, finished=True)
    return stats


def fetch_x_users(
    conn=None,
    *,
    count=None,
    batch_size=None,
    workers=None,
    retry_rounds=None,
):
    import remote_db

    write_remote = remote_db.fetch_write_to_remote()
    own_conn = conn is None and not write_remote
    if own_conn:
        conn = db.get_conn()
    try:
        sources = _active_x_sources(conn, write_remote=write_remote)
        handles = [source['source_key'] for source in sources]
        if not handles:
            print("  无可抓取 x_user，跳过")
            return {"handles": 0, "ok": 0, "failed": 0}

        if _x_fetch_mode() == 'list':
            configured_ids = [item['list_id'] for item in _x_list_definitions()]
            print(
                f"  x_user 通道：{len(configured_ids)} 个 X List 并发聚合抓取 "
                f"{len(handles)} 个配置账号 (count={_x_list_fetch_count()})"
            )
            return _fetch_x_users_from_list(conn, sources, write_remote=write_remote)

        n = _user_posts_count(count)
        worker_count = _user_posts_workers(workers)
        retry_count = _user_posts_retry_rounds(retry_rounds)
        out_dir = source_dir('twitter')
        os.makedirs(out_dir, exist_ok=True)
        print(
            f"  x_user 通道：公平并发抓取 {len(handles)} 个配置账号 "
            f"(workers={worker_count}, retry_rounds={retry_count})"
        )
        stats = {"handles": len(handles), "ok": 0, "failed": 0}
        state = {
            'started_at': _utc_now(),
            'planned_sources': list(sources),
            'results_by_key': {},
        }
        _write_attempt_summary(state)

        broken_after = db._broken_after_threshold()
        import ingest

        jobs = _source_jobs(conn, sources, write_remote=write_remote)

        pending = [
            job for job in jobs
            if _attempt_key(job['source']) not in state['results_by_key']
        ]
        attempt_counts = {}
        for wave in range(retry_count + 1):
            if not pending:
                break
            wave_results = _fetch_wave(pending, n, worker_count, attempt_counts)
            retry_jobs = []
            saw_rate_limit = False
            for result in wave_results:
                job = result['job']
                source = job['source']
                handle = source['source_key']
                source_id = source.get('id')
                key = _attempt_key(source)
                job['duration_ms'] += result['duration_ms']
                if result['ok']:
                    raw_posts = result['posts']
                    new_posts = _filter_new_posts(raw_posts, job['watermark'])
                    payload = {
                        "ok": True,
                        "source": "x_user",
                        "handle": handle,
                        "data": [_normalize_tweet(t, handle) for t in new_posts],
                    }
                    out_path = os.path.join(out_dir, _x_user_filename(handle))
                    with open(out_path, 'w') as output:
                        json.dump(payload, output, ensure_ascii=False, indent=2)
                    ingest.record_source_fetch_result_current_backend(
                        source_id, ok=True, broken_after=broken_after
                    )
                    outcome = 'success' if new_posts else 'no_new'
                    state['results_by_key'][key] = {
                        'source_id': source_id,
                        'handle': handle,
                        'outcome': outcome,
                        'attempts': result['attempt_no'],
                        'duration_ms': job['duration_ms'],
                        'new_count': len(new_posts),
                        'raw_count': len(raw_posts),
                        'error_code': None,
                        'error': None,
                        'finished_at': _utc_now(),
                    }
                    stats['ok'] += 1
                    if job['watermark']:
                        print(f"  ✅ @{handle}: {len(new_posts)}/{len(raw_posts)} new tweets")
                    else:
                        print(f"  ✅ @{handle}: {len(new_posts)} tweets")
                elif result['retryable'] and wave < retry_count:
                    retry_jobs.append(job)
                    saw_rate_limit = saw_rate_limit or result['error_code'] == 'rate_limited'
                    state['results_by_key'][key] = {
                        'source_id': source_id,
                        'handle': handle,
                        'outcome': 'retrying',
                        'attempts': result['attempt_no'],
                        'duration_ms': job['duration_ms'],
                        'new_count': 0,
                        'error_code': result['error_code'],
                        'error': str(result['error'])[:500],
                        'finished_at': None,
                    }
                    print(f"  ↻ @{handle}: {result['error']} (retry wave {wave + 1})")
                else:
                    ingest.record_source_fetch_result_current_backend(
                        source_id,
                        ok=False,
                        error=result['error'],
                        broken_after=broken_after,
                    )
                    state['results_by_key'][key] = {
                        'source_id': source_id,
                        'handle': handle,
                        'outcome': 'failed',
                        'attempts': result['attempt_no'],
                        'duration_ms': job['duration_ms'],
                        'new_count': 0,
                        'error_code': result['error_code'],
                        'error': str(result['error'])[:500],
                        'finished_at': _utc_now(),
                    }
                    stats['failed'] += 1
                    print(f"  ❌ @{handle}: {result['error']}")
                _write_attempt_summary(state)

            pending = retry_jobs
            if pending:
                delay = min(30, (2 ** wave) * (2 if saw_rate_limit else 1))
                print(f"  ⏳ X retry wave {wave + 1}: shared cooldown {delay}s")
                time.sleep(delay)

        _write_attempt_summary(state, finished=True)
        return stats
    finally:
        if own_conn:
            conn.close()


def main():
    fetch_x_users()


if __name__ == '__main__':
    main()
