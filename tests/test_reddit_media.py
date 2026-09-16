from __future__ import annotations

import json
import os
import sys


sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


def _native_video_post() -> dict:
    return {
        "id": "1v4ol30",
        "permalink": "/r/ClaudeAI/comments/1v4ol30/oversteer/",
        "preview": {
            "images": [
                {
                    "source": {
                        "url": "https://preview.redd.it/poster.jpg?x=1&amp;y=2",
                    }
                }
            ]
        },
        "secure_media": {
            "reddit_video": {
                "fallback_url": "https://v.redd.it/demo/DASH_720.mp4?source=fallback",
            }
        },
    }


def test_extract_reddit_media_keeps_native_video_poster_and_source():
    import reddit_media

    media = reddit_media.extract_reddit_media(_native_video_post())

    assert media == [
        {
            "type": "video",
            "url": "https://v.redd.it/demo/DASH_720.mp4?source=fallback",
            "poster_url": "https://preview.redd.it/poster.jpg?x=1&y=2",
            "provider": "reddit",
            "source_url": "https://www.reddit.com/r/ClaudeAI/comments/1v4ol30/oversteer/",
        }
    ]


def test_extract_reddit_media_uses_crosspost_native_video():
    import reddit_media

    post = {
        "id": "child",
        "permalink": "/r/OpenAI/comments/child/repost/",
        "crosspost_parent_list": [_native_video_post()],
    }

    media = reddit_media.extract_reddit_media(post)

    assert media[0]["type"] == "video"
    assert media[0]["url"].startswith("https://v.redd.it/")
    assert media[0]["source_url"].endswith("/r/ClaudeAI/comments/1v4ol30/oversteer/")


def test_complete_reddit_media_returns_empty_when_ytdlp_fails():
    import reddit_media

    def failing_extractor(_url: str) -> dict:
        raise RuntimeError("network blocked")

    assert (
        reddit_media.complete_reddit_media(
            "https://www.reddit.com/r/demo/comments/abc/post/",
            extractor=failing_extractor,
        )
        == []
    )


def test_complete_reddit_media_falls_back_to_official_embed_without_direct_video():
    import reddit_media

    source_url = "https://www.reddit.com/r/ClaudeAI/comments/1v4ol30/oversteer/"
    embed_html = """
        <shreddit-player
          src="https://v.redd.it/demo/HLSPlaylist.m3u8?token=temporary"
          poster="https://external-preview.redd.it/poster.jpg?x=1&amp;y=2"
          preview="https://v.redd.it/demo/CMAF_96.mp4">
        </shreddit-player>
    """

    assert reddit_media.complete_reddit_media(
        source_url,
        extractor=lambda _url: {},
        embed_loader=lambda url: embed_html if "redditmedia.com" in url else "",
    ) == [
        {
            "type": "embed",
            "url": "https://www.redditmedia.com/r/ClaudeAI/comments/1v4ol30/oversteer/?ref_source=embed&ref=share&embed=true",
            "poster_url": "https://external-preview.redd.it/poster.jpg?x=1&y=2",
            "provider": "reddit",
            "source_url": source_url,
        }
    ]


def test_complete_reddit_media_ignores_embed_page_without_player():
    import reddit_media

    assert reddit_media.complete_reddit_media(
        "https://www.reddit.com/r/demo/comments/abc/post/",
        extractor=lambda _url: {},
        embed_loader=lambda _url: "<html><body>post unavailable</body></html>",
    ) == []


def test_complete_reddit_media_rejects_non_reddit_source_before_extractor():
    import reddit_media

    called = []

    assert reddit_media.complete_reddit_media(
        "http://127.0.0.1/internal",
        extractor=lambda url: called.append(url) or {},
    ) == []
    assert called == []


def test_complete_reddit_media_rejects_localhost_and_private_literal_sources():
    import reddit_media

    called = []
    for source_url in (
        "http://localhost/internal",
        "http://127.0.0.1/internal",
        "http://10.0.0.8/internal",
        "http://[::1]/internal",
    ):
        assert reddit_media.complete_reddit_media(
            source_url,
            extractor=lambda url: called.append(url) or {},
        ) == []
    assert called == []


def test_complete_reddit_media_rejects_unsafe_extractor_media_urls():
    import reddit_media

    assert reddit_media.complete_reddit_media(
        "https://www.reddit.com/r/demo/comments/abc/post/",
        extractor=lambda _url: {
            "url": "file:///etc/passwd",
            "thumbnail": "javascript:alert(1)",
        },
    ) == []


