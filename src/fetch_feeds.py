#!/usr/bin/env python3
"""Fetch RSS, WeChat RSS, Hacker News, Reddit, and GitHub Trending into JSON files.
Usage: python3 fetch_feeds.py [--rss] [--wechat] [--hn] [--reddit] [--github]
  No flags = fetch all.
"""
import html, json, logging, os, re, sys, time, hashlib
from datetime import datetime, timezone
from urllib.parse import urlparse

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def data_dir():
    return os.environ.get('INFO2ACTION_DATA_DIR') or os.path.join(BASE, 'data')


def source_dir(*parts):
    return os.path.join(data_dir(), 'sources', *parts)

# Load config (graceful on missing keys)
with open(os.path.join(BASE, 'config', 'config.json')) as f:
    CONFIG = json.load(f)

logger = logging.getLogger(__name__)


def _registry_sources(platform, conn=None):
    import db
    import remote_db

    if remote_db.fetch_write_to_remote():
        return remote_db.list_active_sources_remote(platform)

    own_conn = conn is None
    if conn is None:
        conn = db.get_conn()
    try:
        return db.list_active_sources(conn, platform)
    finally:
        if own_conn:
            conn.close()


def _log_registry_count(platform, count):
    msg = f"从注册表读到 {count} 个 active {platform} 源"
    logger.info(msg)
    print(f"  ℹ️  {msg}")


def _log_registry_fallback(platform, reason):
    msg = f"{platform} 注册表{reason}，回退 config"
    logger.warning(msg)
    print(f"  ⚠️  {msg}")


def _without_source_id(rows):
    out = []
    for row in rows:
        if isinstance(row, dict):
            item = dict(row)
            item.pop('source_id', None)
            out.append(item)
        else:
            out.append(row)
    return out


def _record_source_fetch_result(source_id, ok, error=None):
    if source_id is None:
        return
    try:
        import ingest

        ingest.record_source_fetch_result_current_backend(source_id, ok=ok, error=error)
    except Exception as e:
        print(f"  ⚠️  source result tracking skipped for {source_id}: {e}")


def _is_http_feed_url(value):
    parsed = urlparse(str(value or ""))
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


def _config_rss_feeds():
    return CONFIG.get('rss', {}).get('feeds', [])


def _active_rss_feed_sources(conn=None):
    fallback = _config_rss_feeds()
    try:
        sources = _registry_sources('rss', conn)
        if not sources:
            _log_registry_fallback('rss', '为空')
            return fallback
        _log_registry_count('rss', len(sources))
        feeds = []
        for source in sources:
            url = source['source_key']
            name = source.get('display_name') or url
            slug = (source.get('config_json') or {}).get('slug')
            if not slug:
                slug = name.replace(' ', '_').replace('/', '_')[:50]
            feeds.append({'source_id': source.get('id'), 'name': name, 'slug': slug, 'url': url})
        return feeds
    except Exception as e:
        _log_registry_fallback('rss', f'异常: {e}')
        return fallback


def _active_rss_feeds(conn=None):
    return _without_source_id(_active_rss_feed_sources(conn))


def _config_reddit_subreddits():
    return CONFIG.get('reddit', {}).get('subreddits', [])


def _active_reddit_sources(conn=None):
    fallback = [{'subreddit': sub} for sub in _config_reddit_subreddits()]
    try:
        sources = _registry_sources('reddit', conn)
        if not sources:
            _log_registry_fallback('reddit', '为空')
            return fallback
        _log_registry_count('reddit', len(sources))
        return [
            {'source_id': source.get('id'), 'subreddit': source['source_key']}
            for source in sources
        ]
    except Exception as e:
        _log_registry_fallback('reddit', f'异常: {e}')
        return fallback


def _active_reddit_subreddits(conn=None):
    return [source['subreddit'] for source in _active_reddit_sources(conn)]


def _active_github_awesome_repo_sources(conn=None, tracking_cfg=None):
    tracking_cfg = tracking_cfg or {}
    fallback = [{'full_name': repo} for repo in (tracking_cfg.get('awesome_repos', []) or [])]
    try:
        sources = _registry_sources('github_repo', conn)
        if not sources:
            _log_registry_fallback('github_repo', '为空')
            return fallback
        _log_registry_count('github_repo', len(sources))
        return [
            {'source_id': source.get('id'), 'full_name': source['source_key']}
            for source in sources
        ]
    except Exception as e:
        _log_registry_fallback('github_repo', f'异常: {e}')
        return fallback


