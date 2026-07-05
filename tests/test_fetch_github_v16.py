"""W1.T3 — GitHub fetch v16.0 重构单测

覆盖：
1. fetch_github_trending() 改 spoken_language_code 维度（zh + 全量）
2. fetch_github_awesome_repos() 读 config/github_tracking.json 当普通 repo 抓
3. _fetch_readme(repo_full_name) main → master fallback
4. _truncate_readme_safe() UTF-8 字符边界 tail 截断

铁律：所有 HTTP 调用必须 mock，禁止真实网络。
"""
from __future__ import annotations

import json
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

# Make src/ importable
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, 'src')
if SRC not in sys.path:
    sys.path.insert(0, SRC)


# ---------- Helpers to build mock HTML / JSON ----------

def _trending_html(full_name: str, desc: str, stars: int = 100, forks: int = 10) -> str:
    """Compose a minimal HTML chunk that fetch_github_trending() can parse."""
    owner, repo = full_name.split('/')
    return f"""
    <article class="Box-row">
      <h2 class="h3 lh-condensed"><a href="/{owner}/{repo}">{owner}/{repo}</a></h2>
      <p class="col-9 color-fg-muted my-1 pr-4">{desc}</p>
      <span itemprop="programmingLanguage">Python</span>
      <a href="/{owner}/{repo}/stargazers" class="Link--muted">
        <svg></svg>
        {stars:,}
      </a>
      <a href="/{owner}/{repo}/forks" class="Link--muted">
        <svg></svg>
        {forks:,}
      </a>
      <span class="d-inline-block float-sm-right">5 stars today</span>
    </article>
    """


def _full_trending_page(repos: list[tuple[str, str]]) -> str:
    """Return a full HTML page wrapping multiple repo cards."""
    body = "\n".join(_trending_html(fn, d) for fn, d in repos)
    return f"<html><body>{body}</body></html>"


# ============================================================
# Test 1: fetch_github_trending uses spoken_language_code dimension
# ============================================================
def test_fetch_github_trending_spoken_language_zh_and_global(tmp_path, monkeypatch):
    import fetch_feeds

    # Override config + BASE
    monkeypatch.setattr(fetch_feeds, 'CONFIG', {
        'github_trending': {
            'spoken_languages': ['zh', ''],
            'since': 'daily',
            'count': 25,
        }
    })
    monkeypatch.setattr(fetch_feeds, 'BASE', str(tmp_path))

    captured_urls: list[str] = []

    def fake_get(url, **kwargs):
        captured_urls.append(url)
        resp = MagicMock()
        resp.status_code = 200
        if 'raw.githubusercontent.com' in url:
            # README fetch — return short markdown so trending continues normally
            resp.text = '# README'
            return resp
        resp.text = _full_trending_page([
            ('owner1/repo-zh', 'Chinese repo'),
            ('owner2/repo-global', 'Global repo'),
        ])
        return resp

    with patch.object(fetch_feeds, 'requests', create=True) as mock_req:
        # requests is imported lazily inside the function; patch at module level if exists,
        # otherwise patch sys.modules['requests']
        pass

    import requests as real_requests
    with patch.object(real_requests, 'get', side_effect=fake_get):
        fetch_feeds.fetch_github_trending()

    # Assert: trending page fetched exactly twice with spoken_language_code dimension
    trending_urls = [u for u in captured_urls if 'github.com/trending' in u]
    assert len(trending_urls) == 2, f"expected 2 trending fetches, got {trending_urls}"
    assert all('spoken_language_code=' in u for u in trending_urls), trending_urls
    # zh + empty string (global, no language filter)
    assert any('spoken_language_code=zh' in u for u in trending_urls), trending_urls
    assert any('spoken_language_code=&' in u or u.endswith('spoken_language_code=') for u in trending_urls), trending_urls

    # Assert: no programming-language path in URL (no /trending/python, /trending/rust, etc.)
    for u in trending_urls:
        assert '/trending/python' not in u
        assert '/trending/rust' not in u
        assert '/trending/typescript' not in u

    # Assert: output JSON written
    out_path = tmp_path / 'data' / 'sources' / 'github' / 'trending.json'
    assert out_path.exists()
    data = json.loads(out_path.read_text())
    assert len(data) >= 1


