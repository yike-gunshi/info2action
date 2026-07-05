#!/usr/bin/env python3
"""fetch_url.py — Generic URL content fetcher for info2action.

fetch_url(url) → dict with keys: title, content, author, cover_url, published_at
Special handling for GitHub repos via API.
"""
import base64
import ipaddress
import json
import os
import re
import shutil
import socket
import ssl
import subprocess
import urllib.parse
import urllib.request
from html.parser import HTMLParser


_UA = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
_TIMEOUT = 15
_BLOCKED_FETCH_HOSTS = {'localhost', 'localhost.localdomain'}


def _gh_token():
    """Get GitHub token from gh CLI or env."""
    t = os.environ.get('GITHUB_TOKEN') or os.environ.get('GH_TOKEN', '')
    if t:
        return t
    gh = shutil.which('gh')
    if gh:
        try:
            r = subprocess.run([gh, 'auth', 'token'], capture_output=True, text=True, timeout=5)
            if r.returncode == 0 and r.stdout.strip():
                return r.stdout.strip()
        except Exception:
            pass
    return ''


def _ssl_ctx():
    return ssl.create_default_context()


def _is_blocked_fetch_ip(ip_text):
    try:
        ip = ipaddress.ip_address(ip_text)
    except ValueError:
        return False
    return any((
        ip.is_private,
        ip.is_loopback,
        ip.is_link_local,
        ip.is_multicast,
        ip.is_reserved,
        ip.is_unspecified,
    ))


def _is_blocked_fetch_target(hostname):
    host = (hostname or '').strip().lower().rstrip('.')
    if not host:
        return True
    if host in _BLOCKED_FETCH_HOSTS:
        return True
    if _is_blocked_fetch_ip(host):
        return True
    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except socket.gaierror:
        return False
    return any(_is_blocked_fetch_ip(info[4][0]) for info in infos)


def _assert_public_http_url(url):
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ('http', 'https') or not parsed.hostname:
        raise ValueError('blocked non-http URL')
    if _is_blocked_fetch_target(parsed.hostname):
        raise ValueError('blocked private network URL')


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _assert_public_http_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _open_url(req, timeout=_TIMEOUT):
    opener = urllib.request.build_opener(
        urllib.request.HTTPSHandler(context=_ssl_ctx()),
        _SafeRedirectHandler(),
    )
    return opener.open(req, timeout=timeout)


def _fetch_raw(url):
    """Fetch URL and return (bytes, content_type)."""
    _assert_public_http_url(url)
    req = urllib.request.Request(url, headers={'User-Agent': _UA})
    with _open_url(req, timeout=_TIMEOUT) as resp:
        data = resp.read()
        ct = resp.headers.get('Content-Type', '')
    return data, ct


def _get_json(url, auth_token=''):
    """Fetch URL and parse as JSON dict."""
    headers = {'User-Agent': _UA, 'Accept': 'application/vnd.github+json'}
    if auth_token:
        headers['Authorization'] = f'Bearer {auth_token}'
    _assert_public_http_url(url)
    req = urllib.request.Request(url, headers=headers)
    with _open_url(req, timeout=_TIMEOUT) as resp:
        return json.loads(resp.read())


# ── GitHub repo special handling ──

_GH_RE = re.compile(r'github\.com/([^/]+)/([^/?#]+)')


