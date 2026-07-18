"""Lingowhale groups snapshot import into the sources registry."""
from contextlib import contextmanager
import json
import os
import sys
import tempfile

import pytest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "src"))
sys.path.insert(0, os.path.join(BASE, "scripts"))


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


def _write_groups(path):
    groups = [
        {
            "name": "每日查看",
            "channels": [
                {"channel_id": "lw-channel-1", "name": "一号公众号"},
                {"channel_id": "lw-channel-2", "name": "二号公众号"},
            ],
        },
        {
            "name": "AI",
            "channels": [
                {"channel_id": "lw-channel-2", "name": "二号公众号重复"},
                {"channel_id": "lw-channel-3", "name": "三号公众号"},
            ],
        },
    ]
    path.write_text(json.dumps(groups, ensure_ascii=False), encoding="utf-8")


def test_import_lingowhale_sources_upserts_unique_channels_idempotently(tmp_db, tmp_path):
    import db
    import import_lingowhale_sources

    groups_path = tmp_path / "groups.json"
    _write_groups(groups_path)
    conn = db.get_conn()

    first = import_lingowhale_sources.import_lingowhale_sources(
        conn=conn, groups_path=str(groups_path)
    )
    second = import_lingowhale_sources.import_lingowhale_sources(
        conn=conn, groups_path=str(groups_path)
    )

    assert first == {"inserted": 3, "updated": 0, "seen": 3}
    assert second == {"inserted": 0, "updated": 3, "seen": 3}
    rows = conn.execute(
        """SELECT source_key, display_name, status, config_json, origin
             FROM sources
            WHERE platform = 'wechat_mp'
            ORDER BY source_key"""
    ).fetchall()
    assert [row["source_key"] for row in rows] == [
        "lw-channel-1", "lw-channel-2", "lw-channel-3",
    ]
    assert [row["display_name"] for row in rows] == [
        "一号公众号", "二号公众号", "三号公众号",
    ]
    assert {row["status"] for row in rows} == {"active"}
    assert {json.loads(row["config_json"])["backend"] for row in rows} == {"lingowhale"}
    assert {row["origin"] for row in rows} == {"lingowhale_import"}
    conn.close()


def test_import_lingowhale_sources_preserves_existing_status(tmp_db, tmp_path):
    import db
    import import_lingowhale_sources

    groups_path = tmp_path / "groups.json"
    _write_groups(groups_path)
    conn = db.get_conn()
    conn.execute(
        """INSERT INTO sources(platform, source_key, display_name, status, origin)
           VALUES('wechat_mp', 'lw-channel-2', 'Admin Name', 'paused', 'admin_add')"""
    )
    conn.commit()

    result = import_lingowhale_sources.import_lingowhale_sources(
        conn=conn, groups_path=str(groups_path)
    )

    assert result == {"inserted": 2, "updated": 1, "seen": 3}
    row = conn.execute(
        """SELECT display_name, status, config_json, origin
             FROM sources
            WHERE platform = 'wechat_mp' AND source_key = 'lw-channel-2'"""
    ).fetchone()
    assert row["display_name"] == "二号公众号"
    assert row["status"] == "paused"
    assert json.loads(row["config_json"])["backend"] == "lingowhale"
    assert row["origin"] == "admin_add"
    conn.close()


def test_import_lingowhale_sources_dispatches_to_remote_without_opening_local(
    monkeypatch, tmp_path
):
    import db
    import import_lingowhale_sources
    import remote_db

    groups_path = tmp_path / "groups.json"
    _write_groups(groups_path)
    calls = []

    class FakeConn:
        def commit(self):
            pass

    @contextmanager
    def fake_connect():
        yield FakeConn()

    monkeypatch.setattr(remote_db, "fetch_write_to_remote", lambda: True)
    monkeypatch.setattr(remote_db, "connect", fake_connect)
    monkeypatch.setattr(
        remote_db,
        "upsert_source_registry_remote",
        lambda conn, **kwargs: calls.append(kwargs) or (
            "updated" if kwargs["source_key"] == "lw-channel-2" else "inserted"
        ),
        raising=False,
    )
    monkeypatch.setattr(db, "get_conn", lambda: pytest.fail("opened local SQLite"))

    result = import_lingowhale_sources.import_lingowhale_sources(
        groups_path=str(groups_path)
    )

    assert result == {"inserted": 2, "updated": 1, "seen": 3}
    assert [call["source_key"] for call in calls] == [
        "lw-channel-1", "lw-channel-2", "lw-channel-3",
    ]
    assert {call["platform"] for call in calls} == {"wechat_mp"}
    assert {call["status"] for call in calls} == {"active"}
    assert {call["origin"] for call in calls} == {"lingowhale_import"}
