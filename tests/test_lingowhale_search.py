import os
import sys

import pytest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "src"))

import fetch_lingowhale as lw  # noqa: E402


def test_search_channels_normalizes_fields_and_applies_limit(monkeypatch):
    calls = []

    def fake_post_json(path, payload):
        calls.append((path, payload))
        return {
            "code": 0,
            "data": {
                "channels": [
                    {
                        "channel_id": "ch-1",
                        "name": "一号",
                        "description": "desc",
                        "surface_url": "https://img.test/a.png",
                        "has_subscribed": True,
                        "last_7_article_count": "3",
                        "subscribe_user_count": "42",
                        "is_official": 1,
                    },
                    {
                        "channel_id": "ch-2",
                        "name": "二号",
                        "last_7_article_count": None,
                        "subscribe_user_count": None,
                    },
                ]
            },
        }

    monkeypatch.setattr(lw, "_post_json", fake_post_json)

    assert lw.search_channels("赛博", limit=1) == [
        {
            "channel_id": "ch-1",
            "name": "一号",
            "description": "desc",
            "avatar_url": "https://img.test/a.png",
            "has_subscribed": True,
            "last_7d_count": 3,
            "subscriber_count": 42,
            "is_official": True,
        }
    ]
    assert calls == [
        (
            "/api/lingowhale/v1/search",
            {"query_type": 1, "query": "赛博", "cursor": ""},
        )
    ]


def test_search_channels_empty_query_returns_empty_without_request(monkeypatch):
    monkeypatch.setattr(lw, "_post_json", lambda path, payload: pytest.fail("request should not be sent"))

    assert lw.search_channels("   ") == []


def test_search_channels_raises_on_nonzero_code(monkeypatch):
    monkeypatch.setattr(
        lw,
        "_post_json",
        lambda path, payload: {"code": 50001, "msg": "upstream failed"},
    )

    with pytest.raises(RuntimeError, match="lingowhale search failed: code=50001 msg=upstream failed"):
        lw.search_channels("赛博")


def test_search_channels_token_error_mentions_refresh(monkeypatch):
    monkeypatch.setattr(
        lw,
        "_post_json",
        lambda path, payload: {"code": 10010, "msg": "token expired"},
    )

    with pytest.raises(RuntimeError) as exc:
        lw.search_channels("赛博")

    assert "token 失效" in str(exc.value)
    assert "LINGOWHALE_" in str(exc.value)