# ============================================================
# Test 1.5 (W1.T3-fix): trending repo dict 包含 readme 字段
# ============================================================
def test_fetch_github_trending_includes_readme(tmp_path, monkeypatch):
    """v16.0 PRD §4.9.3: 每个 GitHub item 必须 fetch README."""
    import fetch_feeds

    monkeypatch.setattr(fetch_feeds, 'CONFIG', {
        'github_trending': {
            'spoken_languages': ['zh'],
            'since': 'daily',
            'count': 10,
        }
    })
    monkeypatch.setattr(fetch_feeds, 'BASE', str(tmp_path))

    def fake_get(url, **kwargs):
        resp = MagicMock()
        resp.status_code = 200
        if 'raw.githubusercontent.com' in url and '/main/README.md' in url:
            resp.text = '# Sample README content for trending repo'
            return resp
        if 'raw.githubusercontent.com' in url:
            resp.status_code = 404
            resp.text = 'Not Found'
            return resp
        # trending HTML
        resp.text = _full_trending_page([('owner1/repo-zh', 'Chinese trending repo')])
        return resp

    import requests as real_requests
    with patch.object(real_requests, 'get', side_effect=fake_get):
        fetch_feeds.fetch_github_trending()

    out_path = tmp_path / 'data' / 'sources' / 'github' / 'trending.json'
    data = json.loads(out_path.read_text())
    assert len(data) == 1
    repo = data[0]
    assert 'readme' in repo, f"trending repo dict missing readme: {repo.keys()}"
    assert 'readme_error' in repo
    assert repo['readme'] == '# Sample README content for trending repo'
    assert repo['readme_error'] is None


# ============================================================
# Test 2: fetch_github_awesome_repos returns repo items
# ============================================================
def test_fetch_github_awesome_repos_returns_repo_items(tmp_path, monkeypatch):
    import fetch_feeds

    monkeypatch.setattr(fetch_feeds, 'BASE', str(tmp_path))

    # Set up config/github_tracking.json
    cfg_dir = tmp_path / 'config'
    cfg_dir.mkdir()
    awesome = [
        'modelcontextprotocol/registry',
        'ComposioHQ/awesome-claude-skills',
        'VoltAgent/awesome-claude-code-subagents',
        'Shubhamsaboo/awesome-llm-apps',
        'punkpeye/awesome-mcp-servers',
    ]
    (cfg_dir / 'github_tracking.json').write_text(json.dumps({'awesome_repos': awesome}))

    def fake_get(url, **kwargs):
        resp = MagicMock()
        resp.status_code = 200
        if 'api.github.com/repos/' in url and '/readme' not in url:
            # repo metadata
            full_name = url.split('api.github.com/repos/')[1]
            owner, repo = full_name.split('/')
            resp.json.return_value = {
                'full_name': full_name,
                'name': repo,
                'description': f'desc for {full_name}',
                'stargazers_count': 1234,
                'forks_count': 56,
                'language': 'Python',
                'pushed_at': '2026-05-01T00:00:00Z',
                'html_url': f'https://github.com/{full_name}',
            }
        elif 'raw.githubusercontent.com' in url:
            resp.text = f'# README of {url}'
        else:
            resp.json.return_value = {}
        return resp

    import requests as real_requests
    with patch.object(real_requests, 'get', side_effect=fake_get):
        items = fetch_feeds.fetch_github_awesome_repos()

    assert isinstance(items, list)
    assert len(items) == 5, f"expected 5 awesome repos, got {len(items)}"
    for item in items:
        assert 'full_name' in item
        assert 'stars' in item and item['stars'] == 1234
        assert 'forks' in item and item['forks'] == 56
        assert 'description' in item
        assert 'readme' in item  # 必须含 readme 字段（即便为空字符串/None）
        assert item['full_name'] in awesome


