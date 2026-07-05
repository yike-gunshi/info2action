#!/usr/bin/env python3
"""Info Radar — Download and localize cover/media images.

Downloads images from CDN URLs (XHS, Bilibili, etc.) to local storage,
then updates the DB to point to local paths. This prevents 403 errors
when CDN signatures expire.

Usage:
    python3 download_images.py [--platform xiaohongshu] [--limit 200]
"""
import argparse, json, os, re, sys, threading, urllib.request, urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
import db

IMG_DIR = os.path.join(BASE, 'data', 'images')

# Platform-specific Referer headers
REFERERS = {
    'xiaohongshu': 'https://www.xiaohongshu.com/',
    'bilibili': 'https://www.bilibili.com/',
    'twitter': 'https://x.com/',
    'waytoagi': 'https://mp.weixin.qq.com/',
}

# Platforms that require proxy to download images (blocked in China)
PROXY_PLATFORMS = {'twitter', 'reddit'}

# Global opener (set in main() if --proxy is provided)
_opener = None

# Regex to match signed XHS CDN URLs and extract the image path
# Pattern: http(s)://sns-webpic-*.xhscdn.com/{timestamp}/{signature}/{image_path}
_XHS_SIGNED_RE = re.compile(
    r'^https?://sns-webpic[^/]*\.xhscdn\.com/\d+/[0-9a-f]+/(.+)$'
)


def _normalize_xhs_url(url):
    """Convert a signed XHS CDN URL to an unsigned ci.xiaohongshu.com URL.

    Signed URLs expire quickly (403 after a few hours). The unsigned
    ci.xiaohongshu.com host serves the same images without signatures.
    """
    m = _XHS_SIGNED_RE.match(url)
    if m:
        return f'http://ci.xiaohongshu.com/{m.group(1)}'
    return url

_print_lock = threading.Lock()
_counter = {'done': 0}
_counter_lock = threading.Lock()


def _guess_ext(url, content_type=None):
    """Guess file extension from URL or content-type."""
    # Try content-type first
    if content_type:
        ct = content_type.lower()
        if 'webp' in ct:
            return '.webp'
        if 'jpeg' in ct or 'jpg' in ct:
            return '.jpg'
        if 'png' in ct:
            return '.png'
        if 'gif' in ct:
            return '.gif'
        if 'avif' in ct:
            return '.avif'

    # Try URL path (strip query string)
    path = urlparse(url).path.lower()
    for ext in ('.webp', '.jpg', '.jpeg', '.png', '.gif', '.avif'):
        if path.endswith(ext):
            return ext

    # Default
    return '.webp'


def download_one(url, save_path, platform):
    """Download a single image. Returns (save_path, size) or raises."""
    os.makedirs(os.path.dirname(save_path), exist_ok=True)

    referer = REFERERS.get(platform, '')
    headers = {'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36'}
    if referer:
        headers['Referer'] = referer

    req = urllib.request.Request(url, headers=headers)
    open_fn = _opener.open if _opener else urllib.request.urlopen
    with open_fn(req, timeout=15) as resp:
        data = resp.read()
        content_type = resp.headers.get('Content-Type', '')

    # If save_path has no extension yet, determine from response
    if save_path.endswith('.tmp'):
        ext = _guess_ext(url, content_type)
        save_path = save_path[:-4] + ext

    with open(save_path, 'wb') as f:
        f.write(data)

    return save_path, len(data)


