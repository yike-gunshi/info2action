#!/usr/bin/env python3
"""One-off maximal source backfill for a date window.

This is intentionally separate from ``ops/fetch_all.sh`` and ``/api/fetch``:
those paths are tuned for small recurring windows, while this runner is for a
manual historical catch-up where the stopping condition is time/no-more-data
rather than product count limits.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Iterable

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))

import db  # noqa: E402
import ingest  # noqa: E402
import ai_provider_guard  # noqa: E402
from clustering import visibility_policy  # noqa: E402
from env_utils import load_project_env  # noqa: E402
from time_utils import parse_datetime  # noqa: E402


LOCAL_TZ = timezone(timedelta(hours=8))
DEFAULT_SINCE = "2026-04-29"
DEFAULT_SOURCES = (
    "twitter",
    "lingowhale",
    "reddit",
    "hackernews",
    "rss",
    "waytoagi",
    "github",
    "bilibili",
    "xiaohongshu",
)
USER_AGENT = "info2action/1.0"


@dataclass
class SourceStats:
    fetched: int = 0
    ingested: int = 0
    skipped_old: int = 0
    errors: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "fetched": self.fetched,
            "ingested": self.ingested,
            "skipped_old": self.skipped_old,
        }
        if self.errors:
            out["errors"] = self.errors
        if self.notes:
            out["notes"] = self.notes
        return out


@dataclass
class BackfillContext:
    conn: Any
    config: dict[str, Any]
    topics: dict[str, Any]
    run_id: int
    since: datetime
    until: datetime
    args: argparse.Namespace
    stats: dict[str, SourceStats] = field(default_factory=dict)


def _load_json(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def _load_config() -> dict[str, Any]:
    return _load_json(BASE / "config" / "config.json")


def _load_topics() -> dict[str, Any]:
    path = BASE / "config" / "topics.json"
    return _load_json(path) if path.exists() else {"topics": []}


def _apply_project_env() -> None:
    """Make one-off CLIs behave like dev-stack scripts that source .env."""
    for key, value in load_project_env(BASE).items():
        os.environ.setdefault(key, value)
    local_bin = str(Path.home() / ".local" / "bin")
    os.environ["PATH"] = local_bin + os.pathsep + os.environ.get("PATH", "")


def _parse_window_start(value: str) -> datetime:
    text = value.strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        return datetime.fromisoformat(text).replace(tzinfo=LOCAL_TZ).astimezone(timezone.utc)
    parsed = parse_datetime(text)
    if parsed is None:
        raise argparse.ArgumentTypeError(f"invalid datetime: {value}")
    return parsed


def _parse_window_end(value: str | None) -> datetime:
    if not value:
        return datetime.now(LOCAL_TZ).astimezone(timezone.utc)
    return _parse_window_start(value)


def _iso_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def _window_label(start: datetime, end: datetime) -> str:
    local_start = start.astimezone(LOCAL_TZ)
    local_end = end.astimezone(LOCAL_TZ)
    if local_start.date() == local_end.date():
        return f"{local_start.date().isoformat()} {local_start.strftime('%H:%M')}-{local_end.strftime('%H:%M')}"
    return f"{local_start.isoformat()} → {local_end.isoformat()}"


def iter_processing_windows(
    since: datetime,
    until: datetime,
    days: int,
    hours: int | None = None,
) -> list[tuple[datetime, datetime]]:
    if hours and hours > 0:
        windows: list[tuple[datetime, datetime]] = []
        cursor_end = until.astimezone(LOCAL_TZ)
        since_local = since.astimezone(LOCAL_TZ)
        while cursor_end > since_local:
            hour_start = cursor_end.replace(minute=0, second=0, microsecond=0)
            if cursor_end == hour_start:
                raw_start = hour_start - timedelta(hours=hours)
            else:
                raw_start = hour_start - timedelta(hours=max(0, hours - 1))
            window_start_local = max(since_local, raw_start)
            if window_start_local >= cursor_end:
                break
            windows.append((
                window_start_local.astimezone(timezone.utc),
                cursor_end.astimezone(timezone.utc),
            ))
            cursor_end = window_start_local
        return windows

    if days <= 0:
        return [(since, until)]
    windows: list[tuple[datetime, datetime]] = []
    cursor_end = until.astimezone(LOCAL_TZ)
    since_local = since.astimezone(LOCAL_TZ)
    while cursor_end > since_local:
        day_start = cursor_end.replace(hour=0, minute=0, second=0, microsecond=0)
        if cursor_end == day_start:
            raw_start = day_start - timedelta(days=days)
        else:
            raw_start = day_start - timedelta(days=max(0, days - 1))
        window_start_local = max(since_local, raw_start)
        if window_start_local >= cursor_end:
            break
        windows.append((
            window_start_local.astimezone(timezone.utc),
            cursor_end.astimezone(timezone.utc),
        ))
        cursor_end = window_start_local
    return windows


def _twitter_until_date(until: datetime) -> str:
    """Twitter CLI's --until is date-like; use next local date as exclusive bound."""
    local_until = until.astimezone(LOCAL_TZ)
    return (local_until.date() + timedelta(days=1)).isoformat()


def _since_date(since: datetime) -> str:
    return since.astimezone(LOCAL_TZ).date().isoformat()


def _safe_slug(value: str) -> str:
    slug = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff._-]+", "_", value.strip())[:80]
    return slug or "query"