def _active_github_awesome_repos(conn=None, tracking_cfg=None):
    return [
        source['full_name']
        for source in _active_github_awesome_repo_sources(conn, tracking_cfg)
    ]


def _active_wechat_feed_sources(conn=None):
    try:
        sources = _registry_sources('wechat_mp', conn)
        if not sources:
            msg = "wechat_mp 注册表为空，跳过公众号 RSS"
            logger.info(msg)
            print(f"  ℹ️  {msg}")
            return []
        _log_registry_count('wechat_mp', len(sources))
        feeds = []
        for source in sources:
            url = source['source_key']
            config = source.get('config_json') or {}
            backend = config.get('backend') if isinstance(config, dict) else None
            if backend == 'lingowhale' or not _is_http_feed_url(url):
                continue
            name = source.get('display_name') or url
            feeds.append({'source_id': source.get('id'), 'name': name, 'url': url})
        return feeds
    except Exception as e:
        msg = f"wechat_mp 注册表异常: {e}，跳过公众号 RSS"
        logger.warning(msg)
        print(f"  ⚠️  {msg}")
        return []


def _active_wechat_feeds(conn=None):
    return _without_source_id(_active_wechat_feed_sources(conn))


def _wechat_feed_safe_name(name, url):
    base = ''.join(
        ch if ch.isalnum() or ch in ('_', '-') else '_'
        for ch in (name or 'wechat')
    ).strip('_')[:40] or 'wechat'
    digest = hashlib.md5(url.encode()).hexdigest()[:12]
    return f'{base}_{digest}'


# ============================================================
# RSS
# ============================================================
def fetch_rss():
    """Fetch all configured RSS feeds."""
    import feedparser, requests

    feeds_cfg = _active_rss_feed_sources()
    if not feeds_cfg:
        print("  ⚠️  No RSS feeds in config.json → rss.feeds")
        return

    out_dir = source_dir('rss')
    os.makedirs(out_dir, exist_ok=True)

    for feed_cfg in feeds_cfg:
        source_id = feed_cfg.get('source_id') if isinstance(feed_cfg, dict) else None
        url = feed_cfg['url']
        name = feed_cfg.get('name', url)
        safe_name = feed_cfg.get('slug') or name.replace(' ', '_').replace('/', '_')[:50]

        print(f"  Fetching {name}...")
        try:
            # Use requests to fetch (handles SSL properly), then parse content
            r = requests.get(url, headers={'User-Agent': 'info2action/1.0'}, timeout=15)
            r.raise_for_status()
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
            _record_source_fetch_result(source_id, True)
            print(f"  ✅ {name}: {len(items)} items")
        except Exception as e:
            _record_source_fetch_result(source_id, False, e)
            print(f"  ❌ {name}: {e}")


# ============================================================
# WECHAT RSS
# ============================================================
def fetch_wechat_rss():
    """Fetch active WeChat MP RSS feeds from the sources registry."""
    import feedparser, requests

    feeds_cfg = _active_wechat_feed_sources()
    if not feeds_cfg:
        print("  ⚠️  No active wechat_mp RSS feeds in sources registry")
        return

    out_dir = source_dir('wechat')
    os.makedirs(out_dir, exist_ok=True)

    for feed_cfg in feeds_cfg:
        source_id = feed_cfg.get('source_id') if isinstance(feed_cfg, dict) else None
        url = feed_cfg['url']
        name = feed_cfg.get('name', url)
        safe_name = _wechat_feed_safe_name(name, url)

        print(f"  Fetching {name}...")
        try:
            r = requests.get(url, headers={'User-Agent': 'info2action/1.0'}, timeout=15)
            r.raise_for_status()
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
                    'author': entry.get('author', d.feed.get('title', name)),
                    'published': entry.get('published', ''),
                    'tags': [t.get('term', '') for t in entry.get('tags', [])],
                })

            out_path = os.path.join(out_dir, f'{safe_name}.json')
            with open(out_path, 'w') as f:
                json.dump({
                    'feed_title': d.feed.get('title', name),
                    'feed_url': url,
                    'source_name': name,
                    'items': items,
                }, f, ensure_ascii=False, indent=2)
            _record_source_fetch_result(source_id, True)
            print(f"  ✅ {name}: {len(items)} items")
        except Exception as e:
            _record_source_fetch_result(source_id, False, e)
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
# BF-0716-1: 生产数据中心 IP(Vultr Tokyo)被 reddit 全部 JSON 端点 403
# (www/old/api,与 UA 无关),6 个源全走 RSS 兜底;而该 IP 未认证 RSS 配额是
# 每 ~60s 窗口 1 次请求(x-ratelimit-remaining: 0.0)。原实现以 ≤2s 间隔串行
# 连打且无 429 退避 → 每周期只有 ORDER BY id 最前的源成功,其余稳定 429
# 累积失败进 broken(5/6 broken)。因此 RSS 路径按响应头做窗口 pacing +
# 429 退避重试一次,总等待预算封顶(耗尽退化为原行为,保证周期有上界)。
# Reddit OAuth 是长期解,见 docs/ops 运维记录。
REDDIT_RSS_DEFAULT_BACKOFF_SEC = 61.0  # 实测窗口 ~60s;响应头缺失时的兜底
REDDIT_RSS_MAX_WAIT_SEC = 90.0         # 单次等待上限
REDDIT_RSS_WAIT_BUDGET_SEC = 360.0     # 每轮 fetch_reddit 的总等待预算