def _fetch_github(user, repo):
    """Use GitHub API to get repo info + README."""
    result = {'title': f'{user}/{repo}', 'content': '', 'author': user, 'cover_url': '', 'published_at': ''}
    token = _gh_token()
    try:
        info = _get_json(f'https://api.github.com/repos/{user}/{repo}', token)
        desc = info.get('description') or ''
        stars = info.get('stargazers_count', 0)
        lang = info.get('language') or ''
        created = info.get('created_at') or ''
        result['title'] = f'{user}/{repo}: {desc}' if desc else f'{user}/{repo}'
        result['published_at'] = created
        result['cover_url'] = info.get('owner', {}).get('avatar_url', '')
        parts = [f'Stars: {stars}']
        if lang:
            parts.append(f'Language: {lang}')
        if desc:
            parts.append(f'Description: {desc}')
        result['content'] = '\n'.join(parts)
    except Exception as e:
        print(f'[fetch_url] GitHub API error for {user}/{repo}: {e}')

    # Fetch README
    try:
        readme_info = _get_json(f'https://api.github.com/repos/{user}/{repo}/readme', token)
        readme_b64 = readme_info.get('content', '')
        if readme_b64:
            readme_text = base64.b64decode(readme_b64).decode('utf-8', errors='replace')
            result['content'] += '\n\n--- README ---\n' + readme_text[:5000]
    except Exception as e:
        print(f'[fetch_url] GitHub README error for {user}/{repo}: {e}')

    return result


# ── HTML parsing helpers ──

class _MetaParser(HTMLParser):
    """Extract meta tags, title, and body/article/main text from HTML."""

    def __init__(self):
        super().__init__()
        self.meta = {}  # property/name → content
        self.title = ''
        self._in_title = False
        self._title_parts = []
        self._in_article = False
        self._in_main = False
        self._in_body = False
        self._article_parts = []
        self._main_parts = []
        self._body_parts = []
        self._skip_tags = {'script', 'style', 'noscript', 'nav', 'header', 'footer', 'aside'}
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == 'meta':
            prop = a.get('property') or a.get('name', '')
            content = a.get('content', '')
            if prop and content:
                self.meta[prop.lower()] = content
        elif tag == 'title':
            self._in_title = True
            self._title_parts = []
        elif tag == 'article':
            self._in_article = True
        elif tag == 'main':
            self._in_main = True
        elif tag == 'body':
            self._in_body = True
        if tag in self._skip_tags:
            self._skip_depth += 1

    def handle_endtag(self, tag):
        if tag == 'title':
            self._in_title = False
            self.title = ''.join(self._title_parts).strip()
        elif tag == 'article':
            self._in_article = False
        elif tag == 'main':
            self._in_main = False
        elif tag == 'body':
            self._in_body = False
        if tag in self._skip_tags and self._skip_depth > 0:
            self._skip_depth -= 1

    def handle_data(self, data):
        if self._in_title:
            self._title_parts.append(data)
        if self._skip_depth > 0:
            return
        if self._in_article:
            self._article_parts.append(data)
        if self._in_main:
            self._main_parts.append(data)
        if self._in_body:
            self._body_parts.append(data)


def _is_error_page(title, content):
    """Detect common error pages that don't contain real content."""
    indicators = [
        'Something went wrong',
        'Try again',
        'Page not found',
        'Access denied',
        'Just a moment',  # Cloudflare challenge
        'Enable JavaScript',
        'Attention Required',  # Cloudflare
    ]
    check = (title + ' ' + content[:500]).lower()
    return any(ind.lower() in check for ind in indicators) and len(content) < 500


# ── WeChat article detection ──

_WX_RE = re.compile(r'mp\.weixin\.qq\.com/s[/?]')


def _is_wechat_verify_page(html_text):
    """Detect WeChat verification/anti-scraping page."""
    return 'secitptpage/verify' in html_text or ('weui-msg' in html_text and len(html_text) < 30000 and 'rich_media_content' not in html_text)


def _parse_html(html_text):
    """Parse HTML and return dict with title, content, author, cover_url, published_at."""
    parser = _MetaParser()
    try:
        parser.feed(html_text)
    except Exception:
        pass

    meta = parser.meta

    title = meta.get('og:title') or parser.title or ''

    # Content: prefer article, then main, then body (first 5000 chars)
    if parser._article_parts:
        content = ' '.join(parser._article_parts)
    elif parser._main_parts:
        content = ' '.join(parser._main_parts)
    else:
        content = ' '.join(parser._body_parts)
    # Clean whitespace
    content = re.sub(r'\s+', ' ', content).strip()[:5000]

    # Detect error pages (e.g. X.com "Something went wrong", Cloudflare challenges)
    if _is_error_page(title, content):
        return {'title': '', 'content': '', 'author': '', 'cover_url': '', 'published_at': ''}

    author = meta.get('og:author') or meta.get('article:author') or ''
    cover_url = meta.get('og:image') or ''
    published_at = meta.get('article:published_time') or meta.get('og:published_time') or ''

    return {
        'title': title,
        'content': content,
        'author': author,
        'cover_url': cover_url,
        'published_at': published_at,
    }