def _dedupe(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if not text:
            continue
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(text)
    return out


def _topic_queries(ctx: BackfillContext) -> list[str]:
    queries: list[str] = []
    for topic in ctx.topics.get("topics", []):
        queries.extend(topic.get("search_queries", []) or [])
    return _dedupe(queries)


def _search_queries(ctx: BackfillContext, platform: str) -> list[str]:
    cfg = ctx.config
    queries: list[str] = []
    queries.extend(cfg.get("global", {}).get("search_keywords", []) or [])
    queries.extend((cfg.get(platform, {}).get("search", {}) or {}).get("extra_keywords", []) or [])
    queries.extend(_topic_queries(ctx))
    return _dedupe(queries)


def _event_dt(value: Any, fallback: Any = None) -> datetime | None:
    dt = parse_datetime(value)
    if dt is not None:
        return dt
    return parse_datetime(fallback)


def _unix_dt(value: Any) -> datetime | None:
    try:
        if value is None or value == "":
            return None
        return datetime.fromtimestamp(float(value), tz=timezone.utc)
    except (TypeError, ValueError, OSError):
        return None


def _is_in_window(row: dict[str, Any], since: datetime, until: datetime) -> bool:
    # For freshly fetched source rows, missing/unknown published_at means this
    # is a current snapshot from a source that cannot expose historical dates
    # (for example GitHub Trending). Include it instead of comparing the
    # freshly written fetched_at to the run's start-time ``until`` boundary.
    published_at = row.get("published_at")
    if not published_at:
        return True
    dt = parse_datetime(published_at)
    if dt is None:
        return True
    return since <= dt <= until


def _filter_rows_since(
    rows: list[dict[str, Any]],
    since: datetime,
    until: datetime,
) -> tuple[list[dict[str, Any]], int]:
    kept: list[dict[str, Any]] = []
    skipped = 0
    for row in rows:
        if _is_in_window(row, since, until):
            kept.append(row)
        else:
            skipped += 1
    return kept, skipped


def _items_from_payload(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    if not isinstance(payload, dict):
        return []
    for key in ("data", "items", "posts", "repos"):
        value = payload.get(key)
        if isinstance(value, list):
            return [x for x in value if isinstance(x, dict)]
        if isinstance(value, dict):
            nested = _items_from_payload(value)
            if nested:
                return nested
    return []


def _run_json_command(
    cmd: list[str],
    *,
    timeout: int = 300,
) -> tuple[Any | None, str | None]:
    try:
        result = subprocess.run(
            cmd,
            cwd=BASE,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=os.environ.copy(),
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        return None, str(exc)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        return None, detail[:500] or f"exit {result.returncode}"
    try:
        return json.loads(result.stdout), None
    except json.JSONDecodeError as exc:
        return None, f"invalid json: {exc}"


def _upsert_source_rows(
    ctx: BackfillContext,
    source: str,
    rows: list[dict[str, Any]],
    stats: SourceStats,
) -> None:
    filtered, skipped = _filter_rows_since(rows, ctx.since, ctx.until)
    stats.fetched += len(rows)
    stats.skipped_old += skipped
    if filtered:
        stats.ingested += db.batch_upsert(ctx.conn, filtered, fetch_run_id=ctx.run_id)
    ctx.stats[source] = stats


def _twitter_row(
    tweet: dict[str, Any],
    source: str,
    *,
    expand_articles: bool = False,
) -> dict[str, Any] | None:
    tid = str(tweet.get("id") or "").strip()
    if not tid:
        return None
    author = tweet.get("author") or {}
    if not isinstance(author, dict):
        author = {}
    metrics = tweet.get("metrics") or {}
    if not isinstance(metrics, dict):
        metrics = {}
    media = tweet.get("media") or []
    text = tweet.get("text") or ""
    urls = tweet.get("urls") or []
    detail: dict[str, Any] = {}
    if tweet.get("quotedTweet"):
        detail["quotedTweet"] = tweet["quotedTweet"]
    if tweet.get("isRetweet"):
        detail["isRetweet"] = True
        detail["retweetedBy"] = tweet.get("retweetedBy", "")
    if urls:
        detail["urls"] = urls

    article_title = tweet.get("articleTitle")
    article_text = tweet.get("articleText")
    is_x_article = ingest._is_x_article_tweet(text, urls)
    if not article_text and is_x_article and expand_articles:
        article_title, article_text = ingest._expand_x_article(tid)
    if article_text:
        title = article_title or (text[:80] if text else None)
        content = article_text
        detail["articleTitle"] = article_title
        detail["isXArticle"] = True
    else:
        title = text[:80] if text else None
        content = text
        if is_x_article:
            detail["isXArticle"] = True
            detail["articleExpandSkipped"] = True

    screen_name = author.get("screenName") or "_"
    metrics_dict = {
        "likes": metrics.get("likes", 0),
        "retweets": metrics.get("retweets", 0),
        "views": metrics.get("views", 0),
        "bookmarks": metrics.get("bookmarks", 0),
        "replies": metrics.get("replies", 0),
    }
    return {
        "id": tid,
        "platform": "twitter",
        "source": source,
        "title": title,
        "content": content,
        "author_name": author.get("name", ""),
        "author_id": author.get("id", ""),
        "author_avatar": author.get("profileImageUrl", ""),
        "url": f"https://x.com/{screen_name}/status/{tid}",
        "cover_url": ingest._twitter_cover_url(tid, media),
        "media_json": json.dumps(media, ensure_ascii=False) if media else None,
        "metrics_json": json.dumps(metrics_dict, ensure_ascii=False),
        "tags_json": None,
        "lang": tweet.get("lang", ""),
        "detail_json": json.dumps(detail, ensure_ascii=False) if detail else None,
        "comments_json": None,
        "ai_summary": None,
        "relevance_score": ingest.calc_relevance(content, "", metrics_dict, "twitter"),
        "fetched_at": ingest.now_ts(),
        "published_at": tweet.get("createdAtLocal") or tweet.get("createdAt") or None,
    }


def fetch_twitter(ctx: BackfillContext) -> None:
    stats = SourceStats()
    rows: list[dict[str, Any]] = []
    since = _since_date(ctx.since)
    until = _twitter_until_date(ctx.until)
    max_tweets = int(ctx.args.twitter_max)

    search_types = ["latest", "top"]
    for query in _search_queries(ctx, "twitter"):
        for search_type in search_types:
            cmd = [
                "twitter",
                "search",
                query,
                "-t",
                search_type,
                "--since",
                since,
                "--until",
                until,
                "-n",
                str(max_tweets),
                "--json",
            ]
            payload, err = _run_json_command(cmd, timeout=ctx.args.twitter_timeout)
            if err:
                stats.errors.append(f"search:{query}:{search_type}: {err}")
                continue
            for item in _items_from_payload(payload):
                row = _twitter_row(
                    item,
                    f"search:{query}:{search_type}",
                    expand_articles=ctx.args.expand_x_articles,
                )
                if row:
                    rows.append(row)
            time.sleep(ctx.args.twitter_delay)

    for feed_type in ("following", "for-you"):
        cmd = ["twitter", "feed", "-t", feed_type, "-n", str(ctx.args.feed_max), "--json"]
        payload, err = _run_json_command(cmd, timeout=ctx.args.twitter_timeout)
        if err:
            stats.errors.append(f"feed:{feed_type}: {err}")
            continue
        for item in _items_from_payload(payload):
            row = _twitter_row(
                item,
                feed_type.replace("-", "_"),
                expand_articles=ctx.args.expand_x_articles,
            )
            if row:
                rows.append(row)
    payload, err = _run_json_command(
        ["twitter", "bookmarks", "-n", str(ctx.args.feed_max), "--json"],
        timeout=ctx.args.twitter_timeout,
    )
    if err:
        stats.notes.append(f"bookmarks unavailable: {err}")
    else:
        for item in _items_from_payload(payload):
            row = _twitter_row(item, "bookmarks", expand_articles=ctx.args.expand_x_articles)
            if row:
                rows.append(row)

    _upsert_source_rows(ctx, "twitter", rows, stats)


def _reddit_cover(post: dict[str, Any]) -> str:
    thumbnail = post.get("thumbnail", "")
    if thumbnail in ("self", "default", "nsfw", "spoiler", ""):
        thumbnail = ""
    preview_imgs = (post.get("preview") or {}).get("images") or []
    preview_src = ""
    if preview_imgs:
        preview_src = ((preview_imgs[0].get("source") or {}).get("url") or "").replace("&amp;", "&")
    direct_url = post.get("url_overridden_by_dest") or ""
    if not preview_src and post.get("post_hint") == "image":
        preview_src = direct_url
    return preview_src or thumbnail


def _reddit_row(post: dict[str, Any], sub: str) -> dict[str, Any] | None:
    pid = post.get("id", "")
    if not pid:
        return None
    title = post.get("title", "")
    selftext = post.get("selftext", "")
    permalink = post.get("permalink", "")
    url = f"https://www.reddit.com{permalink}" if permalink else post.get("url", "")
    external_url = post.get("url", "") if not post.get("is_self", True) else ""
    flair = post.get("link_flair_text", "")
    metrics = {
        "score": post.get("score", 0),
        "upvote_ratio": post.get("upvote_ratio", 0),
        "comments": post.get("num_comments", 0),
    }
    detail = {}
    if external_url and external_url != url:
        detail["external_url"] = external_url
    if flair:
        detail["flair"] = flair
    return {
        "id": f"reddit_{pid}",
        "platform": "reddit",
        "source": f"r/{sub}",
        "title": title or None,
        "content": selftext[:2000] or None,
        "author_name": post.get("author", ""),
        "author_id": post.get("author", ""),
        "author_avatar": "",
        "url": url,
        "cover_url": _reddit_cover(post) or None,
        "media_json": None,
        "metrics_json": json.dumps(metrics, ensure_ascii=False),
        "tags_json": json.dumps([flair], ensure_ascii=False) if flair else None,
        "lang": "en",
        "detail_json": json.dumps(detail, ensure_ascii=False) if detail else None,
        "comments_json": None,
        "ai_summary": None,
        "relevance_score": ingest.calc_relevance(title, selftext, metrics, "reddit"),
        "fetched_at": ingest.now_ts(),
        "published_at": _unix_dt(post.get("created_utc")).isoformat() if post.get("created_utc") else None,
    }


def fetch_reddit(ctx: BackfillContext) -> None:
    import requests

    stats = SourceStats()
    rows: list[dict[str, Any]] = []
    headers = {"User-Agent": USER_AGENT}
    max_pages = int(ctx.args.reddit_max_pages or 0)
    for sub in ctx.config.get("reddit", {}).get("subreddits", []) or []:
        after = None
        page = 0
        while True:
            page += 1
            params = {"limit": 100}
            if after:
                params["after"] = after
            try:
                resp = requests.get(
                    f"https://www.reddit.com/r/{sub}/new.json",
                    headers=headers,
                    params=params,
                    timeout=20,
                )
                resp.raise_for_status()
                payload = resp.json()
            except Exception as exc:  # noqa: BLE001
                stats.errors.append(f"r/{sub}: page {page}: {exc}")
                break
            children = (payload.get("data") or {}).get("children") or []
            if not children:
                break
            reached_old = False
            for child in children:
                post = child.get("data") or {}
                created = _unix_dt(post.get("created_utc"))
                if created and created < ctx.since:
                    reached_old = True
                    continue
                row = _reddit_row(post, sub)
                if row:
                    rows.append(row)
            after = (payload.get("data") or {}).get("after")
            if reached_old or not after:
                break
            if max_pages and page >= max_pages:
                stats.notes.append(f"r/{sub}: stopped by technical max_pages={max_pages}")
                break
            time.sleep(ctx.args.reddit_delay)
    _upsert_source_rows(ctx, "reddit", rows, stats)


def _hn_row(story: dict[str, Any], source: str) -> dict[str, Any] | None:
    sid = story.get("id", "")
    if not sid or story.get("type") != "story":
        return None
    title = story.get("title", "")
    text = re.sub(r"<[^>]+>", "", story.get("text", ""))[:2000] if story.get("text") else None
    metrics = {"score": story.get("score", 0), "comments": story.get("descendants", 0)}
    published = _unix_dt(story.get("time"))
    return {
        "id": f"hn_{sid}",
        "platform": "hackernews",
        "source": source,
        "title": title or None,
        "content": text,
        "author_name": story.get("by", ""),
        "author_id": story.get("by", ""),
        "author_avatar": "",
        "url": story.get("url") or f"https://news.ycombinator.com/item?id={sid}",
        "cover_url": None,
        "media_json": None,
        "metrics_json": json.dumps(metrics, ensure_ascii=False),
        "tags_json": None,
        "lang": "en",
        "detail_json": json.dumps(
            {"hn_url": f"https://news.ycombinator.com/item?id={sid}", "type": story.get("type", "story")},
            ensure_ascii=False,
        ),
        "comments_json": None,
        "ai_summary": None,
        "relevance_score": ingest.calc_relevance(title, text or "", metrics, "hackernews"),
        "fetched_at": ingest.now_ts(),
        "published_at": published.isoformat() if published else None,
    }


def fetch_hackernews(ctx: BackfillContext) -> None:
    import requests

    stats = SourceStats()
    rows: list[dict[str, Any]] = []
    seen: set[int] = set()
    max_ids = int(ctx.args.hn_max_ids)
    lists = ("newstories", "topstories", "beststories")
    for list_name in lists:
        try:
            ids = requests.get(
                f"https://hacker-news.firebaseio.com/v0/{list_name}.json",
                timeout=20,
            ).json()
        except Exception as exc:  # noqa: BLE001
            stats.errors.append(f"{list_name}: {exc}")
            continue
        reached_old = False
        for sid in ids[:max_ids]:
            if sid in seen:
                continue
            seen.add(sid)
            try:
                story = requests.get(
                    f"https://hacker-news.firebaseio.com/v0/item/{sid}.json",
                    timeout=10,
                ).json()
            except Exception as exc:  # noqa: BLE001
                stats.errors.append(f"{list_name}:{sid}: {exc}")
                continue
            story_dt = _unix_dt((story or {}).get("time"))
            if list_name == "newstories" and story_dt and story_dt < ctx.since:
                reached_old = True
                break
            row = _hn_row(story or {}, list_name.replace("stories", ""))
            if row:
                rows.append(row)
            time.sleep(ctx.args.hn_delay)
        if reached_old:
            stats.notes.append(f"{list_name}: reached {DEFAULT_SINCE} boundary")
    _upsert_source_rows(ctx, "hackernews", rows, stats)


def fetch_rss(ctx: BackfillContext) -> None:
    import feedparser
    import requests

    stats = SourceStats()
    rows: list[dict[str, Any]] = []
    for feed_cfg in ctx.config.get("rss", {}).get("feeds", []) or []:
        url = feed_cfg["url"]
        name = feed_cfg.get("name", url)
        slug = feed_cfg.get("slug") or _safe_slug(name)
        try:
            resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=20)
            parsed = feedparser.parse(resp.content)
        except Exception as exc:  # noqa: BLE001
            stats.errors.append(f"{name}: {exc}")
            continue
        feed_title = parsed.feed.get("title", name)
        for entry in parsed.entries:
            content_val = ""
            if entry.get("content"):
                content_val = entry["content"][0].get("value", "")
            entry_id = entry.get("id") or entry.get("link", "")
            if not entry_id:
                continue
            item_id = "rss_" + hashlib.md5(str(entry_id).encode()).hexdigest()[:12]
            title = entry.get("title", "")
            content = re.sub(r"<[^>]+>", "", content_val or entry.get("summary", ""))[:2000]
            published = entry.get("published", "")
            rows.append({
                "id": item_id,
                "platform": "rss",
                "source": f"feed:{slug}",
                "title": title or None,
                "content": content or None,
                "author_name": entry.get("author", feed_title),
                "author_id": "",
                "author_avatar": "",
                "url": entry.get("link", ""),
                "cover_url": None,
                "media_json": None,
                "metrics_json": None,
                "tags_json": json.dumps(
                    [t.get("term", "") for t in entry.get("tags", [])],
                    ensure_ascii=False,
                ) if entry.get("tags") else None,
                "lang": "en",
                "detail_json": json.dumps({"feed": feed_title, "entry_id": entry_id}, ensure_ascii=False),
                "comments_json": None,
                "ai_summary": None,
                "relevance_score": ingest.calc_relevance(title, content, {}, "rss"),
                "fetched_at": ingest.now_ts(),
                "published_at": published or None,
            })
    _upsert_source_rows(ctx, "rss", rows, stats)


def _lingowhale_rows(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for entry in entries:
        entry_id = str(entry.get("entry_id") or "")
        if not entry_id:
            continue
        title = entry.get("title", "")
        content = entry.get("content", "")
        abstract = entry.get("abstract", "") or entry.get("description", "")
        viewpoint = entry.get("viewpoint", [])
        if viewpoint:
            viewpoint = [v.replace("<hl>", "").replace("</hl>", "") if isinstance(v, str) else v for v in viewpoint]
        detail = {"group": entry.get("group_name", "未分组")}
        if abstract:
            detail["lingowhale_abstract"] = abstract.replace("<hl>", "").replace("</hl>", "")
        if viewpoint:
            detail["lingowhale_viewpoint"] = viewpoint
        info_source = entry.get("info_source") or {}
        channel = entry.get("channel") or {}
        published = _unix_dt(entry.get("pub_time"))
        rows.append({
            "id": f"lw_{entry_id}",
            "platform": "lingowhale",
            "source": channel.get("name", "subscription"),
            "title": title,
            "content": content,
            "author_name": info_source.get("info_source_name", ""),
            "author_id": "",
            "author_avatar": info_source.get("info_source_profile", ""),
            "url": entry.get("wechat_url") or f"https://lingowhale.com/reader/web/{entry_id}",
            "cover_url": entry.get("surface_url") or None,
            "media_json": None,
            "metrics_json": None,
            "tags_json": None,
            "lang": "zh",
            "detail_json": json.dumps(detail, ensure_ascii=False),
            "comments_json": None,
            "ai_summary": None,
            "ai_key_points": None,
            "relevance_score": ingest.calc_relevance(title, content, {}, "lingowhale"),
            "fetched_at": ingest.now_ts(),
            "published_at": published.isoformat() if published else None,
        })
    return rows


def fetch_lingowhale(ctx: BackfillContext) -> None:
    stats = SourceStats()
    rows: list[dict[str, Any]] = []
    try:
        import fetch_lingowhale as lw
    except SystemExit as exc:
        stats.errors.append(f"lingowhale import skipped: {exc}")
        ctx.stats["lingowhale"] = stats
        return

    if not lw.HEADERS.get("Auth-Token"):
        stats.errors.append("missing Lingowhale auth token")
        ctx.stats["lingowhale"] = stats
        return
    try:
        channel_to_group, _ = lw.fetch_groups()
    except Exception as exc:  # noqa: BLE001
        stats.errors.append(f"groups: {exc}")
        channel_to_group = {}

    entries: list[dict[str, Any]] = []
    cursor = ""
    seen_cursors: set[str] = set()
    page = 0
    max_pages = int(ctx.args.lingowhale_max_pages or 0)
    while True:
        page += 1
        body = json.dumps({"channel_ids": ["all"], "sort_type": 0, "cursor": cursor}).encode("utf-8")
        req = lw.urllib.request.Request(
            f"{lw.API_BASE}/api/feed/v2/feed/subscription",
            data=body,
            headers=lw.HEADERS,
            method="POST",
        )
        try:
            with lw.urllib.request.urlopen(req, timeout=30, context=lw._SSL_CTX) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except Exception as exc:  # noqa: BLE001
            stats.errors.append(f"page {page}: {exc}")
            break
        if data.get("code") != 0:
            stats.errors.append(f"api code={data.get('code')} msg={data.get('msg', '')}")
            break
        result = data.get("data") or {}
        page_entries = result.get("feed_list") or []
        if not page_entries:
            break
        fresh = [e for e in page_entries if (_unix_dt(e.get("pub_time")) or datetime.min.replace(tzinfo=timezone.utc)) >= ctx.since]
        entries.extend(fresh)
        if len(fresh) < len(page_entries):
            stats.notes.append("reached 4/29 boundary")
            break
        cursor = result.get("cursor") or ""
        if not result.get("has_more") or not cursor:
            break
        if cursor in seen_cursors:
            stats.notes.append("cursor repeated; stopped")
            break
        seen_cursors.add(cursor)
        if max_pages and page >= max_pages:
            stats.notes.append(f"stopped by technical max_pages={max_pages}")
            break
        time.sleep(ctx.args.lingowhale_delay)

    if entries:
        entries = lw.annotate_groups(entries, channel_to_group)
        entries = lw.enrich_entries(entries)
        try:
            lw.enrich_wechat_urls(entries)
        except Exception as exc:  # noqa: BLE001
            stats.notes.append(f"wechat url enrichment skipped: {exc}")
        rows = _lingowhale_rows(entries)
    _upsert_source_rows(ctx, "lingowhale", rows, stats)


def _waytoagi_rows(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for entry in entries:
        token = entry.get("id", "")
        if not token:
            continue
        item_id = "wtagi_" + hashlib.md5(token.encode()).hexdigest()[:12]
        title = entry.get("title", "")
        content = entry.get("content") or entry.get("summary", "")
        date_str = entry.get("date", "")
        rows.append({
            "id": item_id,
            "platform": "waytoagi",
            "source": "waytoagi:daily",
            "title": title or None,
            "content": content or None,
            "author_name": "WayToAGI",
            "author_id": "",
            "author_avatar": "",
            "url": entry.get("url", ""),
            "cover_url": entry.get("cover_url") or None,
            "media_json": None,
            "metrics_json": None,
            "tags_json": None,
            "lang": "zh",
            "detail_json": json.dumps({"wiki_token": token}, ensure_ascii=False),
            "comments_json": None,
            "ai_summary": None,
            "relevance_score": ingest.calc_relevance(title, content, {}, "waytoagi"),
            "fetched_at": ingest.now_ts(),
            "published_at": f"{date_str}T00:00:00" if date_str else None,
        })
    return rows


def fetch_waytoagi(ctx: BackfillContext) -> None:
    stats = SourceStats()
    try:
        import fetch_waytoagi as wtagi

        md = wtagi.fetch_doc_markdown(wtagi.WIKI_URL)
        if not md:
            stats.errors.append("main wiki fetch failed")
            ctx.stats["waytoagi"] = stats
            return
        items = wtagi.parse_daily_updates(md)
        items = [
            item for item in items
            if _event_dt(f"{item.get('date', '')}T00:00:00") and _event_dt(f"{item.get('date', '')}T00:00:00") >= ctx.since
        ]
        if items:
            wtagi.fetch_full_text(items)
            wtagi.fetch_cover_images(items)
        _upsert_source_rows(ctx, "waytoagi", _waytoagi_rows(items), stats)
    except Exception as exc:  # noqa: BLE001
        stats.errors.append(str(exc))
        ctx.stats["waytoagi"] = stats


def _github_repo_row(repo: dict[str, Any], source: str) -> dict[str, Any] | None:
    full_name = repo.get("full_name") or repo.get("fullName") or ""
    if not full_name:
        return None
    owner = full_name.split("/")[0] if "/" in full_name else ""
    stars = repo.get("stargazers_count", repo.get("stars", 0))
    forks = repo.get("forks_count", repo.get("forks", 0))
    lang = repo.get("language", "")
    description = repo.get("description") or ""
    metrics = {
        "stars": stars,
        "forks": forks,
        "stars_today": repo.get("stars_today", 0),
    }
    detail = {
        "language": lang,
        "source": source,
        "created_at": repo.get("created_at"),
        "updated_at": repo.get("updated_at"),
        "pushed_at": repo.get("pushed_at"),
    }
    return {
        "id": f"gh_{full_name.replace('/', '_')}",
        "platform": "github",
        "source": source,
        "title": full_name,
        "content": description or None,
        "author_name": owner,
        "author_id": owner,
        "author_avatar": f"https://github.com/{owner}.png" if owner else "",
        "url": repo.get("html_url") or repo.get("url") or f"https://github.com/{full_name}",
        "cover_url": None,
        "media_json": None,
        "metrics_json": json.dumps(metrics, ensure_ascii=False),
        "tags_json": json.dumps([lang], ensure_ascii=False) if lang else None,
        "lang": "en",
        "detail_json": json.dumps(detail, ensure_ascii=False),
        "comments_json": None,
        "ai_summary": None,
        "relevance_score": ingest.calc_relevance(full_name, description, metrics, "github"),
        "fetched_at": ingest.now_ts(),
        "published_at": repo.get("pushed_at") or repo.get("updated_at") or repo.get("created_at"),
    }


def _fetch_github_trending_rows(ctx: BackfillContext, stats: SourceStats) -> list[dict[str, Any]]:
    import requests

    rows: list[dict[str, Any]] = []
    cfg = ctx.config.get("github_trending", {})
    languages = cfg.get("languages", [""])
    seen: set[str] = set()
    for since in ("daily", "weekly", "monthly"):
        for lang in languages:
            label = lang or "all"
            url = f"https://github.com/trending/{lang}?since={since}"
            try:
                html = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=20).text
            except Exception as exc:  # noqa: BLE001
                stats.errors.append(f"trending:{label}:{since}: {exc}")
                continue
            articles = re.findall(r'<article class="Box-row">(.*?)</article>', html, re.DOTALL)
            for article in articles:
                full_name = ""
                for href in re.findall(r'href="(/[^"]+)"', article):
                    path = href.strip("/")
                    if "/" not in path or path.startswith(("login", "sponsors/", "apps/")):
                        continue
                    if path.endswith(("/stargazers", "/forks")):
                        continue
                    full_name = path
                    break
                if not full_name or full_name in seen:
                    continue
                seen.add(full_name)
                desc_match = re.search(r'<p class="[^"]*">(.*?)</p>', article, re.DOTALL)
                desc = re.sub(r"<[^>]+>", "", desc_match.group(1)).strip() if desc_match else ""
                lang_match = re.search(r'itemprop="programmingLanguage">(.*?)<', article)
                repo_lang = lang_match.group(1).strip() if lang_match else ""
                stars_today_match = re.search(r"([\d,]+)\s+stars\s+today", article)
                stars_today = int(stars_today_match.group(1).replace(",", "")) if stars_today_match else 0
                row = _github_repo_row({
                    "full_name": full_name,
                    "description": desc,
                    "language": repo_lang,
                    "stars_today": stars_today,
                    "url": f"https://github.com/{full_name}",
                }, f"trending:{label}:{since}")
                if row:
                    rows.append(row)
            time.sleep(ctx.args.github_delay)
    return rows


def _fetch_github_search_rows(ctx: BackfillContext, stats: SourceStats) -> list[dict[str, Any]]:
    import requests

    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    headers = {"User-Agent": USER_AGENT, "Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    queries = _dedupe(_search_queries(ctx, "github") + ["AI", "LLM", "AI agent", "Claude", "OpenAI", "MCP"])
    since_date = _since_date(ctx.since)
    pages = int(ctx.args.github_search_pages)
    for query in queries:
        search_q = f"{query} in:name,description,readme pushed:>={since_date}"
        for page in range(1, pages + 1):
            try:
                resp = requests.get(
                    "https://api.github.com/search/repositories",
                    headers=headers,
                    params={
                        "q": search_q,
                        "sort": "updated",
                        "order": "desc",
                        "per_page": 100,
                        "page": page,
                    },
                    timeout=25,
                )
                if resp.status_code in (403, 429):
                    stats.errors.append(f"search:{query}: rate limited/status {resp.status_code}")
                    return rows
                resp.raise_for_status()
                payload = resp.json()
            except Exception as exc:  # noqa: BLE001
                stats.errors.append(f"search:{query}: page {page}: {exc}")
                break
            items = payload.get("items") or []
            if not items:
                break
            for repo in items:
                full_name = repo.get("full_name", "")
                if not full_name or full_name in seen:
                    continue
                seen.add(full_name)
                row = _github_repo_row(repo, f"search:{query}")
                if row:
                    rows.append(row)
            if len(items) < 100:
                break
            time.sleep(ctx.args.github_delay)
    return rows


def fetch_github(ctx: BackfillContext) -> None:
    stats = SourceStats()
    rows = _fetch_github_trending_rows(ctx, stats)
    rows.extend(_fetch_github_search_rows(ctx, stats))
    _upsert_source_rows(ctx, "github", rows, stats)


def fetch_bilibili(ctx: BackfillContext) -> None:
    stats = SourceStats()
    rows: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    queries = _search_queries(ctx, "bilibili")
    for query_index, query in enumerate(queries, 1):
        print(f"  Bili search {query_index}/{len(queries)}: {query}", flush=True)
        for page in range(1, int(ctx.args.bili_max_pages) + 1):
            payload, err = _run_json_command(
                ["bili", "search", query, "--type", "video", "--page", str(page), "-n", "20", "--json"],
                timeout=ctx.args.bili_timeout,
            )
            if err:
                stats.errors.append(f"search:{query}: page {page}: {err}")
                break
            raw_items = _items_from_payload(payload)
            if not raw_items:
                break
            page_rows = [r for r in (ingest._bili_item_to_row(item, f"search:{query}") for item in raw_items) if r]
            new_rows = [row for row in page_rows if row["id"] not in seen_ids]
            for row in new_rows:
                seen_ids.add(row["id"])
            rows.extend(new_rows)
            if page % 10 == 0:
                print(f"    page {page}: total unique {len(rows)}", flush=True)
            if not new_rows:
                break
            dated = [_event_dt(r.get("published_at"), r.get("fetched_at")) for r in page_rows]
            if dated and all(dt and dt < ctx.since for dt in dated):
                break
            time.sleep(ctx.args.bili_delay)
    _upsert_source_rows(ctx, "bilibili", rows, stats)


def fetch_xiaohongshu(ctx: BackfillContext) -> None:
    stats = SourceStats()
    rows: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    queries = _search_queries(ctx, "xiaohongshu")
    for query_index, query in enumerate(queries, 1):
        print(f"  XHS search {query_index}/{len(queries)}: {query}", flush=True)
        for page in range(1, int(ctx.args.xhs_max_pages) + 1):
            payload, err = _run_json_command(
                ["xhs", "search", query, "--sort", "latest", "--page", str(page), "--json"],
                timeout=ctx.args.xhs_timeout,
            )
            if err:
                stats.errors.append(f"search:{query}: page {page}: {err}")
                break
            raw_items = _items_from_payload(payload)
            if not raw_items:
                break
            page_rows = [r for r in (ingest._xhs_item_to_row(item, f"search:{query}") for item in raw_items) if r]
            new_rows = [row for row in page_rows if row["id"] not in seen_ids]
            for row in new_rows:
                seen_ids.add(row["id"])
            rows.extend(new_rows)
            if page % 10 == 0:
                print(f"    page {page}: total unique {len(rows)}", flush=True)
            if not new_rows:
                break
            dated = [_event_dt(r.get("published_at"), r.get("fetched_at")) for r in page_rows]
            if dated and all(dt and dt < ctx.since for dt in dated):
                break
            time.sleep(ctx.args.xhs_delay)
    _upsert_source_rows(ctx, "xiaohongshu", rows, stats)


FETCHERS = {
    "twitter": fetch_twitter,
    "lingowhale": fetch_lingowhale,
    "reddit": fetch_reddit,
    "hackernews": fetch_hackernews,
    "rss": fetch_rss,
    "waytoagi": fetch_waytoagi,
    "github": fetch_github,
    "bilibili": fetch_bilibili,
    "xiaohongshu": fetch_xiaohongshu,
}


def attach_existing_since(conn: Any, run_id: int, since: datetime, until: datetime) -> dict[str, int]:
    """Attach already stored unfinished items in the date window to this run."""
    rows = conn.execute(
        """SELECT id, platform, published_at, fetched_at, fetch_run_id,
                  ai_summary, ai_category, ai_categories,
                  embedding IS NULL AS embedding_missing,
                  cluster_id
             FROM items"""
    ).fetchall()
    scoped_ids: list[str] = []
    for row in rows:
        item = dict(row)
        dt = _event_dt(item.get("published_at"), item.get("fetched_at"))
        if dt is None or dt < since or dt > until:
            continue
        unfinished = (
            item.get("cluster_id") is None
            or bool(item.get("embedding_missing"))
            or not item.get("ai_summary")
            or not item.get("ai_category")
            or not item.get("ai_categories")
        )
        if unfinished:
            scoped_ids.append(item["id"])
    for start in range(0, len(scoped_ids), 500):
        chunk = scoped_ids[start:start + 500]
        placeholders = ",".join("?" * len(chunk))
        conn.execute(
            f"UPDATE items SET fetch_run_id=? WHERE id IN ({placeholders})",
            [run_id, *chunk],
        )
    conn.commit()
    return {"attached_existing": len(scoped_ids)}


def attach_ready_cluster_only_since(
    conn: Any,
    run_id: int,
    since: datetime,
    until: datetime,
    *,
    require_published_at: bool = True,
) -> dict[str, int]:
    """Attach only existing items that can enter clustering without AI/embed work."""
    window_filter, window_params = _window_sql_filter(
        since,
        until,
        require_published_at=require_published_at,
    )
    category_ids = sorted(visibility_policy.HIGH_VALUE_SINGLE_SOURCE_CATEGORY_ALIASES)
    category_placeholders = ",".join("?" * len(category_ids))
    rows = conn.execute(
        f"""SELECT id
             FROM items
            WHERE cluster_id IS NULL
              AND embedding IS NOT NULL
              AND ai_summary IS NOT NULL AND ai_summary != ''
              AND ai_category IS NOT NULL AND ai_category != ''
              AND lower(COALESCE(ai_category, '')) IN ({category_placeholders})
              AND ai_categories IS NOT NULL AND ai_categories != ''
              AND ai_quality_score IS NOT NULL
              {window_filter}""",
        tuple(category_ids) + tuple(window_params),
    ).fetchall()
    scoped_ids = [row["id"] for row in rows]
    for start in range(0, len(scoped_ids), 500):
        chunk = scoped_ids[start:start + 500]
        placeholders = ",".join("?" * len(chunk))
        conn.execute(
            f"UPDATE items SET fetch_run_id=? WHERE id IN ({placeholders})",
            [run_id, *chunk],
        )
    conn.commit()
    return {"attached_existing": len(scoped_ids)}


def reset_ai_retry_for_run(conn: Any, run_id: int) -> int:
    cur = conn.execute(
        """UPDATE items
              SET ai_retry_after=NULL
            WHERE fetch_run_id=?
              AND platform!='bilibili'
              AND ai_retry_after IS NOT NULL""",
        (run_id,),
    )
    conn.commit()
    return cur.rowcount or 0


def _window_sql_filter(
    window_start: datetime | None = None,
    window_end: datetime | None = None,
    *,
    require_published_at: bool = False,
) -> tuple[str, list[str]]:
    if not window_start and not window_end:
        return "", []
    expr = (
        "datetime(NULLIF(published_at, ''))"
        if require_published_at
        else "COALESCE(datetime(NULLIF(published_at, '')), datetime(NULLIF(fetched_at, '')))"
    )
    clauses: list[str] = []
    params: list[str] = []
    if require_published_at:
        clauses.append(" AND datetime(NULLIF(published_at, '')) IS NOT NULL")
    if window_start:
        clauses.append(f" AND {expr} >= datetime(?)")
        params.append(_iso_utc(window_start))
    if window_end:
        clauses.append(f" AND {expr} < datetime(?)")
        params.append(_iso_utc(window_end))
    return "".join(clauses), params


def pending_ai_count(
    conn: Any,
    run_id: int,
    window_start: datetime | None = None,
    window_end: datetime | None = None,
    require_published_at: bool = False,
) -> int:
    window_filter, window_params = _window_sql_filter(
        window_start,
        window_end,
        require_published_at=require_published_at,
    )
    row = conn.execute(
        f"""SELECT COUNT(*)
             FROM items
            WHERE fetch_run_id=?
              AND platform!='bilibili'
              {window_filter}
              AND (
                ai_summary IS NULL OR ai_summary=''
                OR ai_quality_score IS NULL
                OR ai_category IS NULL OR ai_category=''
                OR ai_categories IS NULL
              )""",
        (run_id, *window_params),
    ).fetchone()
    return int(row[0] or 0)


def _active_provider_messages(providers: Iterable[str] | None = None) -> list[str]:
    messages: list[str] = []
    selected = tuple(providers) if providers is not None else (
        ai_provider_guard.MINIMAX_CHAT_PROVIDER,
    )
    for provider in selected:
        try:
            if ai_provider_guard.is_action_required(provider) or ai_provider_guard.is_cooldown_active(provider):
                messages.append(ai_provider_guard.provider_message(provider))
        except Exception:
            continue
    return messages


def _record_provider_messages(ctx: BackfillContext, providers: Iterable[str] | None = None) -> None:
    for message in _active_provider_messages(providers):
        ctx.stats.setdefault("processing", SourceStats()).errors.append(message)
        print(f"[provider] {message}", flush=True)


def _record_pipeline_error(ctx: BackfillContext, output: str) -> bool:
    payload = _parse_last_json_line(output)
    if not payload or not payload.get("error"):
        return False
    message = str(payload.get("message") or payload.get("error") or "").strip()
    error = str(payload.get("error") or "pipeline_error").strip()
    detail = f"pipeline {error}: {message}" if message else f"pipeline {error}"
    ctx.stats.setdefault("processing", SourceStats()).errors.append(detail)
    print(f"[pipeline] {detail}", flush=True)
    return True


def run_processing_window(
    ctx: BackfillContext,
    window_start: datetime | None = None,
    window_end: datetime | None = None,
) -> bool:
    label = _window_label(window_start, window_end) if window_start and window_end else "full-window"
    require_published_at = bool(getattr(ctx.args, "window_require_published_at", False))
    processing = ctx.stats.setdefault("processing", SourceStats())
    processing.notes.append(f"window_start={label}")
    ready_cluster_only = bool(getattr(ctx.args, "ready_cluster_only", False))

    if ctx.args.skip_ai:
        ctx.stats.setdefault("processing", SourceStats()).notes.append("AI skipped by flag")
        return False

    if ready_cluster_only:
        ctx.stats.setdefault("processing", SourceStats()).notes.append(
            f"{label}: enrich skipped by ready_cluster_only"
        )
    else:
        if not ctx.args.respect_ai_retry_after:
            reset_count = reset_ai_retry_for_run(ctx.conn, ctx.run_id)
            if reset_count:
                ctx.stats.setdefault("processing", SourceStats()).notes.append(
                    f"reset ai_retry_after for {reset_count} run items"
                )

        enrich_cmd = [
            sys.executable,
            "-u",
            str(BASE / "src" / "enrich_items.py"),
            "--limit",
            "0",
            "--run-id",
            str(ctx.run_id),
            "--batch-size",
            str(ctx.args.batch_size),
            "--workers",
            str(ctx.args.workers),
        ]
        chat_interval = getattr(ctx.args, "chat_request_interval_sec", None)
        if chat_interval is not None:
            enrich_cmd.extend(["--request-interval-sec", str(chat_interval)])
        if window_start:
            enrich_cmd.extend(["--window-start", _iso_utc(window_start)])
        if window_end:
            enrich_cmd.extend(["--window-end", _iso_utc(window_end)])
        if require_published_at:
            enrich_cmd.append("--window-require-published-at")
        enrich = subprocess.run(enrich_cmd, cwd=BASE, env=os.environ.copy(), timeout=ctx.args.ai_timeout)
        if enrich.returncode != 0:
            ctx.stats.setdefault("processing", SourceStats()).errors.append(f"enrich exit {enrich.returncode}")
            _record_provider_messages(ctx, providers=(ai_provider_guard.MINIMAX_CHAT_PROVIDER,))
            return False
    remaining = pending_ai_count(
        ctx.conn,
        ctx.run_id,
        window_start,
        window_end,
        require_published_at=require_published_at,
    )
    ctx.stats.setdefault("processing", SourceStats()).notes.append(
        f"{label}: pending_ai_after_enrich={remaining}"
    )
    if remaining:
        return False
    if ctx.args.skip_cluster:
        ctx.stats.setdefault("processing", SourceStats()).notes.append("cluster skipped by flag")
        return False

    cluster_cmd = [
        sys.executable,
        str(BASE / "src" / "clustering" / "pipeline.py"),
        "--run-id",
        str(ctx.run_id),
    ]
    if window_start:
        cluster_cmd.extend(["--window-start", _iso_utc(window_start)])
    if window_end:
        cluster_cmd.extend(["--window-end", _iso_utc(window_end)])
    if require_published_at:
        cluster_cmd.append("--window-require-published-at")
    if ready_cluster_only:
        cluster_cmd.append("--feed-candidates-only")
    if getattr(ctx.args, "top_k", None) is not None:
        cluster_cmd.extend(["--top-k", str(ctx.args.top_k)])
    cluster_cmd.extend(["--judge-workers", str(ctx.args.judge_workers)])
    cluster_cmd.extend(["--judge-min-interval-sec", str(ctx.args.judge_min_interval_sec)])
    cluster_cmd.extend(["--summary-workers", str(getattr(ctx.args, "summary_workers", 1))])
    cluster = subprocess.run(
        cluster_cmd,
        cwd=BASE,
        env=os.environ.copy(),
        timeout=ctx.args.cluster_timeout,
        capture_output=True,
        text=True,
    )
    if cluster.stdout:
        print(cluster.stdout, end="" if cluster.stdout.endswith("\n") else "\n", flush=True)
    if cluster.stderr:
        print(cluster.stderr, end="" if cluster.stderr.endswith("\n") else "\n", file=sys.stderr, flush=True)
    if cluster.returncode != 0:
        ctx.stats.setdefault("processing", SourceStats()).errors.append(f"pipeline exit {cluster.returncode}")
        recorded = _record_pipeline_error(ctx, cluster.stdout)
        if not recorded:
            providers = None
            if ready_cluster_only:
                providers = (ai_provider_guard.MINIMAX_CHAT_PROVIDER,)
            _record_provider_messages(ctx, providers=providers)
        return False
    pipeline_stats = _parse_last_json_line(cluster.stdout)
    if pipeline_stats:
        ctx.stats.setdefault("processing", SourceStats()).notes.append(
            f"{label}: pipeline={json.dumps(pipeline_stats, ensure_ascii=False)}"
        )
        if int(pipeline_stats.get("summary_failed") or 0) > 0:
            ctx.stats.setdefault("processing", SourceStats()).errors.append(
                f"pipeline summary_failed={pipeline_stats.get('summary_failed')}"
            )
            return False
    return True


def run_processing(ctx: BackfillContext) -> bool:
    window_days = int(getattr(ctx.args, "process_window_days", 0) or 0)
    window_hours = int(getattr(ctx.args, "process_window_hours", 0) or 0)
    if window_days <= 0 and window_hours <= 0:
        return run_processing_window(ctx)

    windows = iter_processing_windows(
        ctx.since,
        ctx.until,
        window_days,
        hours=window_hours or None,
    )
    window_unit = f"{window_hours}h" if window_hours > 0 else f"{window_days}d"
    ctx.stats.setdefault("processing", SourceStats()).notes.append(
        f"windowed_processing=newest_first,{len(windows)} windows,{window_unit}"
    )
    for idx, (window_start, window_end) in enumerate(windows, start=1):
        label = _window_label(window_start, window_end)
        print(f"\n>>> Processing window {idx}/{len(windows)}: {label}", flush=True)
        ok = run_processing_window(ctx, window_start, window_end)
        if not ok:
            ctx.stats.setdefault("processing", SourceStats()).errors.append(
                f"window failed: {label}"
            )
            return False
        print(f"<<< Window done: {label}", flush=True)
    return True


def _parse_last_json_line(output: str) -> dict[str, Any] | None:
    for line in reversed((output or "").splitlines()):
        line = line.strip()
        if not line or not line.startswith("{"):
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        return value if isinstance(value, dict) else None
    return None


def _selected_sources(value: str) -> list[str]:
    if value.strip().lower() == "all":
        return list(DEFAULT_SOURCES)
    sources = _dedupe(part.strip().lower() for part in value.split(","))
    unknown = [source for source in sources if source not in FETCHERS]
    if unknown:
        raise argparse.ArgumentTypeError(f"unknown sources: {', '.join(unknown)}")
    return sources


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="One-off maximal backfill since a date")
    parser.add_argument("--since", default=DEFAULT_SINCE, help="local date/datetime start, default 2026-04-29")
    parser.add_argument("--until", default=None, help="optional local date/datetime end, default now")
    parser.add_argument("--run-id", type=int, default=None, help="resume/attach to an existing fetch_run_id")
    parser.add_argument("--sources", type=_selected_sources, default=list(DEFAULT_SOURCES),
                        help="comma-separated source list or all")
    parser.add_argument("--skip-fetch", action="store_true")
    parser.add_argument("--skip-ai", action="store_true")
    parser.add_argument("--skip-cluster", action="store_true")
    parser.add_argument(
        "--ready-cluster-only",
        action="store_true",
        help=(
            "attach only unclustered items that already have AI enrichment and "
            "embedding; skip enrichment and avoid embedding generation"
        ),
    )
    parser.add_argument("--respect-ai-retry-after", action="store_true",
                        help="do not clear stale item-level AI retry timers for this backfill run")
    parser.add_argument("--workers", type=int, default=40,
                        help="AI enrichment chat workers for backfill windows")
    parser.add_argument("--batch-size", type=int, default=5)
    parser.add_argument("--chat-request-interval-sec", type=float, default=0.2,
                        help="shared MiniMax chat gate for enrichment workers")
    parser.add_argument("--judge-workers", type=int, default=2,
                        help="LLM judge concurrency for windowed clustering backfill")
    parser.add_argument("--top-k", type=int, default=None,
                        help="override clustering stage1 top-k candidate count")
    parser.add_argument("--judge-min-interval-sec", type=float, default=3.0)
    parser.add_argument("--summary-workers", type=int, default=1,
                        help="cluster summary concurrency for windowed clustering backfill")
    parser.add_argument("--ai-timeout", type=int, default=7200)
    parser.add_argument("--cluster-timeout", type=int, default=7200)
    parser.add_argument(
        "--process-window-days",
        type=int,
        default=1,
        help="process AI+cluster in newest-first day windows; 0 disables windowing",
    )
    parser.add_argument(
        "--process-window-hours",
        type=int,
        default=0,
        help="override day windows with newest-first hour windows; e.g. 6 publishes recent hours first",
    )
    parser.add_argument(
        "--window-require-published-at",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="window by real published_at only; use --no-window-require-published-at to include fetched_at fallback snapshots",
    )
    parser.add_argument("--twitter-max", type=int, default=5000,
                        help="technical per-query CLI guard; not a product result limit")
    parser.add_argument("--feed-max", type=int, default=1500,
                        help="technical feed/bookmarks CLI guard for sources without date paging")
    parser.add_argument("--twitter-timeout", type=int, default=900)
    parser.add_argument("--twitter-delay", type=float, default=1.0)
    parser.add_argument("--expand-x-articles", action="store_true",
                        help="expand X Article bodies inline; off by default because it can dominate backfill time")
    parser.add_argument("--reddit-max-pages", type=int, default=0,
                        help="0 means page until old/no-more; nonzero is a technical guard")
    parser.add_argument("--reddit-delay", type=float, default=1.0)
    parser.add_argument("--hn-max-ids", type=int, default=5000)
    parser.add_argument("--hn-delay", type=float, default=0.05)
    parser.add_argument("--lingowhale-max-pages", type=int, default=0)
    parser.add_argument("--lingowhale-delay", type=float, default=0.3)
    parser.add_argument("--github-search-pages", type=int, default=10,
                        help="GitHub Search API exposes at most 1000 results per query")
    parser.add_argument("--github-delay", type=float, default=1.0)
    parser.add_argument("--bili-max-pages", type=int, default=80)
    parser.add_argument("--bili-timeout", type=int, default=180)
    parser.add_argument("--bili-delay", type=float, default=0.8)
    parser.add_argument("--xhs-max-pages", type=int, default=80)
    parser.add_argument("--xhs-timeout", type=int, default=180)
    parser.add_argument("--xhs-delay", type=float, default=1.5)
    return parser


def _print_stats(run_id: int, stats: dict[str, Any]) -> None:
    print("\n" + "=" * 60, flush=True)
    print(f"Backfill run #{run_id} summary", flush=True)
    for source, value in stats.items():
        print(f"- {source}: {json.dumps(value, ensure_ascii=False)}", flush=True)
    print("=" * 60, flush=True)


def main(argv: list[str] | None = None) -> int:
    _apply_project_env()
    parser = build_parser()
    args = parser.parse_args(argv)
    since = _parse_window_start(args.since)
    until = _parse_window_end(args.until)
    config = _load_config()
    topics = _load_topics()

    conn = db.get_conn()
    run_id = args.run_id or db.start_fetch_run(conn)
    ctx = BackfillContext(
        conn=conn,
        config=config,
        topics=topics,
        run_id=run_id,
        since=since,
        until=until,
        args=args,
    )

    print(
        f"Backfill run #{run_id}: {_since_date(since)} → {until.astimezone(LOCAL_TZ).isoformat()}",
        flush=True,
    )
    error: str | None = None
    interrupted = False
    processing_ok = False
    try:
        if not args.skip_fetch:
            for source in args.sources:
                print(f"\n>>> Fetching {source}...", flush=True)
                started = time.time()
                try:
                    FETCHERS[source](ctx)
                except Exception as exc:  # noqa: BLE001
                    ctx.stats[source] = SourceStats(errors=[str(exc)])
                elapsed = time.time() - started
                ctx.stats.setdefault(source, SourceStats()).notes.append(f"elapsed_sec={elapsed:.1f}")
                print(f"<<< {source}: {ctx.stats[source].as_dict()}", flush=True)

        if args.ready_cluster_only:
            attach_stats = attach_ready_cluster_only_since(
                conn,
                run_id,
                since,
                until,
                require_published_at=bool(args.window_require_published_at),
            )
            attach_note = "attached existing ready-to-cluster items in date window"
        else:
            attach_stats = attach_existing_since(conn, run_id, since, until)
            attach_note = "attached existing unfinished items in date window"
        ctx.stats["existing_scope"] = SourceStats(
            ingested=attach_stats["attached_existing"],
            notes=[attach_note],
        )
        processing_ok = run_processing(ctx)
        if not processing_ok:
            error = "processing incomplete; skipped publish if AI or cluster did not complete"
    except KeyboardInterrupt:
        interrupted = True
        error = "interrupted"
        print("\nInterrupted; marking backfill run as error.", flush=True)
    except Exception as exc:  # noqa: BLE001
        error = str(exc)
        raise
    finally:
        serializable_stats = {k: v.as_dict() for k, v in ctx.stats.items()}
        db.finish_fetch_run(conn, run_id, serializable_stats, error)
        _print_stats(run_id, serializable_stats)
        conn.close()

    if interrupted:
        return 130
    return 0 if processing_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
