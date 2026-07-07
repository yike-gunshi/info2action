"""公众号 RSS ingest: 本地 JSON 产物入库,无网络。"""
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


def test_ingest_wechat_rss_writes_lingowhale_item_with_wechat_source(tmp_db, tmp_path, monkeypatch):
    import db
    import ingest

    data_dir = tmp_path / "data"
    wechat_dir = data_dir / "sources" / "wechat"
    wechat_dir.mkdir(parents=True)
    monkeypatch.setenv("INFO2ACTION_DATA_DIR", str(data_dir))
    monkeypatch.setattr(ingest.remote_db, "fetch_write_to_remote", lambda: False)
    ingest._source_index_cache = None
    ingest._source_index_loaded = False

    feed_url = "https://wechat.example.com/feed.xml"
    (wechat_dir / "demo.json").write_text(json.dumps({
        "feed_title": "Feed Title",
        "feed_url": feed_url,
        "source_name": "示例公众号",
        "items": [{
            "id": "entry-1",
            "title": "第一篇",
            "link": "https://mp.weixin.qq.com/s/abc",
            "summary": "<p>摘要</p>",
            "content": "<p>正文</p>",
            "author": "Feed Author",
            "published": "2026-07-05T10:00:00Z",
            "tags": ["AI"],
        }],
    }, ensure_ascii=False), encoding="utf-8")

    conn = db.get_conn()
    source_id = conn.execute(
        """INSERT INTO sources(platform, source_key, display_name, status, origin)
           VALUES('wechat_mp', ?, '示例公众号', 'active', 'test')""",
        (feed_url,),
    ).lastrowid
    conn.commit()

    count = ingest.ingest_wechat_rss(conn)

    assert count == 1
    row = conn.execute(
        """SELECT platform, source, source_id, title, content, author_name, url,
                  tags_json, published_at
             FROM items
            WHERE platform = 'lingowhale'"""
    ).fetchone()
    assert row["platform"] == "lingowhale"
    assert row["source"] == f"wechat:{feed_url}"
    assert row["source_id"] == source_id
    assert row["title"] == "第一篇"
    assert row["content"] == "正文"
    assert row["author_name"] == "示例公众号"
    assert row["url"] == "https://mp.weixin.qq.com/s/abc"
    assert json.loads(row["tags_json"]) == ["AI"]
    assert row["published_at"] == "2026-07-05T10:00:00Z"
    conn.close()
