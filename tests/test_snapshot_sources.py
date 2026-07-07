"""sources 每日快照脚本测试。"""
import json
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "src"))
sys.path.insert(0, os.path.join(BASE, "scripts"))

import db as db_mod  # noqa: E402
import snapshot_sources  # noqa: E402


@pytest.fixture
def snapshot_env(monkeypatch, tmp_path):
    monkeypatch.setattr(db_mod, "DB_PATH", str(tmp_path / "feed.db"))
    db_mod._item_status_has_user_id = None
    return tmp_path


def _insert_source(conn, platform, source_key, **overrides):
    cur = conn.execute(
        """INSERT INTO sources(platform, source_key, display_name, status, config_json, origin)
           VALUES(?,?,?,?,?,?)""",
        (
            platform,
            source_key,
            overrides.get("display_name", source_key),
            overrides.get("status", "active"),
            json.dumps(overrides["config_json"]) if "config_json" in overrides else None,
            overrides.get("origin", "test"),
        ),
    )
    conn.commit()
    return cur.lastrowid


def test_snapshot_sources_exports_today_file_with_all_sources(snapshot_env, capsys):
    conn = db_mod.get_conn()
    try:
        rss_id = _insert_source(
            conn,
            "rss",
            "https://example.com/feed.xml",
            display_name="Example Feed",
            config_json={"slug": "example"},
        )
        x_id = _insert_source(conn, "x_user", "alpha_ai", status="pending")
    finally:
        conn.close()

    result = snapshot_sources.snapshot_sources(base=str(snapshot_env))

    today = datetime.now(timezone.utc).strftime("%Y%m%d")
    path = snapshot_env / "data" / "backups" / f"sources-{today}.json"
    assert result == {"path": str(path), "rows": 2, "cleaned": 0}
    assert "exported 2 sources" in capsys.readouterr().out

    data = json.loads(path.read_text(encoding="utf-8"))
    assert [row["id"] for row in data] == [rss_id, x_id]
    assert data[0]["config_json"] == {"slug": "example"}
    assert data[1]["platform"] == "x_user"


def test_snapshot_sources_keeps_latest_30_daily_files(snapshot_env):
    backup_dir = snapshot_env / "data" / "backups"
    backup_dir.mkdir(parents=True)
    today = datetime.now(timezone.utc).date()
    dates = [(today - timedelta(days=i)).strftime("%Y%m%d") for i in range(31)]
    for stamp in dates:
        (backup_dir / f"sources-{stamp}.json").write_text("[]\n", encoding="utf-8")

    result = snapshot_sources.snapshot_sources(base=str(snapshot_env))

    files = sorted(path.name for path in backup_dir.glob("sources-*.json"))
    assert result["cleaned"] == 1
    assert len(files) == 30
    assert f"sources-{dates[-1]}.json" not in files
    assert f"sources-{dates[0]}.json" in files
