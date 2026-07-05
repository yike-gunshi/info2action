import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import remote_db  # noqa: E402


class FakeRemoteConn:
    def __init__(self):
        self.calls = []

    def execute(self, sql, params=None):
        self.calls.append((sql, params))
        return self


def test_submit_existing_remote_refresh_uses_update_not_insert():
    conn = FakeRemoteConn()

    remote_db.update_item_light_fields_remote(
        conn,
        "tw_2058356078157631966",
        {
            "title": "Soran shared an AI course",
            "content": "Long enough content",
            "author_name": "Soran",
            "cover_url": "/api/media/twitter-poster/2058356078157631966.jpg",
            "platform": "twitter",
            "source": "user-submit",
        },
    )

    assert len(conn.calls) == 1
    sql, params = conn.calls[0]
    assert sql.startswith(f"UPDATE {remote_db.remote_schema()}.items SET")
    assert "INSERT INTO" not in sql
    assert "platform" not in sql
    assert "source" not in sql
    assert params[-1] == "tw_2058356078157631966"


def test_submit_existing_remote_refresh_skips_empty_update():
    conn = FakeRemoteConn()

    remote_db.update_item_light_fields_remote(
        conn,
        "tw_empty",
        {
            "platform": "twitter",
            "source": "user-submit",
        },
    )

    assert conn.calls == []