def _reddit_ratelimit_seconds(resp):
    """从 reddit 响应头推算下一次未认证请求前应等待的秒数（0 = 不用等）。"""
    headers = getattr(resp, 'headers', None) or {}

    def _num(key):
        try:
            value = float(headers.get(key, ''))
        except (TypeError, ValueError):
            return None
        return value if value >= 0 else None

    reset = _num('x-ratelimit-reset')
    if getattr(resp, 'status_code', None) == 429:
        wait = reset if reset is not None else _num('retry-after')
        if wait is None:
            wait = REDDIT_RSS_DEFAULT_BACKOFF_SEC
        return min(max(wait, 1.0), REDDIT_RSS_MAX_WAIT_SEC)
    remaining = _num('x-ratelimit-remaining')
    if remaining is not None and remaining <= 0 and reset is not None:
        return min(reset + 1.0, REDDIT_RSS_MAX_WAIT_SEC)
    return 0.0


class _RedditRssPacer:
    """RSS 兜底的限流步调器：窗口 pacing + 429 退避，共享总等待预算。"""

    def __init__(self, *, budget_sec=REDDIT_RSS_WAIT_BUDGET_SEC, sleep=None, monotonic=None):
        self._budget = float(budget_sec)
        self._sleep = sleep if sleep is not None else time.sleep
        self._monotonic = monotonic if monotonic is not None else time.monotonic
        self._next_allowed_at = 0.0

    def _pause(self, seconds):
        seconds = min(float(seconds), self._budget)
        if seconds <= 0:
            return False
        self._budget -= seconds
        self._sleep(seconds)
        return True

    def wait_for_slot(self):
        """RSS 请求前调用：等到上一个响应宣告的窗口重置。"""
        self._pause(self._next_allowed_at - self._monotonic())

    def observe(self, resp):
        """每个 RSS 响应后调用：记录下一请求的最早时间。"""
        self._next_allowed_at = self._monotonic() + _reddit_ratelimit_seconds(resp)

    def backoff_for_retry(self, resp):
        """429 后调用：按响应头退避。返回 True 表示已等待、值得重试一次。"""
        return self._pause(_reddit_ratelimit_seconds(resp))


def _reddit_posts_from_rss(content, sub, count):
    import feedparser

    feed = feedparser.parse(content)
    posts = []
    for entry in list(getattr(feed, 'entries', []) or [])[:count]:
        link = entry.get('link', '')
        entry_id = str(entry.get('id') or entry.get('guid') or '').strip()
        if entry_id.startswith('t3_'):
            entry_id = entry_id[3:]
        if not entry_id:
            parts = [part for part in urlparse(link).path.split('/') if part]
            entry_id = parts[3] if len(parts) > 3 and parts[2] == 'comments' else hashlib.sha1(link.encode()).hexdigest()[:16]
        summary = html.unescape(re.sub(r'<[^>]+>', ' ', entry.get('summary', '') or '')).strip()
        posts.append({
            'id': entry_id,
            'title': entry.get('title', ''),
            'selftext': summary,
            'author': entry.get('author', ''),
            'url': link,
            'permalink': urlparse(link).path if 'reddit.com' in urlparse(link).netloc else '',
            'score': 0,
            'upvote_ratio': 0,
            'num_comments': 0,
            'created_utc': 0,
            'thumbnail': '',
            'link_flair_text': '',
            'is_self': True,
            'subreddit': sub,
        })
    return posts


