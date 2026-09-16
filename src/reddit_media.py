"""Reddit media normalization shared by fetch and offline backfill paths."""

from __future__ import annotations

import html
import ipaddress
import logging
from collections.abc import Callable
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urlparse
from urllib.request import Request, urlopen


logger = logging.getLogger(__name__)


def _clean_url(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return html.unescape(value).strip()


def _web_url(value: Any) -> str:
    url = _clean_url(value)
    parsed = urlparse(url)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        return ""
    host = parsed.hostname.lower().rstrip(".")
    if host == "localhost" or host.endswith((".localhost", ".local")):
        return ""
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        if not address.is_global:
            return ""
    return url


def _reddit_url(value: Any) -> str:
    url = _web_url(value)
    host = (urlparse(url).hostname or "").lower()
    if host in {"reddit.com", "redd.it"} or host.endswith((".reddit.com", ".redd.it")):
        return url
    return ""


def _source_url(post: dict[str, Any]) -> str:
    permalink = _clean_url(post.get("permalink"))
    if permalink.startswith("/"):
        return f"https://www.reddit.com{permalink}"
    if permalink.startswith(("http://", "https://")):
        return _web_url(permalink)
    return _web_url(post.get("url"))


def _poster_url(post: dict[str, Any]) -> str:
    images = ((post.get("preview") or {}).get("images") or [])
    if images:
        source = images[0].get("source") or {}
        url = _web_url(source.get("url"))
        if url:
            return url
    thumbnail = _web_url(post.get("thumbnail"))
    if thumbnail not in {"", "self", "default", "nsfw", "spoiler"}:
        return thumbnail
    return ""


def _native_video(post: dict[str, Any]) -> dict[str, Any]:
    for container_key in ("secure_media", "media"):
        container = post.get(container_key) or {}
        video = container.get("reddit_video") or {}
        if isinstance(video, dict) and _clean_url(video.get("fallback_url")):
            return video
    preview = (post.get("preview") or {}).get("reddit_video_preview") or {}
    return preview if isinstance(preview, dict) else {}


def extract_reddit_media(post: dict[str, Any]) -> list[dict[str, str]]:
    """Return normalized native Reddit video metadata from one API post."""
    candidates = [post]
    crossposts = post.get("crosspost_parent_list") or []
    if isinstance(crossposts, list):
        candidates.extend(candidate for candidate in crossposts if isinstance(candidate, dict))

    for candidate in candidates:
        video = _native_video(candidate)
        video_url = _web_url(video.get("fallback_url"))
        if not video_url:
            continue
        source_url = _source_url(candidate) or _source_url(post)
        media = {
            "type": "video",
            "url": video_url,
            "poster_url": _poster_url(candidate) or _poster_url(post),
            "provider": "reddit",
            "source_url": source_url,
        }
        return [{key: value for key, value in media.items() if value}]
    return []


def _default_ytdlp_extractor(url: str) -> dict[str, Any]:
    import yt_dlp

    options = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
    }
    with yt_dlp.YoutubeDL(options) as ydl:
        info = ydl.extract_info(url, download=False)
    return info if isinstance(info, dict) else {}


def _reddit_embed_url(source_url: str) -> str:
    path = urlparse(source_url).path
    if not path.startswith("/"):
        return ""
    return (
        f"https://www.redditmedia.com{path}"
        "?ref_source=embed&ref=share&embed=true"
    )


class _RedditPlayerParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.poster_url = ""
        self.has_player = False

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        if tag.lower() != "shreddit-player":
            return
        self.has_player = True
        values = dict(attrs)
        self.poster_url = _web_url(values.get("poster"))


def _default_reddit_embed_loader(embed_url: str) -> str:
    request = Request(
        embed_url,
        headers={"User-Agent": "Mozilla/5.0 (compatible; Info2Action/1.0)"},
    )
    with urlopen(request, timeout=15) as response:
        return response.read(2_000_001).decode("utf-8", errors="replace")


def _embedded_reddit_media(
    source_url: str,
    loader: Callable[[str], str],
) -> list[dict[str, str]]:
    embed_url = _reddit_embed_url(source_url)
    if not embed_url:
        return []
    parser = _RedditPlayerParser()
    parser.feed(loader(embed_url)[:2_000_000])
    if not parser.has_player:
        return []
    media = {
        "type": "embed",
        "url": embed_url,
        "poster_url": parser.poster_url,
        "provider": "reddit",
        "source_url": source_url,
    }
    return [{key: value for key, value in media.items() if value}]


def _best_direct_video_url(info: dict[str, Any]) -> str:
    direct = _web_url(info.get("url"))
    if direct:
        return direct
    formats = info.get("formats") or []
    mp4_formats = [
        value
        for value in formats
        if isinstance(value, dict)
        and _web_url(value.get("url"))
        and value.get("vcodec") not in (None, "none")
        and value.get("ext") == "mp4"
    ]
    if not mp4_formats:
        return ""
    best = max(mp4_formats, key=lambda value: int(value.get("height") or 0))
    return _web_url(best.get("url"))


def complete_reddit_media(
    source_url: str,
    *,
    extractor: Callable[[str], dict[str, Any]] | None = None,
    embed_loader: Callable[[str], str] | None = None,
) -> list[dict[str, str]]:
    """Best-effort direct-video lookup with official Reddit embed fallback."""
    safe_source_url = _reddit_url(source_url)
    if not safe_source_url:
        return []
    try:
        info = (extractor or _default_ytdlp_extractor)(safe_source_url)
        video_url = _best_direct_video_url(info)
        if video_url:
            provider = str(info.get("extractor_key") or "reddit").lower()
            normalized_source = _reddit_url(info.get("webpage_url")) or safe_source_url
            media = {
                "type": "video",
                "url": video_url,
                "poster_url": _web_url(info.get("thumbnail")),
                "provider": provider,
                "source_url": normalized_source,
            }
            return [{key: value for key, value in media.items() if value}]
    except Exception as exc:  # noqa: BLE001 - completion must never block ingest/backfill loops
        logger.warning("reddit media completion skipped for %s: %s", safe_source_url, exc)

    loader = embed_loader
    if loader is None and extractor is None:
        loader = _default_reddit_embed_loader
    if loader is None:
        return []
    try:
        return _embedded_reddit_media(safe_source_url, loader)
    except Exception as exc:  # noqa: BLE001 - embed fallback is also best effort
        logger.warning("reddit embed completion skipped for %s: %s", safe_source_url, exc)
        return []
