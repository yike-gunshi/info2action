#!/usr/bin/env python3
"""Fetch RSS, Hacker News, Reddit, and GitHub Trending into JSON files.
Usage: python3 fetch_feeds.py [--rss] [--hn] [--reddit] [--github]
  No flags = fetch all four.
"""
import json, logging, os, sys, time, hashlib
from datetime import datetime, timezone

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def data_dir():
    return os.environ.get('INFO2ACTION_DATA_DIR') or os.path.join(BASE, 'data')


def source_dir(*parts):
    return os.path.join(data_dir(), 'sources', *parts)

# Load config (graceful on missing keys)
with open(os.path.join(BASE, 'config', 'config.json')) as f:
    CONFIG = json.load(f)

logger = logging.getLogger(__name__)


# ============================================================
# RSS
# ============================================================
def fetch_rss():
    """Fetch all configured RSS feeds."""
    import feedparser, requests

    feeds_cfg = CONFIG.get('rss', {}).get('feeds', [])
    if not feeds_cfg:
        print("  ⚠️  No RSS feeds in config.json → rss.feeds")
        return

    out_dir = source_dir('rss')
    os.makedirs(out_dir, exist_ok=True)

    for feed_cfg in feeds_cfg:
        url = feed_cfg['url']
        name = feed_cfg.get('name', url)
        safe_name = feed_cfg.get('slug') or name.replace(' ', '_').replace('/', '_')[:50]

        print(f"  Fetching {name}...")
        try:
            # Use requests to fetch (handles SSL properly), then parse content
            r = requests.get(url, headers={'User-Agent': 'info2action/1.0'}, timeout=15)
            d = feedparser.parse(r.content)
            items = []
            for entry in d.entries:
                content_val = ''
                if entry.get('content'):
                    content_val = entry['content'][0].get('value', '')

                items.append({
                    'id': entry.get('id', entry.get('link', '')),
                    'title': entry.get('title', ''),
                    'link': entry.get('link', ''),
                    'summary': entry.get('summary', ''),
                    'content': content_val,
                    'author': entry.get('author', d.feed.get('title', '')),
                    'published': entry.get('published', ''),
                    'tags': [t.get('term', '') for t in entry.get('tags', [])],
                })

            out_path = os.path.join(out_dir, f'{safe_name}.json')
            with open(out_path, 'w') as f:
                json.dump({
                    'feed_title': d.feed.get('title', name),
                    'feed_url': url,
                    'items': items,
                }, f, ensure_ascii=False, indent=2)
            print(f"  ✅ {name}: {len(items)} items")
        except Exception as e:
            print(f"  ❌ {name}: {e}")


# ============================================================
# HACKER NEWS
# ============================================================
def fetch_hackernews():
    """Fetch top HN stories via Firebase API."""
    import requests

    count = CONFIG.get('hackernews', {}).get('count', 30)
    out_dir = source_dir('hackernews')
    os.makedirs(out_dir, exist_ok=True)

    print(f"  Fetching top {count} stories...")
    try:
        r = requests.get(
            'https://hacker-news.firebaseio.com/v0/topstories.json', timeout=15
        )
        story_ids = r.json()[:count]

        items = []
        for sid in story_ids:
            r = requests.get(
                f'https://hacker-news.firebaseio.com/v0/item/{sid}.json', timeout=10
            )
            story = r.json()
            if story and story.get('type') == 'story':
                items.append(story)
            time.sleep(0.1)

        out_path = os.path.join(out_dir, 'top.json')
        with open(out_path, 'w') as f:
            json.dump(items, f, ensure_ascii=False, indent=2)
        print(f"  ✅ HN top: {len(items)} stories")
    except Exception as e:
        print(f"  ❌ HN: {e}")


