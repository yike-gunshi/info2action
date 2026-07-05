#!/usr/bin/env python3
"""Fetch WayToAGI daily updates from Feishu wiki via lark-cli.

Fetches the main wiki page, parses daily update entries,
then fetches each article's full text and cover image.

Usage: python3 fetch_waytoagi.py
Output: data/sources/waytoagi/daily.json
"""
import json, os, re, shutil, subprocess, sys, time, urllib.request, urllib.parse
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.environ.get("INFO2ACTION_DATA_DIR") or os.path.join(BASE, "data")
LARK_CLI = (
    os.environ.get("LARK_CLI")
    or shutil.which("lark-cli")
    or os.path.expanduser("~/claudecode_workspace/工具/lark-cli/lark-cli")
)
LARK_DOC_IDENTITY = (os.environ.get("LARK_DOC_IDENTITY") or "bot").strip() or "bot"
LARK_DOC_API_VERSION = (os.environ.get("LARK_DOC_API_VERSION") or "v1").strip() or "v1"
WIKI_URL = "https://waytoagi.feishu.cn/wiki/QPe5w5g7UisbEkkow8XcDmOpn8e"
OUT_DIR = os.path.join(DATA_DIR, "sources", "waytoagi")
IMG_DIR = os.path.join(BASE, "data", "images", "waytoagi")


def _lark_identity_candidates():
    if LARK_DOC_IDENTITY.lower() == "auto":
        return ["bot", "user"]
    candidates = [LARK_DOC_IDENTITY]
    if LARK_DOC_IDENTITY != "bot":
        candidates.append("bot")
    return candidates


def _extract_markdown(data):
    if not data.get("ok"):
        return None
    payload = data.get("data") or {}
    if not isinstance(payload, dict):
        return None
    markdown = payload.get("markdown")
    if markdown:
        return markdown
    document = payload.get("document") or {}
    if isinstance(document, dict):
        return document.get("markdown") or document.get("content")
    return None


