"""Tests for src/asr_worker.py (v12.2 Twitter 视频 ASR).

策略: 外部调用全 mock (urllib/oss2/subprocess/MiniMax), 只验证主流程编排 + 错误分支写 DB.
"""
import asyncio
import json
import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
import asr_worker
import db as db_mod


@pytest.fixture
def test_db(tmp_path, monkeypatch):
    """独立测试 DB."""
    db_path = tmp_path / "test_feed.db"
    monkeypatch.setattr(db_mod, "DB_PATH", str(db_path))
    conn = db_mod.get_conn()
    yield conn
    conn.close()


@pytest.fixture
def video_item(test_db):
    """预置一条 Twitter 视频 item."""
    test_db.execute(
        "INSERT INTO items(id, platform, source, title, content, media_json, fetched_at) "
        "VALUES (?, 'twitter', 'following', ?, ?, ?, '2026-04-18T00:00:00')",
        ("t_v1", "测试视频",
         "简短 caption",
         json.dumps([{"type": "video", "url": "https://video.example.com/x.mp4"}])),
    )
    test_db.commit()
    return "t_v1"


# ── 纯函数 ──────────────────────────────────

class TestPureFunctions:
    def test_extract_mp4_url_none(self):
        assert asr_worker._extract_mp4_url(None) is None

    def test_extract_mp4_url_no_video_type(self):
        assert asr_worker._extract_mp4_url(
            json.dumps([{"type": "photo", "url": "x.jpg"}])
        ) is None

    def test_extract_mp4_url_video_found(self):
        url = asr_worker._extract_mp4_url(
            json.dumps([{"type": "video", "url": "https://x.mp4"}])
        )
        assert url == "https://x.mp4"

    def test_extract_mp4_url_malformed_json(self):
        assert asr_worker._extract_mp4_url("not json") is None

    def test_is_valid_summary_false_cases(self):
        assert asr_worker._is_valid_summary("") is False
        assert asr_worker._is_valid_summary("no markers") is False
        assert asr_worker._is_valid_summary(None) is False

    def test_is_valid_summary_true_with_preview(self):
        assert asr_worker._is_valid_summary("【精华速览】blah") is True

    def test_is_valid_summary_true_with_breakdown(self):
        assert asr_worker._is_valid_summary("【全文拆解】blah") is True

    def test_ffmpeg_extract_no_audio_raises_clean_error(self):
        stderr = "Output #0, mp3, to '/tmp/out.mp3':\nOutput file does not contain any stream"
        with patch.object(asr_worker.subprocess, "run", return_value=MagicMock(returncode=234, stderr=stderr)):
            with pytest.raises(asr_worker.NoAudioStreamError, match="no audio stream"):
                asr_worker.ffmpeg_extract_mp3("/tmp/in.mp4", "/tmp/out.mp3")


# ── transcribe_and_summarize: 失败分支 ─────────