# ============================================================
# REDDIT
# ============================================================
def fetch_reddit():
    """Fetch hot posts from configured subreddits."""
    import requests

    subs = CONFIG.get('reddit', {}).get('subreddits', [])
    count = CONFIG.get('reddit', {}).get('count', 25)
    if not subs:
        print("  ⚠️  No subreddits in config.json → reddit.subreddits")
        return

    out_dir = source_dir('reddit')
    os.makedirs(out_dir, exist_ok=True)
    headers = {'User-Agent': 'info2action/1.0'}

    for sub in subs:
        print(f"  Fetching r/{sub}...")
        try:
            r = requests.get(
                f'https://www.reddit.com/r/{sub}/hot.json?limit={count}',
                headers=headers, timeout=15,
            )
            # Reddit 对数据中心 IP(如 Vultr Tokyo)返 403 + HTML 而非 JSON
            # 之前没 status check, r.json() 抛 "Expecting value" 被 except 静默吞掉
            # → fetch_run 看不到失败,运维盲点。现在显式拒绝 non-200,让错误冒到 stderr
            # 和 fetch_runs.error_msg。Reddit OAuth 是长期解,见 docs/ops/runbook §5
            if r.status_code != 200:
                print(f"  ❌ r/{sub}: HTTP {r.status_code} (Reddit may block this IP — see runbook §5.6 Reddit IP block)")
                continue
            data = r.json()
            posts = []
            for child in data.get('data', {}).get('children', []):
                post = child.get('data', {})
                thumbnail = post.get('thumbnail', '')
                if thumbnail in ('self', 'default', 'nsfw', 'spoiler', ''):
                    thumbnail = ''
                # 2026-04-29: 优先取高清原图替代 140×80 缩略图
                # 优先级: preview.images[0].source.url(800-1200px 原图) >
                #         url_overridden_by_dest(图片帖直链 i.redd.it/xxx.jpg) >
                #         post.url(post_hint=image 时是直链) > thumbnail(140 缩略图兜底)
                preview_imgs = (post.get('preview') or {}).get('images') or []
                preview_src = ''
                if preview_imgs:
                    src = preview_imgs[0].get('source') or {}
                    # reddit JSON 把 & 编码成 &amp;,要还原
                    preview_src = (src.get('url') or '').replace('&amp;', '&')
                direct_url = post.get('url_overridden_by_dest') or ''
                post_hint = post.get('post_hint', '')
                if not preview_src and post_hint == 'image':
                    preview_src = direct_url
                cover = preview_src or thumbnail
                posts.append({
                    'id': post.get('id', ''),
                    'title': post.get('title', ''),
                    'selftext': post.get('selftext', ''),
                    'author': post.get('author', ''),
                    'url': post.get('url', ''),
                    'permalink': post.get('permalink', ''),
                    'score': post.get('score', 0),
                    'upvote_ratio': post.get('upvote_ratio', 0),
                    'num_comments': post.get('num_comments', 0),
                    'created_utc': post.get('created_utc', 0),
                    'thumbnail': cover,
                    'link_flair_text': post.get('link_flair_text', ''),
                    'is_self': post.get('is_self', False),
                    'subreddit': sub,
                })

            out_path = os.path.join(out_dir, f'{sub}.json')
            with open(out_path, 'w') as f:
                json.dump(posts, f, ensure_ascii=False, indent=2)
            print(f"  ✅ r/{sub}: {len(posts)} posts")
            time.sleep(2)  # Reddit rate limit
        except Exception as e:
            print(f"  ❌ r/{sub}: {e}")