def _process_item(item, total):
    """Process a single item: download cover and media images, return DB updates."""
    item_id = item['id']
    platform = item['platform']
    cover_url = item['cover_url'] or ''
    media_json_str = item['media_json'] or ''

    results = []  # list of (field, old_url, local_path)
    errors = []

    platform_dir = os.path.join(IMG_DIR, platform)
    os.makedirs(platform_dir, exist_ok=True)

    # Download cover image
    if cover_url and not cover_url.startswith('/images/'):
        # Normalize signed XHS CDN URLs to unsigned ones
        if platform == 'xiaohongshu':
            cover_url = _normalize_xhs_url(cover_url)
        ext = _guess_ext(cover_url)
        filename = f'{item_id}_0{ext}'
        save_path = os.path.join(platform_dir, filename)
        local_path = f'/images/{platform}/{filename}'

        if os.path.exists(save_path):
            results.append(('cover', local_path, 0))
        else:
            try:
                # Use .tmp extension; download_one will fix it
                actual_path, size = download_one(cover_url, save_path, platform)
                actual_filename = os.path.basename(actual_path)
                local_path = f'/images/{platform}/{actual_filename}'
                results.append(('cover', local_path, size))

                with _counter_lock:
                    _counter['done'] += 1
                    done = _counter['done']
                with _print_lock:
                    kb = size / 1024
                    print(f'[{done}/{total}] {platform}/{actual_filename} ({kb:.0f}KB)')
            except Exception as e:
                errors.append(f'cover {cover_url}: {e}')

    # Download media images
    media_urls = []
    media_is_list_of_strings = False
    if media_json_str:
        try:
            parsed = json.loads(media_json_str)
            if isinstance(parsed, list):
                # Could be list of strings (XHS) or list of dicts (Twitter)
                if parsed and isinstance(parsed[0], str):
                    media_urls = [(i, u) for i, u in enumerate(parsed)]
                    media_is_list_of_strings = True
                elif parsed and isinstance(parsed[0], dict):
                    media_urls = [(i, m.get('url', '')) for i, m in enumerate(parsed)]
        except (json.JSONDecodeError, TypeError):
            pass

    new_media = None
    if media_urls:
        try:
            parsed_media = json.loads(media_json_str)
        except (json.JSONDecodeError, TypeError):
            parsed_media = None

        if parsed_media:
            changed = False
            for idx, url in media_urls:
                if not url or url.startswith('/images/'):
                    continue
                # Normalize signed XHS CDN URLs
                if platform == 'xiaohongshu':
                    url = _normalize_xhs_url(url)
                ext = _guess_ext(url)
                filename = f'{item_id}_{idx + 1}{ext}'
                save_path = os.path.join(platform_dir, filename)
                local_path = f'/images/{platform}/{filename}'

                if os.path.exists(save_path):
                    # Already downloaded, just update path
                    if media_is_list_of_strings:
                        parsed_media[idx] = local_path
                    else:
                        parsed_media[idx]['url'] = local_path
                    changed = True
                    continue

                try:
                    actual_path, size = download_one(url, save_path, platform)
                    actual_filename = os.path.basename(actual_path)
                    local_path = f'/images/{platform}/{actual_filename}'

                    if media_is_list_of_strings:
                        parsed_media[idx] = local_path
                    else:
                        parsed_media[idx]['url'] = local_path
                    changed = True

                    with _counter_lock:
                        _counter['done'] += 1
                        done = _counter['done']
                    with _print_lock:
                        kb = size / 1024
                        print(f'[{done}/{total}] {platform}/{actual_filename} ({kb:.0f}KB)')
                except Exception as e:
                    errors.append(f'media[{idx}] {url}: {e}')

            if changed:
                new_media = json.dumps(parsed_media, ensure_ascii=False)

    return item_id, results, new_media, errors