def test_complete_reddit_media_normalizes_ytdlp_result():
    import reddit_media

    def extractor(_url: str) -> dict:
        return {
            "webpage_url": "https://www.reddit.com/r/demo/comments/abc/post/",
            "url": "https://v.redd.it/abc/DASH_480.mp4",
            "thumbnail": "https://preview.redd.it/abc.jpg",
            "extractor_key": "Reddit",
        }

    assert reddit_media.complete_reddit_media(
        "https://www.reddit.com/r/demo/comments/abc/post/",
        extractor=extractor,
    ) == [
        {
            "type": "video",
            "url": "https://v.redd.it/abc/DASH_480.mp4",
            "poster_url": "https://preview.redd.it/abc.jpg",
            "provider": "reddit",
            "source_url": "https://www.reddit.com/r/demo/comments/abc/post/",
        }
    ]


def test_ingest_reddit_persists_fetched_media_json(tmp_path, monkeypatch):
    import ingest

    reddit_dir = tmp_path / "reddit"
    reddit_dir.mkdir()
    post = {
        "id": "1v4ol30",
        "title": "OVERSTEER",
        "selftext": "Built with Godot",
        "author": "Grobot93",
        "url": "https://www.reddit.com/r/ClaudeAI/comments/1v4ol30/oversteer/",
        "permalink": "/r/ClaudeAI/comments/1v4ol30/oversteer/",
        "score": 10,
        "upvote_ratio": 0.9,
        "num_comments": 4,
        "created_utc": 0,
        "thumbnail": "https://preview.redd.it/poster.jpg",
        "link_flair_text": "",
        "is_self": True,
        "subreddit": "ClaudeAI",
        "media": [
            {
                "type": "video",
                "url": "https://v.redd.it/demo/DASH_720.mp4",
                "poster_url": "https://preview.redd.it/poster.jpg",
                "provider": "reddit",
                "source_url": "https://www.reddit.com/r/ClaudeAI/comments/1v4ol30/oversteer/",
            }
        ],
    }
    (reddit_dir / "ClaudeAI.json").write_text(
        json.dumps([post]),
        encoding="utf-8",
    )
    captured: list[dict] = []
    monkeypatch.setattr(ingest, "source_path", lambda _platform: str(reddit_dir))
    monkeypatch.setattr(
        ingest,
        "batch_upsert_current_run",
        lambda _conn, items: captured.extend(items),
    )

    assert ingest.ingest_reddit(object()) == 1
    assert json.loads(captured[0]["media_json"]) == post["media"]
    assert captured[0]["cover_url"] == "https://preview.redd.it/poster.jpg"


def test_ingest_reddit_passes_video_item_ids_to_asr_hook(tmp_path, monkeypatch):
    import ingest

    reddit_dir = tmp_path / "reddit"
    reddit_dir.mkdir()
    (reddit_dir / "ClaudeAI.json").write_text(
        json.dumps(
            [
                {
                    "id": "video",
                    "title": "Video",
                    "permalink": "/r/ClaudeAI/comments/video/demo/",
                    "media": [{"type": "video", "url": "https://v.redd.it/demo.mp4"}],
                },
                {
                    "id": "text",
                    "title": "Text",
                    "permalink": "/r/ClaudeAI/comments/text/demo/",
                    "media": [],
                },
            ]
        ),
        encoding="utf-8",
    )
    seen: list[str] = []
    monkeypatch.setattr(ingest, "source_path", lambda _platform: str(reddit_dir))
    monkeypatch.setattr(ingest, "batch_upsert_current_run", lambda _conn, _items: None)
    monkeypatch.setattr(
        ingest,
        "_run_asr_for_video_items_inline",
        lambda _conn, item_ids: seen.extend(item_ids),
        raising=False,
    )

    assert ingest.ingest_reddit(object()) == 2
    assert seen == ["reddit_video"]


def test_generic_video_asr_hook_supports_remote_authority(monkeypatch):
    import asr_worker
    import ingest
    from asr_worker import AsrResult

    monkeypatch.setenv("INGEST_SKIP_ASR", "0")
    monkeypatch.setenv("DOUBAO_ASR_API_KEY", "fake")
    monkeypatch.setenv("ASR_INGEST_CONCURRENCY", "1")
    monkeypatch.setattr(ingest.remote_db, "app_state_to_remote", lambda: True)
    monkeypatch.setattr(
        ingest.remote_db,
        "get_pending_asr_item_ids_remote",
        lambda item_ids: list(item_ids),
        raising=False,
    )
    seen = []
    monkeypatch.setattr(
        asr_worker,
        "run_asr_inline",
        lambda item_id, **_kwargs: (
            seen.append(item_id)
            or AsrResult("success", "transcript", 60, 0.01)
        ),
    )

    ingest._run_asr_for_video_items_inline(None, ["reddit_video"])

    assert seen == ["reddit_video"]


