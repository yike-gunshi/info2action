import pytest
from fastapi import HTTPException


def test_image_proxy_rejects_private_hosts(monkeypatch):
    import routes.media as media

    monkeypatch.setattr(
        media.socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [(None, None, None, None, ("127.0.0.1", 443))],
    )

    with pytest.raises(HTTPException) as exc:
        media._validate_public_image_url("https://example.com/a.jpg")

    assert exc.value.status_code == 400


def test_fetch_external_image_accepts_image_response(monkeypatch):
    import routes.media as media

    media._twitter_image_lru.clear()
    monkeypatch.setattr(
        media.socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [(None, None, None, None, ("93.184.216.34", 443))],
    )

    class FakeResponse:
        headers = {"Content-Type": "image/webp"}

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self, _size):
            if getattr(self, "_done", False):
                return b""
            self._done = True
            return b"image-bytes"

    monkeypatch.setattr(media._NO_REDIRECT_OPENER, "open", lambda *_args, **_kwargs: FakeResponse())

    data, content_type = media._fetch_external_image("https://example.com/a.webp")

    assert data == b"image-bytes"
    assert content_type == "image/webp"
