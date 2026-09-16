"""Normalize item media into the public cluster media contract."""
from __future__ import annotations

import json
from typing import Any, Iterable
from urllib.parse import urlparse


def _clean(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _safe_url(value: Any) -> str | None:
    """Allow web URLs and app-local absolute paths only."""
    text = _clean(value)
    if not text or any(ord(char) < 32 for char in text):
        return None
    if text.startswith("/") and not text.startswith("//"):
        return text
    parsed = urlparse(text)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        return None
    return text


def normalize_media(
    cover_url: Any,
    media_json: Any,
    *,
    source_url: Any = None,
) -> list[dict[str, str]]:
    """Return ordered, deduplicated image/video/embed assets.

    Video entries precede cover images so cluster consumers can choose a
    playable asset first. ``media_urls`` remains derived separately as an
    image-only compatibility field.
    """
    media = media_json
    if isinstance(media, str):
        try:
            media = json.loads(media)
        except (TypeError, ValueError):
            media = []
    if not isinstance(media, list):
        media = []

    assets: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()

    def add(asset_type: str, url: Any, *, poster: Any = None,
            provider: Any = None, original: Any = None) -> None:
        clean_url = _safe_url(url)
        if not clean_url or asset_type not in {"image", "video", "embed"}:
            return
        key = (asset_type, clean_url)
        if key in seen:
            return
        seen.add(key)
        asset = {
            "type": asset_type,
            "url": clean_url,
            "source_url": _safe_url(original) or _safe_url(source_url) or clean_url,
        }
        clean_poster = _safe_url(poster)
        clean_provider = _clean(provider)
        if clean_poster:
            asset["poster_url"] = clean_poster
        if clean_provider:
            asset["provider"] = clean_provider
        assets.append(asset)

    for entry in media:
        if isinstance(entry, str):
            add("image", entry)
            continue
        if not isinstance(entry, dict):
            continue
        raw_type = str(entry.get("type") or "image").lower()
        if raw_type in {"video", "animated_gif"}:
            asset_type = "video"
        elif raw_type == "embed":
            asset_type = "embed"
        else:
            asset_type = "image"
        add(
            asset_type,
            entry.get("url") or entry.get("src"),
            poster=(entry.get("poster_url") or entry.get("preview_image_url")
                    or entry.get("thumbnail_url")),
            provider=entry.get("provider"),
            original=entry.get("source_url"),
        )

    cover = _safe_url(cover_url)
    if cover:
        playable_assets = [asset for asset in assets if asset["type"] in {"video", "embed"}]
        if playable_assets:
            cover_is_poster = any(asset.get("poster_url") == cover for asset in playable_assets)
            if not cover_is_poster:
                poster_target = next(
                    (asset for asset in playable_assets if not asset.get("poster_url")),
                    None,
                )
                if poster_target is not None:
                    poster_target["poster_url"] = cover
                else:
                    add("image", cover)
        else:
            add("image", cover)
    return assets


def image_urls(assets: Iterable[dict[str, Any]]) -> list[str]:
    """Return legacy image URLs, including a video's poster evidence."""
    urls: list[str] = []
    for asset in assets:
        candidate = asset.get("url") if asset.get("type") == "image" else asset.get("poster_url")
        value = _safe_url(candidate)
        if value and value not in urls:
            urls.append(value)
    return urls


def merge_media(groups: Iterable[Iterable[dict[str, Any]]]) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for group in groups:
        for asset in group:
            asset_type = str(asset.get("type") or "")
            safe_url = _safe_url(asset.get("url"))
            key = (asset_type, safe_url or "")
            if asset_type not in {"image", "video", "embed"} or not safe_url or key in seen:
                continue
            seen.add(key)
            safe_asset = dict(asset)
            safe_asset["url"] = safe_url
            safe_asset["source_url"] = _safe_url(asset.get("source_url")) or safe_url
            safe_poster = _safe_url(asset.get("poster_url"))
            if safe_poster:
                safe_asset["poster_url"] = safe_poster
            else:
                safe_asset.pop("poster_url", None)
            merged.append(safe_asset)
    return merged


def media_kind(assets: Iterable[dict[str, Any]]) -> str | None:
    kinds = [str(asset.get("type") or "") for asset in assets]
    for preferred in ("video", "embed", "image"):
        if preferred in kinds:
            return preferred
    return None