# ============================================================
# GITHUB TRENDING (v16.0)
# ============================================================
# v16.0 决策（feat/channels-simplify-v2，2026-05-12）：
# - 删除编程语言遍历（rust / python / typescript）
# - 改用 spoken_language_code 维度（zh + 全量），完全覆盖中文/全球开发者视角
# - awesome 仓库走单独 fetch_github_awesome_repos()，不混在 trending 里
def fetch_github_trending():
    """Fetch GitHub trending repos by scraping the trending page.

    v16.0: 按 spoken_language_code 维度抓（zh + 空字符串=全量），
    不再按编程语言切分。
    """
    import requests, re

    cfg = CONFIG.get('github_trending', {})
    # v16.0 配置 key = spoken_languages；老 key 'languages' 不再读
    spoken_langs = cfg.get('spoken_languages', ['zh', ''])
    since = cfg.get('since', 'daily')
    count = cfg.get('count', 25)

    # v16.0 PRD §4.9.3: 每个 GitHub item 必须 fetch README，200k token 上限从尾部截
    # 读 github_tracking.json 拿 readme_max_tokens；缺文件用默认 200000
    tracking_cfg = {}
    cfg_path = os.path.join(BASE, 'config', 'github_tracking.json')
    if os.path.exists(cfg_path):
        try:
            with open(cfg_path) as _f:
                tracking_cfg = json.load(_f)
        except Exception as _e:
            logger.warning("trending: github_tracking.json parse error: %s", _e)
    # PL-2(B6): 默认 200k tokens(≈800KB/仓库)曾把 items 行撑爆——巨块横穿 upsert 语句/enrich 内存/read-model 重建;摘要用途 48KB 足够
    readme_max_tokens = int(tracking_cfg.get('readme_max_tokens', 12_000))

    out_dir = source_dir('github')
    os.makedirs(out_dir, exist_ok=True)

    all_repos = []
    seen = set()

    for spoken in spoken_langs:
        label = spoken or 'global'
        print(f"  Fetching trending [spoken={label}] ({since})...")
        try:
            url = f'https://github.com/trending?spoken_language_code={spoken}&since={since}'
            r = requests.get(url, headers={'User-Agent': 'info2action/1.0'}, timeout=15)
            html = r.text

            # Parse repo entries from HTML
            # Each repo is in <article class="Box-row">
            articles = re.findall(
                r'<article class="Box-row">(.*?)</article>', html, re.DOTALL
            )
            for article in articles:
                # Full name: /owner/repo — skip /login, /sponsors, /apps, single-segment paths
                all_hrefs = re.findall(r'href="(/[^"]+)"', article)
                full_name = ''
                for href in all_hrefs:
                    path = href.strip('/')
                    if '/' not in path:
                        continue
                    if path.startswith(('login', 'sponsors/', 'apps/')):
                        continue
                    if path.endswith(('/stargazers', '/forks')):
                        continue
                    full_name = path
                    break
                if not full_name:
                    continue
                if full_name in seen:
                    continue
                seen.add(full_name)

                # Description
                m_desc = re.search(r'<p class="[^"]*">(.*?)</p>', article, re.DOTALL)
                desc = re.sub(r'<[^>]+>', '', m_desc.group(1)).strip() if m_desc else ''

                # Language
                m_lang = re.search(r'itemprop="programmingLanguage">(.*?)<', article)
                repo_lang = m_lang.group(1).strip() if m_lang else ''

                # Stars today
                m_stars_today = re.search(r'([\d,]+)\s+stars\s+today', article)
                stars_today = int(m_stars_today.group(1).replace(',', '')) if m_stars_today else 0

                # Total stars — strip SVG tags first, then extract number
                m_stars = re.findall(r'href="/[^"]+/stargazers"[^>]*>(.*?)</a>', article, re.DOTALL)
                total_stars = 0
                if m_stars:
                    text_only = re.sub(r'<[^>]+>', '', m_stars[0]).strip()
                    s = text_only.replace(',', '')
                    total_stars = int(s) if s.isdigit() else 0

                # Forks — strip SVG tags first
                m_forks = re.findall(r'href="/[^"]+/forks"[^>]*>(.*?)</a>', article, re.DOTALL)
                total_forks = 0
                if m_forks:
                    text_only = re.sub(r'<[^>]+>', '', m_forks[0]).strip()
                    s = text_only.replace(',', '')
                    total_forks = int(s) if s.isdigit() else 0

                # v16.0: fetch README，失败 robust（readme=None + readme_error 记原因）
                readme_text, readme_err = _fetch_readme(full_name)
                readme_safe = (
                    _truncate_readme_safe(readme_text, readme_max_tokens) if readme_text else ''
                )

                all_repos.append({
                    'full_name': full_name,
                    'description': desc,
                    'language': repo_lang,
                    'stars': total_stars,
                    'forks': total_forks,
                    'stars_today': stars_today,
                    'url': f'https://github.com/{full_name}',
                    'spoken_language': spoken or 'global',
                    'since': since,
                    'readme': readme_safe,
                    'readme_error': readme_err,
                })

            spoken_count = len([r for r in all_repos if r['spoken_language'] == (spoken or 'global')])
            print(f"  ✅ trending [spoken={label}]: {spoken_count} repos")
            time.sleep(1)
        except Exception as e:
            print(f"  ❌ trending [spoken={label}]: {e}")

    # Deduplicate and limit
    all_repos = all_repos[:count]
    out_path = os.path.join(out_dir, 'trending.json')
    with open(out_path, 'w') as f:
        json.dump(all_repos, f, ensure_ascii=False, indent=2)
    print(f"  📊 Total: {len(all_repos)} unique repos")