def main():
    parser = argparse.ArgumentParser(description='Download and localize images')
    parser.add_argument('--platform', type=str, default=None,
                        help='Process only this platform')
    parser.add_argument('--limit', type=int, default=200,
                        help='Max items to process (default: 200)')
    parser.add_argument('--all', action='store_true',
                        help='Process ALL items with remote URLs (ignores --limit)')
    parser.add_argument('--proxy', type=str, default=None,
                        help='HTTP proxy URL (e.g. http://127.0.0.1:7890) for platforms behind GFW')
    parser.add_argument('--skip', type=str, default=None,
                        help='Comma-separated platforms to skip (e.g. twitter,reddit)')
    args = parser.parse_args()

    if args.all:
        args.limit = 999999  # effectively no limit

    # Set up proxy opener if provided
    global _opener
    if args.proxy:
        proxy_handler = urllib.request.ProxyHandler({
            'http': args.proxy,
            'https': args.proxy,
        })
        _opener = urllib.request.build_opener(proxy_handler)
        print(f'Using proxy: {args.proxy}')

    skip_platforms = set(args.skip.split(',')) if args.skip else set()
    # Auto-skip proxy-required platforms when no proxy is configured
    if not args.proxy:
        skip_platforms |= PROXY_PLATFORMS
        if PROXY_PLATFORMS - skip_platforms != PROXY_PLATFORMS:
            pass  # some were already in skip
        print(f'No proxy configured, skipping: {", ".join(PROXY_PLATFORMS)}')

    conn = db.get_conn()

    # Query items that have remote cover_url or media_json with remote URLs
    where_parts = [
        "(cover_url IS NOT NULL AND cover_url != '' AND cover_url NOT LIKE '/images/%')",
    ]
    params = []
    if args.platform:
        platform_clause = "AND platform = ?"
        params.append(args.platform)
    elif skip_platforms:
        placeholders = ','.join(['?'] * len(skip_platforms))
        platform_clause = f"AND platform NOT IN ({placeholders})"
        params.extend(skip_platforms)
    else:
        platform_clause = ""

    sql = f"""
        SELECT id, platform, cover_url, media_json FROM items
        WHERE ({' OR '.join(where_parts)})
        {platform_clause}
        ORDER BY fetched_at DESC
        LIMIT ?
    """
    params.append(args.limit)
    rows = conn.execute(sql, params).fetchall()
    items = [dict(r) for r in rows]

    if not items:
        print('No items need image download.')
        conn.close()
        return

    print(f'Downloading images for {len(items)} items...')
    _counter['done'] = 0

    # Count total images to download (for progress)
    total_images = 0
    for item in items:
        cover = item.get('cover_url', '') or ''
        if cover and not cover.startswith('/images/'):
            save_path = os.path.join(IMG_DIR, item['platform'], f"{item['id']}_0.webp")
            if not os.path.exists(save_path):
                total_images += 1
        mj = item.get('media_json', '') or ''
        if mj:
            try:
                parsed = json.loads(mj)
                if isinstance(parsed, list):
                    for i, entry in enumerate(parsed):
                        url = entry if isinstance(entry, str) else (entry.get('url', '') if isinstance(entry, dict) else '')
                        if url and not url.startswith('/images/'):
                            save_path = os.path.join(IMG_DIR, item['platform'], f"{item['id']}_{i+1}.webp")
                            if not os.path.exists(save_path):
                                total_images += 1
            except (json.JSONDecodeError, TypeError):
                pass

    print(f'Estimated {total_images} images to download.')

    # Process items with thread pool
    with ThreadPoolExecutor(max_workers=10) as pool:
        futures = {pool.submit(_process_item, item, total_images): item for item in items}

        for future in as_completed(futures):
            try:
                item_id, results, new_media, errors = future.result()
            except Exception as e:
                print(f'Error processing item: {e}')
                continue

            # Update DB
            update_parts = []
            update_params = []

            for r in results:
                if r[0] == 'cover':
                    update_parts.append('cover_url = ?')
                    update_params.append(r[1])

            if new_media is not None:
                update_parts.append('media_json = ?')
                update_params.append(new_media)

            if update_parts:
                update_params.append(item_id)
                conn.execute(
                    f"UPDATE items SET {', '.join(update_parts)} WHERE id = ?",
                    update_params
                )
                conn.commit()

            for err in errors:
                with _print_lock:
                    print(f'  WARN {item_id}: {err}')

    conn.close()
    print(f'\nDone. Downloaded {_counter["done"]} images.')


if __name__ == '__main__':
    main()