def fetch_reddit():
    """Fetch hot posts from configured subreddits."""
    import requests

    subs = _active_reddit_sources()
    count = CONFIG.get('reddit', {}).get('count', 25)
    if not subs:
        print("  ⚠️  No subreddits in config.json → reddit.subreddits")
        return

    out_dir = source_dir('reddit')
    os.makedirs(out_dir, exist_ok=True)
    headers = {'User-Agent': 'info2action/1.0'}
    # BF-0716-1: RSS 兜底共享的限流步调器(budget 在调用时读模块常量,便于测试覆盖)
    pacer = _RedditRssPacer(budget_sec=REDDIT_RSS_WAIT_BUDGET_SEC)

    for source in subs:
        source_id = source.get('source_id') if isinstance(source, dict) else None
        sub = source['subreddit'] if isinstance(source, dict) else source
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
                print(f"  ⚠️  r/{sub}: JSON HTTP {r.status_code}，降级 RSS")
                # BF-0716-1: 未认证 RSS 配额 = 每 ~60s 窗口 1 次(按 IP)。
                # 先等上一响应宣告的窗口重置,429 再按响应头退避重试一次。
                rss_url = f'https://www.reddit.com/r/{sub}/.rss'
                pacer.wait_for_slot()
                rss = requests.get(rss_url, headers=headers, timeout=15)
                pacer.observe(rss)
                if rss.status_code == 429 and pacer.backoff_for_retry(rss):
                    print(f"  ⏳ r/{sub}: RSS 429，等待限流窗口后重试")
                    rss = requests.get(rss_url, headers=headers, timeout=15)
                    pacer.observe(rss)
                if rss.status_code != 200:
                    error = f"JSON HTTP {r.status_code}; RSS HTTP {rss.status_code}"
                    print(f"  ❌ r/{sub}: {error}")
                    _record_source_fetch_result(source_id, False, error)
                    continue
                posts = _reddit_posts_from_rss(rss.content, sub, count)
            else:
                data = r.json()
                posts = []
                for child in data.get('data', {}).get('children', []):
                    post = child.get('data', {})
                    thumbnail = post.get('thumbnail', '')
                    if thumbnail in ('self', 'default', 'nsfw', 'spoiler', ''):
                        thumbnail = ''
                    # 2026-04-29: 优先取高清原图替代 140×80 缩略图
                    preview_imgs = (post.get('preview') or {}).get('images') or []
                    preview_src = ''
                    if preview_imgs:
                        src = preview_imgs[0].get('source') or {}
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
            _record_source_fetch_result(source_id, True)
            print(f"  ✅ r/{sub}: {len(posts)} posts")
            time.sleep(2)  # Reddit rate limit
        except Exception as e:
            _record_source_fetch_result(source_id, False, e)
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

    awesome_list = _active_github_awesome_repo_sources(tracking_cfg=tracking_cfg)
    if not awesome_list:
        logger.warning("github_tracking.json has empty awesome_repos list")
        return []

    # Token cap for README — keep generous tail (200k tokens per decision)
    max_tokens = int(tracking_cfg.get('readme_max_tokens', 12_000))  # PL-2(B6)

    out_dir = source_dir('github')
    os.makedirs(out_dir, exist_ok=True)

    items = []
    for source in awesome_list:
        source_id = source.get('source_id') if isinstance(source, dict) else None
        full_name = source['full_name'] if isinstance(source, dict) else source
        if '/' not in full_name:
            logger.warning("invalid awesome repo entry: %s", full_name)
            _record_source_fetch_result(source_id, False, "invalid github repo entry")
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
                _record_source_fetch_result(source_id, False, f"HTTP {r.status_code}")
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
            _record_source_fetch_result(source_id, True)
            time.sleep(1)
        except Exception as e:
            _record_source_fetch_result(source_id, False, e)
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
    print("  信息雷达 — RSS/公众号/HN/Reddit/GitHub 抓取")
    print("=" * 60)

    if run_all or '--rss' in flags:
        print("\n📡 RSS Feeds...")
        fetch_rss()

    if run_all or '--wechat' in flags:
        print("\n🐋 公众号 RSS...")
        fetch_wechat_rss()

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