# ============================================================
# GITHUB AWESOME REPOS (v16.0 新增)
# ============================================================
# 决策稿：docs/讨论/频道精简/2026-05-12-频道精简-需求讨论.md §4.3
# - 把 config/github_tracking.json 配置的 awesome_repos 当作普通 repo 抓
# - 本轮不实施 git diff 增量抓取（用户已锁定）
# - 失败 robust：config 不存在 → 返回 [] + 记 warning，不 raise
def fetch_github_awesome_repos():
    """Fetch each configured awesome repo as a single GitHub repo item.

    Reads config/github_tracking.json:
        {"awesome_repos": ["owner/repo", ...]}

    Returns a list of repo dicts with the same schema as trending repos
    plus a `readme` field (full text after UTF-8 safe truncation).
    """
    import requests

    cfg_path = os.path.join(BASE, 'config', 'github_tracking.json')
    if not os.path.exists(cfg_path):
        msg = f"config/github_tracking.json not found at {cfg_path}; awesome repos skipped"
        logger.warning(msg)
        print(f"  ⚠️  {msg}")
        return []

    try:
        with open(cfg_path) as f:
            tracking_cfg = json.load(f)
    except Exception as e:
        logger.warning("failed to parse github_tracking.json: %s", e)
        print(f"  ❌ github_tracking.json parse error: {e}")
        return []

    awesome_list = tracking_cfg.get('awesome_repos', []) or []
    if not awesome_list:
        logger.warning("github_tracking.json has empty awesome_repos list")
        return []

    # Token cap for README — keep generous tail (200k tokens per decision)
    max_tokens = int(tracking_cfg.get('readme_max_tokens', 12_000))  # PL-2(B6)

    out_dir = source_dir('github')
    os.makedirs(out_dir, exist_ok=True)

    items = []
    for full_name in awesome_list:
        if '/' not in full_name:
            logger.warning("invalid awesome repo entry: %s", full_name)
            continue
        owner, repo = full_name.split('/', 1)
        api_url = f'https://api.github.com/repos/{full_name}'
        print(f"  Fetching awesome repo {full_name}...")
        try:
            r = requests.get(
                api_url,
                headers={'User-Agent': 'info2action/1.0',
                         'Accept': 'application/vnd.github.v3+json'},
                timeout=15,
            )
            if r.status_code != 200:
                print(f"  ❌ {full_name}: HTTP {r.status_code}")
                continue
            data = r.json() or {}
            readme_text, readme_err = _fetch_readme(full_name)
            readme_safe = _truncate_readme_safe(readme_text, max_tokens) if readme_text else ''

            items.append({
                'full_name': data.get('full_name', full_name),
                'description': (data.get('description') or '')[:500],
                'language': data.get('language', ''),
                'stars': data.get('stargazers_count', 0),
                'forks': data.get('forks_count', 0),
                'stars_today': 0,  # awesome 仓库无 trending 当日数据
                'url': data.get('html_url', f'https://github.com/{full_name}'),
                'pushed_at': data.get('pushed_at', ''),
                'source_type': 'awesome',
                'readme': readme_safe,
                'readme_error': readme_err,
            })
            time.sleep(1)
        except Exception as e:
            print(f"  ❌ {full_name}: {e}")
            continue

    out_path = os.path.join(out_dir, 'awesome.json')
    with open(out_path, 'w') as f:
        json.dump(items, f, ensure_ascii=False, indent=2)
    print(f"  📊 Awesome repos: {len(items)} items")
    return items


# ============================================================
# README HELPERS (v16.0 新增)
# ============================================================
def _fetch_readme(repo_full_name):
    """Fetch raw README from GitHub (main → master fallback).

    Returns (text, error). On success: (text, None). On failure: (None, "<reason>").
    Caller decides whether to ingest.
    """
    import requests

    branches = ['main', 'master']
    last_err = None
    for branch in branches:
        url = f'https://raw.githubusercontent.com/{repo_full_name}/{branch}/README.md'
        try:
            r = requests.get(
                url,
                headers={'User-Agent': 'info2action/1.0'},
                timeout=15,
            )
            if r.status_code == 200 and r.text:
                return r.text, None
            last_err = f"HTTP {r.status_code} on {branch} branch"
        except Exception as e:
            last_err = f"{branch}: {type(e).__name__}: {e}"
            continue
    return None, last_err or 'all branches failed'


def _truncate_readme_safe(text, max_tokens):
    """Tail-truncate text to roughly max_tokens, never splitting a multi-byte UTF-8 char.

    决策稿：「从尾部截」= keep tail, drop head（保留最后部分）。
    Token estimator: 粗算 bytes / 4（与 LLM 经验一致；CJK 实际偏紧，更保守）。

    Bug 防御：UTF-8 多字节字符的中间字节首位是 0b10。若截断点落到中间字节，
    需把切点向前推到一个合法的字符起始字节（首位 != 0b10）。
    """
    if not text:
        return text or ''

    encoded = text.encode('utf-8')
    max_bytes = max_tokens * 4
    if len(encoded) <= max_bytes:
        return text

    # Tail-truncation: keep last `max_bytes` bytes
    cut = len(encoded) - max_bytes  # index of first byte to keep
    # Walk forward until cut byte is a legal UTF-8 start (high bit not 10xxxxxx)
    while cut < len(encoded) and (encoded[cut] & 0b1100_0000) == 0b1000_0000:
        cut += 1

    tail = encoded[cut:]
    # Defensive decode — should always succeed because cut now lands on a char boundary
    return tail.decode('utf-8', errors='ignore')


# ============================================================
# MAIN
# ============================================================
if __name__ == '__main__':
    flags = set(sys.argv[1:])
    run_all = not flags

    print("=" * 60)
    print("  信息雷达 — RSS/HN/Reddit/GitHub 抓取")
    print("=" * 60)

    if run_all or '--rss' in flags:
        print("\n📡 RSS Feeds...")
        fetch_rss()

    if run_all or '--hn' in flags:
        print("\n🔶 Hacker News...")
        fetch_hackernews()

    if run_all or '--reddit' in flags:
        print("\n🤖 Reddit...")
        fetch_reddit()

    if run_all or '--github' in flags:
        print("\n🐙 GitHub Trending...")
        fetch_github_trending()

    print("\n✅ 抓取完成!")
