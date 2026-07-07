"""Lingowhale ingest attribution through the sources registry."""
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


def _entry(entry_id, channel_id, channel_name):
    return {
        "entry_id": entry_id,
        "title": f"标题 {entry_id}",
        "content": "正文",
        "description": "摘要",
        "pub_time": 1783296000,
        "channel": {"channel_id": channel_id, "name": channel_name},
        "info_source": {"info_source_name": channel_name},
    }


def test_ingest_lingowhale_maps_channel_id_and_drops_paused_sources(
    tmp_db, tmp_path, monkeypatch
):
    import db
    import ingest

    data_dir = tmp_path / "data"
    lw_dir = data_dir / "lingowhale"
    lw_dir.mkdir(parents=True)
    monkeypatch.setenv("INFO2ACTION_DATA_DIR", str(data_dir))
    monkeypatch.setattr(ingest.remote_db, "fetch_write_to_remote", lambda: False)
    ingest._source_index_cache = None
    ingest._source_index_loaded = False

    (lw_dir / "feed.json").write_text(json.dumps([
        _entry("active-entry", "lw-active", "Active 公众号"),
        _entry("paused-entry", "lw-paused", "Paused 公众号"),
    ], ensure_ascii=False), encoding="utf-8")

    conn = db.get_conn()
    active_id = conn.execute(
        """INSERT INTO sources(platform, source_key, display_name, status, config_json, origin)
           VALUES('wechat_mp', 'lw-active', 'Active 公众号', 'active',
                  '{"backend":"lingowhale"}', 'test')"""
    ).lastrowid
    conn.execute(
        """INSERT INTO sources(platform, source_key, display_name, status, config_json, origin)
           VALUES('wechat_mp', 'lw-paused', 'Paused 公众号', 'paused',
                  '{"backend":"lingowhale"}', 'test')"""
    )
    conn.commit()

    count = ingest.ingest_lingowhale(conn)

    assert count == 2
    rows = conn.execute(
        "SELECT id, source, source_id, author_name FROM items ORDER BY id"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["id"] == "lw_active-entry"
    assert rows[0]["source"] == "lingowhale:lw-active"
    assert rows[0]["source_id"] == active_id
    assert rows[0]["author_name"] == "Active 公众号"
    conn.close()