def fetch_doc_markdown(url):
    """Fetch a Feishu document's markdown via lark-cli."""
    for identity in _lark_identity_candidates():
        result = subprocess.run(
            [
                LARK_CLI, "docs", "+fetch",
                "--doc", url,
                "--as", identity,
                "--api-version", LARK_DOC_API_VERSION,
                "--format", "json",
            ],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0 or not result.stdout.strip():
            err = (result.stderr or result.stdout or "").strip()
            print(
                f"  ⚠️  lark-cli fetch failed ({identity}): {err[:300]}",
                file=sys.stderr,
                flush=True,
            )
            continue
        try:
            data = json.loads(result.stdout)
        except json.JSONDecodeError:
            print(
                f"  ⚠️  lark-cli returned non-JSON output ({identity}): {result.stdout[:300]}",
                file=sys.stderr,
                flush=True,
            )
            continue
        markdown = _extract_markdown(data)
        if markdown:
            return markdown
        err = ((data.get("error") or {}).get("message") or "empty markdown")
        print(f"  ⚠️  lark-cli fetch failed ({identity}): {err[:300]}", file=sys.stderr, flush=True)
    return None


def clean_feishu_markdown(md):
    """Strip Feishu-specific markup tags, keeping plain text."""
    text = re.sub(r'<image\s+[^/]*/>', '', md)
    text = re.sub(r'</?(?:callout|text|grid|column|mention-doc)[^>]*>', '', text)
    text = re.sub(r'<[^>]+/>', '', text)
    text = re.sub(r'\{[^}]*\}', '', text)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


def extract_wechat_url(content):
    """Extract WeChat article URL from content."""
    # URL-encoded form in markdown link: (https%3A%2F%2Fmp.weixin.qq.com%2Fs%2F...)
    m = re.search(r'https%3A%2F%2Fmp\.weixin\.qq\.com%2Fs%2F([A-Za-z0-9_-]+)', content)
    if m:
        return urllib.parse.unquote(m.group(0))
    # Plain URL form
    m = re.search(r'https?://mp\.weixin\.qq\.com/s/[A-Za-z0-9_-]{16,}', content)
    return m.group(0) if m else None


def fetch_og_image(url):
    """Fetch og:image from a web page."""
    try:
        req = urllib.request.Request(url, headers={
            'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)',
        })
        html = urllib.request.urlopen(req, timeout=10).read().decode('utf-8', errors='ignore')
        # Try og:image first, then msg_cdn_url (WeChat-specific)
        for pattern in [
            r'<meta\s+property="og:image"\s+content="([^"]+)"',
            r'var\s+msg_cdn_url\s*=\s*"([^"]+)"',
        ]:
            m = re.search(pattern, html)
            if m:
                return m.group(1)
    except Exception:
        pass
    return None


def extract_first_image_url(content):
    """Extract the first image URL from article content as fallback."""
    # Common image hosting patterns
    m = re.search(r'https?://[^\s\)\]"]+\.(?:png|jpg|jpeg|webp|gif)(?:\?[^\s\)\]"]*)?', content)
    return m.group(0) if m else None


def fetch_cover_image(item):
    """Fetch cover image for an article. Returns image URL or None."""
    content = item.get("content", "")

    # Strategy 1: WeChat og:image (most reliable, high quality)
    wechat_url = extract_wechat_url(content)
    if wechat_url:
        img = fetch_og_image(wechat_url)
        if img:
            return img

    # Strategy 2: First image URL in content
    img = extract_first_image_url(content)
    if img:
        return img

    return None


def parse_daily_updates(markdown):
    """Parse '近7日更新日志' section into structured items."""
    log_start = markdown.find("更新日志")
    if log_start == -1:
        print("  ⚠️  未找到更新日志段落", flush=True)
        return []

    md = markdown[log_start:]
    year = datetime.now().year

    date_pattern = re.compile(r"#+\s*(\d+)\s*月\s*(\d+)\s*日")
    date_matches = list(date_pattern.finditer(md))

    article_pattern = re.compile(
        r"《<mention-doc\s+token=\"([^\"]+)\"\s+type=\"wiki\">([^<]+)</mention-doc>》([^\n]+)"
    )

    items = []
    for i, dm in enumerate(date_matches):
        month, day = int(dm.group(1)), int(dm.group(2))
        date_str = f"{year}-{month:02d}-{day:02d}"

        start = dm.end()
        end = date_matches[i + 1].start() if i + 1 < len(date_matches) else len(md)
        section = md[start:end]

        for am in article_pattern.finditer(section):
            token, title, summary = am.group(1), am.group(2).strip(), am.group(3).strip()
            items.append({
                "id": token,
                "title": title,
                "summary": summary,
                "date": date_str,
                "url": f"https://waytoagi.feishu.cn/wiki/{token}",
            })

    return items


def fetch_full_text(items):
    """Fetch full article text for each item from its wiki sub-page."""
    total = len(items)
    for i, item in enumerate(items):
        url = item["url"]
        print(f"  [{i+1}/{total}] {item['title'][:40]}...", flush=True)
        try:
            md = fetch_doc_markdown(url)
            if md:
                item["content"] = clean_feishu_markdown(md)
            else:
                print(f"    ⚠️  fetch failed, using summary as fallback", flush=True)
                item["content"] = item["summary"]
        except Exception as e:
            print(f"    ⚠️  {e}", flush=True)
            item["content"] = item["summary"]
        if i < total - 1:
            time.sleep(0.3)


def fetch_cover_images(items):
    """Fetch cover images for all items concurrently."""
    total = len(items)
    found = 0

    def _fetch(item):
        return item["id"], fetch_cover_image(item)

    with ThreadPoolExecutor(max_workers=5) as pool:
        futures = {pool.submit(_fetch, item): item for item in items}
        for future in as_completed(futures):
            token, img_url = future.result()
            if img_url:
                for item in items:
                    if item["id"] == token:
                        item["cover_url"] = img_url
                        break
                found += 1

    print(f"  🖼️  封面图: {found}/{total} 篇有图", flush=True)


def main():
    print("🔖 WayToAGI — 抓取每日更新...", flush=True)

    md = fetch_doc_markdown(WIKI_URL)
    if not md:
        print("  ❌ 主页面抓取失败", flush=True)
        sys.exit(1)

    items = parse_daily_updates(md)
    if not items:
        print("  ⚠️  未解析到文章", flush=True)
        return

    print(f"  📄 解析到 {len(items)} 篇文章，开始抓取全文...", flush=True)
    fetch_full_text(items)

    print(f"  🖼️  抓取封面图...", flush=True)
    fetch_cover_images(items)

    os.makedirs(OUT_DIR, exist_ok=True)
    out_path = os.path.join(OUT_DIR, "daily.json")
    with open(out_path, "w") as f:
        json.dump({"source": "waytoagi", "fetched_at": datetime.now(timezone.utc).isoformat(), "items": items},
                  f, ensure_ascii=False, indent=2)

    print(f"  ✅ WayToAGI: {len(items)} articles saved", flush=True)


if __name__ == "__main__":
    main()
