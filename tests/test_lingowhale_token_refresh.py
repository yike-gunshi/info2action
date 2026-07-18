import json
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "src"))

import fetch_lingowhale as lw  # noqa: E402


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


def _header(req, name):
    wanted = name.lower()
    for key, value in req.header_items():
        if key.lower() == wanted:
            return value
    return None


def test_post_json_refreshes_token_and_retries_once(monkeypatch, tmp_path):
    monkeypatch.setenv("INFO2ACTION_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("LINGOWHALE_ACCESS_TOKEN", "A1")
    monkeypatch.setenv("LINGOWHALE_AUTH_TOKEN", "B1")
    monkeypatch.setenv("LINGOWHALE_BID", "b1")
    monkeypatch.setenv("LINGOWHALE_UID", "u1")
    monkeypatch.setenv("LINGOWHALE_GUEST_ID", "g1")

    responses = [
        {"code": 10010, "msg": "token expired"},
        {
            "code": 0,
            "data": {
                "access_token": "A2",
                "auth_token": "B2",
                "b_id": "b2",
                "uid": "u2",
                "guest_id": "g2",
            },
        },
        {"code": 0, "data": {"feed_list": []}},
    ]
    requests = []

    def fake_urlopen(req, timeout=30, context=None):
        requests.append(req)
        return FakeResponse(responses.pop(0))

    monkeypatch.setattr(lw.urllib.request, "urlopen", fake_urlopen)

    result = lw._post_json(
        "/api/lingowhale/v1/feed/subscription",
        {"channel_ids": ["ch"], "sort_type": 0, "cursor": ""},
    )

    assert result == {"code": 0, "data": {"feed_list": []}}
    assert [req.full_url for req in requests] == [
        f"{lw.API_BASE}/api/lingowhale/v1/feed/subscription",
        f"{lw.PASSPORT_BASE}/api/user/refresh_token",
        f"{lw.API_BASE}/api/lingowhale/v1/feed/subscription",
    ]
    assert requests[1].data == b""
    assert _header(requests[0], "Access-Token") == "A1"
    assert _header(requests[2], "Access-Token") == "A2"

    stored = json.loads((tmp_path / "lingowhale_tokens.json").read_text())
    assert stored["access_token"] == "A2"
    assert stored["auth_token"] == "B2"
    assert stored["b_id"] == "b2"
    assert stored["uid"] == "u2"
    assert stored["guest_id"] == "g2"
    assert stored["refreshed_at"]

    headers = lw._current_headers()
    assert headers["Access-Token"] == "A2"
    assert headers["Auth-Token"] == "B2"
    assert headers["B-Id"] == "b2"


def test_post_json_returns_original_token_error_when_refresh_fails(monkeypatch, tmp_path):
    monkeypatch.setenv("INFO2ACTION_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("LINGOWHALE_ACCESS_TOKEN", "A1")
    monkeypatch.setenv("LINGOWHALE_AUTH_TOKEN", "B1")
    monkeypatch.setenv("LINGOWHALE_BID", "b1")

    responses = [
        {"code": 10010, "msg": "token expired"},
        {"code": 50001, "msg": "refresh failed"},
    ]
    requests = []

    def fake_urlopen(req, timeout=30, context=None):
        requests.append(req)
        return FakeResponse(responses.pop(0))

    monkeypatch.setattr(lw.urllib.request, "urlopen", fake_urlopen)

    result = lw._post_json("/api/lingowhale/v1/feed/subscription", {})

    assert result == {"code": 10010, "msg": "token expired"}
    assert [req.full_url for req in requests] == [
        f"{lw.API_BASE}/api/lingowhale/v1/feed/subscription",
        f"{lw.PASSPORT_BASE}/api/user/refresh_token",
    ]
    assert not (tmp_path / "lingowhale_tokens.json").exists()


def test_current_headers_prefer_token_store_over_env(monkeypatch, tmp_path):
    monkeypatch.setenv("INFO2ACTION_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("LINGOWHALE_ACCESS_TOKEN", "env-access")
    monkeypatch.setenv("LINGOWHALE_AUTH_TOKEN", "env-auth")
    monkeypatch.setenv("LINGOWHALE_BID", "env-bid")
    monkeypatch.setenv("LINGOWHALE_UID", "env-uid")
    monkeypatch.setenv("LINGOWHALE_GUEST_ID", "env-guest")
    (tmp_path / "lingowhale_tokens.json").write_text(
        json.dumps(
            {
                "access_token": "file-access",
                "auth_token": "file-auth",
                "b_id": "file-bid",
                "uid": "file-uid",
                "guest_id": "file-guest",
            }
        )
    )

    headers = lw._current_headers()

    assert headers["Access-Token"] == "file-access"
    assert headers["Auth-Token"] == "file-auth"
    assert headers["B-Id"] == "file-bid"
    assert headers["U-Id"] == "file-uid"
    assert headers["Guest-Id"] == "file-guest"
    assert lw.HEADERS.get("Access-Token") == "file-access"
    assert dict(lw.HEADERS)["Auth-Token"] == "file-auth"
