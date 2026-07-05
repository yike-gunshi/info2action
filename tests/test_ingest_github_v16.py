"""W2.T4 — ingest GitHub trending + awesome → detail_json.readme 单测

覆盖 PRD §4.9.3：
- detail_json.readme 字段必须入库
- detail_json.readme_error 失败原因可追溯
- source 区分: trending:{spoken_language} vs awesome:{full_name}
- trending + awesome 同 repo 去重（trending 优先）

铁律：所有调用 mock，DB 用 tempfile，不污染主仓库 data/feed.db。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile

import pytest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "src"))


@pytest.fixture
def tmp_db(monkeypatch):
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    monkeypatch.setattr("db.DB_PATH", tmp.name)
    import db as _db
    _db._item_status_has_user_id = None
    yield tmp.name
    try:
        os.unlink(tmp.name)
    except OSError:
        pass


@pytest.fixture
def tmp_sources(tmp_path, monkeypatch):
    """Set ingest BASE to a tmp dir, return data/sources/github subpath."""
    import ingest as _ingest
    monkeypatch.setattr(_ingest, "BASE", str(tmp_path))
    gh_dir = tmp_path / "data" / "sources" / "github"
    gh_dir.mkdir(parents=True)
    return gh_dir


# ============================================================
# Test 1: trending.json with readme → detail_json.readme 入库
# ============================================================
def test_ingest_github_trending_writes_readme(tmp_db, tmp_sources):
    import db
    import ingest

    trending_repos = [
        {
            "full_name": "owner1/repo-zh",
            "description": "中文项目",
            "language": "Python",
            "stars": 1000,
            "forks": 50,
            "stars_today": 25,
            "url": "https://github.com/owner1/repo-zh",
            "spoken_language": "zh",
            "since": "daily",
            "readme": "# 完整 README 内容（中文）",
            "readme_error": None,
        },
    ]
    (tmp_sources / "trending.json").write_text(
        json.dumps(trending_repos, ensure_ascii=False)
    )

    conn = db.get_conn()
    n = ingest.ingest_github_trending(conn)
    assert n == 1

    row = conn.execute(
        "SELECT platform, source, detail_json FROM items WHERE id=?",
        ("gh_owner1_repo-zh",),
    ).fetchone()
    assert row is not None
    assert row["platform"] == "github"
    assert row["source"] == "trending:zh"
    detail = json.loads(row["detail_json"])
    assert detail["readme"] == "# 完整 README 内容（中文）"
    assert detail["readme_error"] is None
    assert detail["source_type"] == "trending"
    assert detail["spoken_language"] == "zh"
    conn.close()


# ============================================================
# Test 2: awesome.json → source=awesome:{full_name} + readme 入库
# ============================================================
def test_ingest_github_awesome_writes_source_and_readme(tmp_db, tmp_sources):
    import db
    import ingest

    awesome_repos = [
        {
            "full_name": "modelcontextprotocol/registry",
            "description": "MCP registry",
            "language": "TypeScript",
            "stars": 5000,
            "forks": 200,
            "stars_today": 0,
            "url": "https://github.com/modelcontextprotocol/registry",
            "pushed_at": "2026-05-01T00:00:00Z",
            "source_type": "awesome",
            "readme": "# Awesome MCP Registry — list of servers...",
            "readme_error": None,
        },
    ]
    (tmp_sources / "awesome.json").write_text(
        json.dumps(awesome_repos, ensure_ascii=False)
    )

    conn = db.get_conn()
    n = ingest.ingest_github_trending(conn)  # 同函数同时处理 awesome
    assert n == 1

    row = conn.execute(
        "SELECT source, detail_json FROM items WHERE id=?",
        ("gh_modelcontextprotocol_registry",),
    ).fetchone()
    assert row is not None
    assert row["source"] == "awesome:modelcontextprotocol/registry"
    detail = json.loads(row["detail_json"])
    assert detail["source_type"] == "awesome"
    assert "Awesome MCP Registry" in detail["readme"]
    assert detail["pushed_at"] == "2026-05-01T00:00:00Z"
    conn.close()


# ============================================================
# Test 3: readme fetch 失败 → readme='' + readme_error 字符串入库
# ============================================================
def test_ingest_github_readme_failure_records_error(tmp_db, tmp_sources):
    import db
    import ingest

    repos = [
        {
            "full_name": "owner/no-readme",
            "description": "no readme repo",
            "language": "Go",
            "stars": 10,
            "forks": 1,
            "stars_today": 0,
            "url": "https://github.com/owner/no-readme",
            "spoken_language": "global",
            "since": "daily",
            "readme": "",  # fetch 失败时 fetch_feeds 写空字符串
            "readme_error": "main 404 + master 404",
        },
    ]
    (tmp_sources / "trending.json").write_text(
        json.dumps(repos, ensure_ascii=False)
    )

    conn = db.get_conn()
    ingest.ingest_github_trending(conn)
    row = conn.execute(
        "SELECT detail_json FROM items WHERE id=?", ("gh_owner_no-readme",)
    ).fetchone()
    detail = json.loads(row["detail_json"])
    assert detail["readme"] == ""
    assert detail["readme_error"] == "main 404 + master 404"
    conn.close()


# ============================================================
# Test 4: trending + awesome 同 repo → 只入库一次（trending 优先）
# ============================================================
def test_ingest_github_dedup_trending_first(tmp_db, tmp_sources):
    import db
    import ingest

    same_repo_id = "owner-x/dual"
    (tmp_sources / "trending.json").write_text(json.dumps([{
        "full_name": same_repo_id,
        "description": "from trending",
        "language": "Rust",
        "stars": 100,
        "forks": 5,
        "stars_today": 10,
        "url": f"https://github.com/{same_repo_id}",
        "spoken_language": "global",
        "since": "daily",
        "readme": "trending readme",
        "readme_error": None,
    }], ensure_ascii=False))
    (tmp_sources / "awesome.json").write_text(json.dumps([{
        "full_name": same_repo_id,
        "description": "from awesome",
        "language": "Rust",
        "stars": 100,
        "forks": 5,
        "stars_today": 0,
        "url": f"https://github.com/{same_repo_id}",
        "pushed_at": "2026-05-01T00:00:00Z",
        "source_type": "awesome",
        "readme": "awesome readme",
        "readme_error": None,
    }], ensure_ascii=False))

    conn = db.get_conn()
    n = ingest.ingest_github_trending(conn)
    assert n == 1, f"expected 1 unique repo (dedup), got {n}"

    row = conn.execute(
        "SELECT source, detail_json FROM items WHERE id=?",
        (f"gh_{same_repo_id.replace('/', '_')}",),
    ).fetchone()
    # 跨源去重：trending 先处理，应保留 trending 版本
    assert row["source"] == "trending:global"
    detail = json.loads(row["detail_json"])
    assert detail["readme"] == "trending readme"
    conn.close()


# ============================================================
# Test 5: 缺 trending.json/awesome.json → 不报错，return 0
# ============================================================
def test_ingest_github_no_files_returns_zero(tmp_db, tmp_sources):
    import db
    import ingest

    # tmp_sources 目录已建但无任何 JSON
    conn = db.get_conn()
    n = ingest.ingest_github_trending(conn)
    assert n == 0
    conn.close()