def test_asr_success_calls_real_item_and_cluster_resummary_chain(monkeypatch):
    import asr_worker
    from asr_worker import AsrResult
    from clustering import summary_writer

    item_calls = []
    cluster_calls = []

    async def fake_item_regenerator(
        item_id,
        user_id,
        emit=None,
        skip_transcript=False,
        regenerate_clusters=True,
    ):
        item_calls.append((item_id, user_id, skip_transcript, regenerate_clusters))
        return AsrResult("success", "transcript", 60, 0.01)

    monkeypatch.setattr(asr_worker, "transcribe_and_summarize", fake_item_regenerator)
    monkeypatch.setattr(
        asr_worker,
        "_cluster_ids_for_item",
        lambda item_id: [71, 72] if item_id == "reddit_video" else [],
        raising=False,
    )
    monkeypatch.setattr(
        asr_worker,
        "load_minimax_config",
        lambda: {"api_key": "k", "api_base": "https://api", "model": "m"},
    )
    monkeypatch.setattr(
        summary_writer,
        "regenerate_and_swap",
        lambda conn, cluster_id, **kwargs: (
            cluster_calls.append((conn, cluster_id, kwargs))
            or True
        ),
    )

    result = asr_worker._regenerate_summaries_after_asr("reddit_video")

    assert result == {"item_summary": True, "cluster_summaries": 2, "cluster_failures": 0}
    assert item_calls == [("reddit_video", 0, True, False)]
    assert [call[1] for call in cluster_calls] == [71, 72]


def test_run_asr_inline_triggers_resummary_after_transcript_success(
    tmp_path,
    monkeypatch,
):
    import asr_worker
    import db

    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "feed.db"))
    monkeypatch.setenv("DOUBAO_ASR_API_KEY", "fake")
    conn = db.get_conn()
    conn.execute(
        """
        INSERT INTO items
          (id, platform, source, title, content, url, media_json, fetched_at)
        VALUES
          ('reddit_video', 'reddit', 'r/demo', 'Video', 'Body',
           'https://reddit.com/video',
           '[{"type":"video","url":"https://v.redd.it/video.mp4"}]',
           '2026-07-24T00:00:00Z')
        """
    )
    conn.commit()
    monkeypatch.setattr(asr_worker, "download_mp4", lambda *_args: (100, 0.01))
    monkeypatch.setattr(asr_worker, "ffmpeg_extract_mp3", lambda *_args: (50, 0.01))
    monkeypatch.setattr(asr_worker, "ffprobe_duration", lambda *_args: 60.0)
    monkeypatch.setattr(
        asr_worker,
        "upload_to_oss",
        lambda *_args: ("https://audio", "key", 0.01),
    )
    monkeypatch.setattr(
        asr_worker,
        "doubao_submit",
        lambda *_args: ("request", None),
    )
    monkeypatch.setattr(
        asr_worker,
        "doubao_poll_until_done_sync",
        lambda *_args, **_kwargs: (
            {"result": {"text": "这是一段足够长的 Reddit 视频语音转写内容，用来验证后续摘要触发。"}},
            1,
            None,
        ),
    )
    seen = []
    monkeypatch.setattr(
        asr_worker,
        "_regenerate_summaries_after_asr",
        lambda item_id: seen.append(item_id) or {},
        raising=False,
    )

    result = asr_worker.run_asr_inline("reddit_video", conn=conn)
    conn.close()

    assert result.status == "success"
    assert seen == ["reddit_video"]


def test_upsert_existing_item_fills_previously_missing_media(tmp_path, monkeypatch):
    import db

    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "feed.db"))
    conn = db.get_conn()
    base = {
        "id": "reddit_1v4ol30",
        "platform": "reddit",
        "source": "r/ClaudeAI",
        "url": "https://www.reddit.com/r/ClaudeAI/comments/1v4ol30/oversteer/",
        "fetched_at": "2026-07-24T00:00:00Z",
    }
    db.upsert_item(conn, base)
    db.upsert_item(
        conn,
        {
            **base,
            "media_json": '[{"type":"video","url":"https://v.redd.it/demo.mp4"}]',
            "cover_url": "https://preview.redd.it/poster.jpg",
        },
    )
    conn.commit()

    row = conn.execute(
        "SELECT media_json, cover_url FROM items WHERE id=?",
        (base["id"],),
    ).fetchone()
    conn.close()

    assert json.loads(row["media_json"])[0]["type"] == "video"
    assert row["cover_url"] == "https://preview.redd.it/poster.jpg"


def test_remote_upsert_updates_media_and_treats_it_as_refresh_change():
    import remote_db

    sql = remote_db._item_upsert_sql("remote_poc")
    refresh_condition = remote_db._item_upsert_read_model_refresh_condition()
    noop_guard = remote_db._item_upsert_noop_guard("FALSE")

    assert "media_json = COALESCE(excluded.media_json, target.media_json)" in sql
    assert (
        "COALESCE(excluded.media_json, target.media_json) "
        "IS DISTINCT FROM target.media_json"
    ) in refresh_condition
    assert (
        "COALESCE(excluded.media_json, target.media_json) "
        "IS DISTINCT FROM target.media_json"
    ) in noop_guard