# ── Twitter/X special handling ──

_TW_RE = re.compile(r'(?:twitter\.com|x\.com)/\w+/status/(\d+)')


def _load_twitter_env():
    """Load TWITTER_AUTH_TOKEN and TWITTER_CT0 from cron_fetch.sh if not in env."""
    if os.environ.get('TWITTER_AUTH_TOKEN') and os.environ.get('TWITTER_CT0'):
        return {}  # already set
    cron_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'cron_fetch.sh')
    extra = {}
    if os.path.isfile(cron_file):
        with open(cron_file) as f:
            for line in f:
                line = line.strip()
                if line.startswith('export TWITTER_AUTH_TOKEN='):
                    extra['TWITTER_AUTH_TOKEN'] = line.split('=', 1)[1]
                elif line.startswith('export TWITTER_CT0='):
                    extra['TWITTER_CT0'] = line.split('=', 1)[1]
    return extra


def _fetch_twitter(tweet_id):
    """Use twitter-cli to fetch a single tweet."""
    result = {'title': '', 'content': '', 'author': '', 'cover_url': '', 'published_at': ''}
    try:
        # Find twitter-cli binary (may be in ~/.local/bin)
        twitter_bin = shutil.which('twitter')
        if not twitter_bin:
            home = os.path.expanduser('~')
            candidate = os.path.join(home, '.local', 'bin', 'twitter')
            if os.path.isfile(candidate):
                twitter_bin = candidate
        if not twitter_bin:
            print("[fetch_url] twitter-cli not found")
            return result
        # Clean env: __PYVENV_LAUNCHER__ from macOS Python breaks uv-managed venvs
        clean_env = dict(os.environ)
        clean_env.pop('__PYVENV_LAUNCHER__', None)
        # Load Twitter tokens from cron_fetch.sh if not in env
        clean_env.update(_load_twitter_env())
        proc = subprocess.run(
            [twitter_bin, 'tweet', str(tweet_id), '--json'],
            capture_output=True, text=True, timeout=30,
            env=clean_env
        )
        if proc.returncode != 0:
            # Check if it's an auth error
            stderr_out = proc.stderr or ''
            stdout_out = proc.stdout or ''
            if 'not_authenticated' in stderr_out or 'not_authenticated' in stdout_out:
                result['_error'] = 'auth_expired'
            return result
        data = json.loads(proc.stdout)
        # Check for auth error in response
        if isinstance(data, dict) and not data.get('ok', True):
            err = data.get('error', {})
            if isinstance(err, dict) and err.get('code') == 'not_authenticated':
                result['_error'] = 'auth_expired'
            return result
        # twitter-cli returns: {ok, schema_version, data: [tweet, ...replies]}
        tweets = data.get('data', []) if isinstance(data, dict) else []
        if not tweets:
            result['_error'] = 'tweet_not_found'
            return result
        tweet = tweets[0]

        text = tweet.get('text', '')
        author = tweet.get('author', {})
        metrics = tweet.get('metrics', {})
        media = tweet.get('media', [])

        author_name = author.get('name', '')
        screen_name = author.get('screenName', '')

        # X Articles have articleTitle/articleText
        article_title = tweet.get('articleTitle', '')
        article_text = tweet.get('articleText', '')

        if article_title:
            result['title'] = article_title
            result['content'] = article_text[:5000] if article_text else text
        else:
            result['title'] = text[:80] if text else ''
            result['content'] = text

        result['author'] = f"{author_name} (@{screen_name})" if screen_name else author_name
        # BF-0420-20: photo→取 url；video/animated_gif→后端 poster 懒抽帧；其他留空。
        # 不再 fallback 到 author.profileImageUrl（历史坑：作者头像污染 cover_url）
        if media and isinstance(media, list) and isinstance(media[0], dict):
            mt = media[0].get('type')
            if mt == 'photo':
                result['cover_url'] = media[0].get('url') or ''
            elif mt in ('video', 'animated_gif'):
                result['cover_url'] = f'/api/media/twitter-poster/{tweet_id}.jpg'
            else:
                result['cover_url'] = ''
        else:
            result['cover_url'] = ''
        # BF-0420-1: 保留完整 media 数组让 submit.py 存 media_json,触发 Twitter 视频 ASR hook
        result['media'] = media if isinstance(media, list) else []
        result['published_at'] = tweet.get('createdAtLocal', tweet.get('createdAt', ''))

        # Append metrics to content for AI summary context
        metric_parts = []
        if metrics.get('likes'):
            metric_parts.append(f"Likes: {metrics['likes']}")
        if metrics.get('retweets'):
            metric_parts.append(f"Retweets: {metrics['retweets']}")
        if metrics.get('views'):
            metric_parts.append(f"Views: {metrics['views']}")
        if metric_parts:
            result['content'] += '\n\n--- Metrics ---\n' + ', '.join(metric_parts)

        # Include quoted tweet if present
        qt = tweet.get('quotedTweet')
        if qt and isinstance(qt, dict):
            qt_text = qt.get('text', '')
            qt_author = qt.get('author', {})
            if qt_text:
                result['content'] += f"\n\n--- Quoted Tweet (@{qt_author.get('screenName', '')}) ---\n{qt_text}"

    except Exception as e:
        print(f"[fetch_url] Twitter fetch error: {e}")
    return result