class TestAsrWorkerFailures:
    """每个失败分支 必须写 asr_status 到 DB, 前端能读到."""

    def test_no_video_in_media_json(self, test_db, monkeypatch):
        """空态: media_json 无 video → failed_empty."""
        monkeypatch.setenv("DOUBAO_ASR_API_KEY", "dummy")
        test_db.execute(
            "INSERT INTO items(id, platform, source, fetched_at) "
            "VALUES ('t_empty', 'twitter', 'following', '2026-04-18T00:00:00')"
        )
        test_db.commit()

        result = asyncio.run(asr_worker.transcribe_and_summarize("t_empty", user_id=1))

        assert result.status == "failed_empty"
        row = test_db.execute(
            "SELECT asr_status, asr_failed_reason FROM items WHERE id='t_empty'"
        ).fetchone()
        assert row[0] == "failed_empty"
        assert "no video" in (row[1] or "")

    def test_download_failure_marks_failed_download(self, test_db, video_item, monkeypatch):
        """下载 mp4 抛异常 → failed_download + 错误写 DB."""
        monkeypatch.setenv("DOUBAO_ASR_API_KEY", "dummy")

        def _boom(url, dst):
            raise RuntimeError("network error 404")

        with patch.object(asr_worker, "download_mp4", side_effect=_boom):
            result = asyncio.run(asr_worker.transcribe_and_summarize(video_item, user_id=1))

        assert result.status == "failed_download"
        row = test_db.execute(
            "SELECT asr_status, asr_failed_reason FROM items WHERE id=?", (video_item,)
        ).fetchone()
        assert row[0] == "failed_download"
        assert "network error 404" in row[1]

    def test_no_audio_stream_marks_failed_empty(self, test_db, video_item, monkeypatch):
        """视频无音轨 → failed_empty,前端展示空态而不是 ffmpeg 报错。"""
        monkeypatch.setenv("DOUBAO_ASR_API_KEY", "dummy")
        events = []

        async def emit(event, payload):
            events.append((event, payload))

        with patch.object(asr_worker, "download_mp4", return_value=(1000, 0.1)), \
             patch.object(asr_worker, "ffmpeg_extract_mp3", side_effect=asr_worker.NoAudioStreamError("no audio stream in video")), \
             patch.object(asr_worker, "upload_to_oss", side_effect=AssertionError("upload should not be called")):
            result = asyncio.run(asr_worker.transcribe_and_summarize(video_item, user_id=1, emit=emit))

        assert result.status == "failed_empty"
        row = test_db.execute(
            "SELECT asr_status, asr_failed_reason FROM items WHERE id=?", (video_item,)
        ).fetchone()
        assert row[0] == "failed_empty"
        assert "no audio stream" in row[1]
        assert ("error", {"code": "empty_transcript", "message": "视频无语音内容"}) in events

    def test_empty_transcript_marks_failed_empty(self, test_db, video_item, monkeypatch):
        """豆包返回空 transcript → failed_empty 不重跑摘要."""
        monkeypatch.setenv("DOUBAO_ASR_API_KEY", "dummy")

        poll_result = (
            {"result": {"text": ""}},  # 空 transcript
            10,
            None,
        )
        mock_poll = AsyncMock(return_value=poll_result)

        with patch.object(asr_worker, "download_mp4", return_value=(1000, 0.1)), \
             patch.object(asr_worker, "ffmpeg_extract_mp3", return_value=(500, 0.05)), \
             patch.object(asr_worker, "ffprobe_duration", return_value=30.0), \
             patch.object(asr_worker, "upload_to_oss", return_value=("https://signed", "k", 0.1)), \
             patch.object(asr_worker, "doubao_submit", return_value=("req-1", None)), \
             patch.object(asr_worker, "doubao_poll_until_done", mock_poll), \
             patch.object(asr_worker, "regenerate_summary_from_transcript",
                          side_effect=AssertionError("should not be called for empty")):
            result = asyncio.run(asr_worker.transcribe_and_summarize(video_item, user_id=1))

        assert result.status == "failed_empty"
        row = test_db.execute(
            "SELECT asr_status, asr_duration_sec, asr_cost_yuan, ai_summary "
            "FROM items WHERE id=?", (video_item,)
        ).fetchone()
        assert row[0] == "failed_empty"
        assert row[1] == 30  # 时长仍然记录
        assert row[2] is not None and row[2] > 0  # 成本也记录
        assert row[3] is None  # ai_summary 未动

    def test_summary_failure_keeps_transcript(self, test_db, video_item, monkeypatch):
        """Transcript 成功但 MiniMax 失败 → asr_text 保留, ai_summary 不变, status=failed_summary."""
        monkeypatch.setenv("DOUBAO_ASR_API_KEY", "dummy")
        # 预置旧摘要, 应保留
        test_db.execute("UPDATE items SET ai_summary='OLD' WHERE id=?", (video_item,))
        test_db.commit()

        mock_poll = AsyncMock(return_value=(
            {"result": {"text": "这是一段完整的转写内容" * 5}},  # >20 字符
            10, None,
        ))

        with patch.object(asr_worker, "download_mp4", return_value=(1000, 0.1)), \
             patch.object(asr_worker, "ffmpeg_extract_mp3", return_value=(500, 0.05)), \
             patch.object(asr_worker, "ffprobe_duration", return_value=60.0), \
             patch.object(asr_worker, "upload_to_oss", return_value=("https://signed", "k", 0.1)), \
             patch.object(asr_worker, "doubao_submit", return_value=("req-2", None)), \
             patch.object(asr_worker, "doubao_poll_until_done", mock_poll), \
             patch.object(asr_worker, "regenerate_summary_from_transcript",
                          side_effect=RuntimeError("MiniMax 429")):
            result = asyncio.run(asr_worker.transcribe_and_summarize(video_item, user_id=1))

        assert result.status == "failed_summary"
        row = test_db.execute(
            "SELECT asr_status, asr_text, ai_summary FROM items WHERE id=?", (video_item,)
        ).fetchone()
        assert row[0] == "failed_summary"
        assert row[1] and len(row[1]) > 20  # transcript 保留
        assert row[2] == "OLD"  # ai_summary 保持旧值

    def test_success_path_updates_both_transcript_and_summary(
            self, test_db, video_item, monkeypatch):
        """正常流程: transcript 写 DB, ai_summary 被新摘要覆盖, status=success."""
        monkeypatch.setenv("DOUBAO_ASR_API_KEY", "dummy")
        test_db.execute("UPDATE items SET ai_summary='OLD' WHERE id=?", (video_item,))
        test_db.commit()

        mock_poll = AsyncMock(return_value=(
            {"result": {"text": "Anthropic 工程师分享了 Claude Code 用法" * 3}},
            105, None,
        ))
        good_summary = "【精华速览】\n基于 transcript 生成的新摘要.\n\n【全文拆解】\n1. 核心要点"

        events = []
        async def emit(event, payload):
            events.append((event, payload))

        with patch.object(asr_worker, "download_mp4", return_value=(91_000_000, 4.7)), \
             patch.object(asr_worker, "ffmpeg_extract_mp3", return_value=(9_000_000, 2.2)), \
             patch.object(asr_worker, "ffprobe_duration", return_value=1547.2), \
             patch.object(asr_worker, "upload_to_oss", return_value=("https://signed", "k", 0.6)), \
             patch.object(asr_worker, "doubao_submit", return_value=("req-3", None)), \
             patch.object(asr_worker, "doubao_poll_until_done", mock_poll), \
             patch.object(asr_worker, "regenerate_summary_from_transcript",
                          return_value=good_summary):
            result = asyncio.run(asr_worker.transcribe_and_summarize(
                video_item, user_id=1, emit=emit,
            ))

        assert result.status == "success"
        assert result.transcript and len(result.transcript) > 20
        assert result.duration_sec == 1547
        assert result.cost_yuan and result.cost_yuan > 0

        row = test_db.execute(
            "SELECT asr_status, asr_text, ai_summary, asr_duration_sec "
            "FROM items WHERE id=?", (video_item,)
        ).fetchone()
        assert row[0] == "success"
        assert row[1] == result.transcript
        assert row[2] == good_summary  # 摘要被覆盖
        assert row[3] == 1547

        # 事件序列: transcript → summary_updated → done 必达
        event_names = [e[0] for e in events]
        assert "transcript" in event_names
        assert "summary_updated" in event_names
        assert "done" in event_names

    def test_skip_transcript_only_rewrites_summary(
            self, test_db, video_item, monkeypatch):
        """skip_transcript=1: 复用已有 asr_text, 只重跑 MiniMax."""
        monkeypatch.setenv("DOUBAO_ASR_API_KEY", "dummy")
        # 预置 transcript + 旧摘要
        test_db.execute(
            "UPDATE items SET asr_text=?, asr_duration_sec=60, ai_summary='OLD' WHERE id=?",
            ("这是一段已有的转写文本" * 3, video_item),
        )
        test_db.commit()

        new_summary = "【精华速览】\n新生成的摘要\n\n【全文拆解】\n- p1"

        # 断言豆包不被调用 (skip_transcript 路径)
        with patch.object(asr_worker, "download_mp4",
                          side_effect=AssertionError("should NOT download")), \
             patch.object(asr_worker, "doubao_submit",
                          side_effect=AssertionError("should NOT call Doubao")), \
             patch.object(asr_worker, "regenerate_summary_from_transcript",
                          return_value=new_summary):
            result = asyncio.run(asr_worker.transcribe_and_summarize(
                video_item, user_id=1, skip_transcript=True,
            ))

        assert result.status == "success"
        row = test_db.execute(
            "SELECT asr_status, ai_summary FROM items WHERE id=?", (video_item,)
        ).fetchone()
        assert row[0] == "success"
        assert row[1] == new_summary