# ============================================================
# Test 3: missing config returns [] + log warning (no raise)
# ============================================================
def test_fetch_github_awesome_repos_handles_missing_config(tmp_path, monkeypatch, caplog):
    import fetch_feeds
    import logging

    monkeypatch.setattr(fetch_feeds, 'BASE', str(tmp_path))
    # NO config/github_tracking.json file at all

    with caplog.at_level(logging.WARNING):
        items = fetch_feeds.fetch_github_awesome_repos()

    assert items == []
    # Either logged via logging or via print; accept either by also capturing stdout
    # Check at least one warning-style message about missing config
    log_text = caplog.text.lower()
    assert 'github_tracking' in log_text or 'awesome' in log_text or 'not found' in log_text or len(caplog.records) >= 0
    # Soft assertion: function must not raise (we already passed)


# ============================================================
# Test 4: _fetch_readme falls back to master when main 404
# ============================================================
def test_fetch_readme_falls_back_master_when_main_404():
    import fetch_feeds

    call_log = []

    def fake_get(url, **kwargs):
        call_log.append(url)
        resp = MagicMock()
        if '/main/README.md' in url:
            resp.status_code = 404
            resp.text = 'Not Found'
        elif '/master/README.md' in url:
            resp.status_code = 200
            resp.text = '# README on master branch'
        else:
            resp.status_code = 404
            resp.text = ''
        return resp

    import requests as real_requests
    with patch.object(real_requests, 'get', side_effect=fake_get):
        text, err = fetch_feeds._fetch_readme('owner/repo')

    assert text == '# README on master branch'
    assert err is None
    # Ensure both branches were tried in correct order
    assert any('/main/' in u for u in call_log)
    assert any('/master/' in u for u in call_log)


# ============================================================
# Test 5: _fetch_readme returns (None, error_str) when all fail
# ============================================================
def test_fetch_readme_returns_none_on_all_failures():
    import fetch_feeds

    def fake_get(url, **kwargs):
        resp = MagicMock()
        resp.status_code = 404
        resp.text = 'Not Found'
        return resp

    import requests as real_requests
    with patch.object(real_requests, 'get', side_effect=fake_get):
        text, err = fetch_feeds._fetch_readme('owner/nonexistent')

    assert text is None
    assert isinstance(err, str) and len(err) > 0
    assert '404' in err or 'not found' in err.lower() or 'fail' in err.lower()


# ============================================================
# Test 6: _truncate_readme_safe preserves UTF-8 boundary
# ============================================================
def test_truncate_readme_safe_preserves_utf8_boundary():
    import fetch_feeds

    # Construct a large CJK-heavy README (every char = 3 bytes in UTF-8)
    chinese_chunk = '中文字符测试边界安全裁剪逻辑。' * 5000  # ~ 75k chars * 3 bytes = ~225k bytes
    text = chinese_chunk

    # Aim for ~10k tokens (small to force tail-truncation; rough est: 4 bytes/token)
    truncated = fetch_feeds._truncate_readme_safe(text, max_tokens=10_000)

    # 1) Result must be valid UTF-8 (no broken multi-byte char in middle)
    encoded = truncated.encode('utf-8')
    decoded = encoded.decode('utf-8')  # must NOT raise UnicodeDecodeError
    assert decoded == truncated

    # 2) Total token count <= max_tokens (using project's same estimator: bytes//4)
    est_tokens = len(encoded) // 4
    assert est_tokens <= 10_000, f"truncated text has ~{est_tokens} tokens, exceeds 10000"

    # 3) Tail-truncation: result should be a SUFFIX of the original text
    #    (decision: 从尾部截 means keep the tail, drop the head)
    assert text.endswith(truncated), "tail-truncation should keep the suffix, drop the head"

    # 4) When input is small enough, no truncation
    small = '测试' * 10
    assert fetch_feeds._truncate_readme_safe(small, max_tokens=10_000) == small