# ── Main entry point ──

def fetch_url(url):
    """Fetch URL content and return structured dict.

    Returns: {title, content, author, cover_url, published_at}
    On error: {title: url, content: '', author: '', cover_url: '', published_at: ''}
    """
    empty = {'title': url, 'content': '', 'author': '', 'cover_url': '', 'published_at': ''}

    # Twitter/X special case
    tw = _TW_RE.search(url)
    if tw:
        try:
            result = _fetch_twitter(tw.group(1))
            if result.get('content'):
                return result
            # Preserve specific error info (auth_expired, tweet_not_found)
            if result.get('_error'):
                empty['_error'] = result['_error']
                return empty
        except Exception:
            pass  # Fall through to generic fetch

    # GitHub repo special case
    m = _GH_RE.search(url)
    if m:
        user, repo = m.group(1), m.group(2)
        # Strip .git suffix
        repo = repo.rstrip('.git') if repo.endswith('.git') else repo
        # Only use API for repo root URLs (not file paths, issues, etc.)
        path_after = url.split(f'{user}/{repo}', 1)[-1].strip('/')
        if not path_after or path_after in ('', 'tree', 'blob'):
            try:
                return _fetch_github(user, repo)
            except Exception:
                pass  # Fall through to generic fetch

    try:
        data, ct = _fetch_raw(url)
        charset = 'utf-8'
        if 'charset=' in ct:
            charset = ct.split('charset=')[-1].split(';')[0].strip()
        html_text = data.decode(charset, errors='replace')

        # Detect WeChat verification page
        if _WX_RE.search(url) and _is_wechat_verify_page(html_text):
            print(f'[fetch_url] WeChat verify page detected for {url}')
            empty['_error'] = 'wechat_verify'
            return empty

        return _parse_html(html_text)
    except Exception as e:
        print(f"[fetch_url] Error fetching {url}: {e}")
        return empty


if __name__ == '__main__':
    import sys
    if len(sys.argv) > 1:
        result = fetch_url(sys.argv[1])
        for k, v in result.items():
            print(f'{k}: {v[:200] if isinstance(v, str) else v}')
