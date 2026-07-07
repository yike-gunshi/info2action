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
    """独立测试 DB. 进程 env 优先于项目 .env,钉死本地模式保证单测密闭."""
    monkeypatch.setenv("INFO2ACTION_APP_STATE_BACKEND", "sqlite")
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


# ── BF-0705-1: 事件循环不被阻塞 ──────────────────

class TestEventLoopNotBlocked:
    """BF-0705-1: transcribe_and_summarize 的重 IO(下载/ffmpeg/OSS/豆包/MiniMax)
    必须外移线程,否则单事件循环僵死 → 其他请求排队超 Cloudflare 100s → 524."""

    def test_transcribe_heavy_io_does_not_block_event_loop(
            self, test_db, video_item, monkeypatch):
        """下载 0.5s + 摘要 0.5s 期间,并发心跳协程必须持续获得调度."""
        import time as time_mod
        monkeypatch.setenv("DOUBAO_ASR_API_KEY", "dummy")

        def slow_download(url, dst):
            time_mod.sleep(0.5)  # 模拟大 mp4 下载
            return (1000, 0.5)

        def slow_summary(*args, **kwargs):
            time_mod.sleep(0.5)  # 模拟 MiniMax 长调用
            return "【精华速览】\n摘要\n\n【全文拆解】\n- p1"

        mock_poll = AsyncMock(return_value=(
            {"result": {"text": "这是一段完整的转写内容" * 5}}, 10, None,
        ))

        async def main() -> int:
            ticks = 0
            stop = asyncio.Event()

            async def ticker():
                nonlocal ticks
                while not stop.is_set():
                    await asyncio.sleep(0.01)
                    ticks += 1

            ticker_task = asyncio.create_task(ticker())
            result = await asr_worker.transcribe_and_summarize(video_item, user_id=1)
            stop.set()
            await ticker_task
            assert result.status == "success"
            return ticks

        with patch.object(asr_worker, "download_mp4", side_effect=slow_download), \
             patch.object(asr_worker, "ffmpeg_extract_mp3", return_value=(500, 0.05)), \
             patch.object(asr_worker, "ffprobe_duration", return_value=60.0), \
             patch.object(asr_worker, "upload_to_oss", return_value=("https://signed", "k", 0.1)), \
             patch.object(asr_worker, "doubao_submit", return_value=("req-el", None)), \
             patch.object(asr_worker, "doubao_poll_until_done", mock_poll), \
             patch.object(asr_worker, "regenerate_summary_from_transcript",
                          side_effect=slow_summary), \
             patch.object(asr_worker, "translate_transcript_cn", return_value=None), \
             patch.object(asr_worker, "translate_segments_cn", return_value=None):
            ticks = asyncio.run(main())

        # 阻塞版全程 ~1s 心跳只能拿到个位数 tick;外移线程后应 ≥30(理论 ~100)
        assert ticks >= 30, (
            f"事件循环在 ASR 重 IO 期间仅调度 {ticks} 次心跳(<30),"
            "同步 IO 仍在阻塞 event loop"
        )

    def test_doubao_poll_query_does_not_block_event_loop(self, monkeypatch):
        """轮询路径: 每轮 doubao_query 的同步 HTTP 也不得阻塞循环."""
        import time as time_mod

        calls = {"n": 0}

        def slow_query(request_id, api_key, resource_id):
            time_mod.sleep(0.3)  # 模拟豆包 query 网络往返
            calls["n"] += 1
            if calls["n"] >= 2:
                return "20000000", {"result": {"text": "done"}}
            return "20000001", {}

        async def main() -> int:
            ticks = 0
            stop = asyncio.Event()

            async def ticker():
                nonlocal ticks
                while not stop.is_set():
                    await asyncio.sleep(0.01)
                    ticks += 1

            ticker_task = asyncio.create_task(ticker())
            body, _elapsed, err = await asr_worker.doubao_poll_until_done(
                "req-x", "key", "rid", poll_interval=0, max_wait_sec=10,
            )
            stop.set()
            await ticker_task
            assert err is None and body is not None
            return ticks

        with patch.object(asr_worker, "doubao_query", side_effect=slow_query):
            ticks = asyncio.run(main())

        # 两轮 query 共 0.6s 阻塞;外移线程后心跳应 ≥20
        assert ticks >= 20, (
            f"doubao_poll_until_done 期间仅 {ticks} 次心跳(<20),"
            "doubao_query 同步 HTTP 仍在阻塞 event loop"
        )


# ── BF-0705-2: 摘要失败不丢翻译 ──────────────────

class TestTranslationDecoupledFromSummary:
    """BF-0705-2: 翻译只依赖 transcript+segments,不得被摘要格式校验失败挡住.

    实证: cluster 62784 视频豆包 236 段转写成功,MiniMax 摘要缺标记
    failed_summary 提前 return → asr_text_cn 从未写入,前端只有英文."""

    _UTTERANCES = [
        {"start_time": 0, "end_time": 1500, "text": "Claude Fable is officially back today."},
        {"start_time": 1500, "end_time": 3200, "text": "Let me walk you through what changed."},
    ]

    def _run(self, video_item, summary_output, cn_segments):
        mock_poll = AsyncMock(return_value=(
            {"result": {
                "text": "Claude Fable is officially back today. Let me walk you through what changed.",
                "utterances": self._UTTERANCES,
            }},
            10, None,
        ))
        with patch.object(asr_worker, "download_mp4", return_value=(1000, 0.1)), \
             patch.object(asr_worker, "ffmpeg_extract_mp3", return_value=(500, 0.05)), \
             patch.object(asr_worker, "ffprobe_duration", return_value=60.0), \
             patch.object(asr_worker, "upload_to_oss", return_value=("https://signed", "k", 0.1)), \
             patch.object(asr_worker, "doubao_submit", return_value=("req-t", None)), \
             patch.object(asr_worker, "doubao_poll_until_done", mock_poll), \
             patch.object(asr_worker, "regenerate_summary_from_transcript",
                          return_value=summary_output), \
             patch.object(asr_worker, "translate_segments_cn", return_value=cn_segments):
            return asyncio.run(asr_worker.transcribe_and_summarize(video_item, user_id=1))

    def test_summary_format_invalid_still_translates(self, test_db, video_item, monkeypatch):
        """摘要输出缺【精华速览】标记 → failed_summary,但中文翻译必须照常落库."""
        monkeypatch.setenv("DOUBAO_ASR_API_KEY", "dummy")
        result = self._run(video_item,
                           summary_output="A plain summary without required markers.",
                           cn_segments=["Claude Fable 今天正式回归。", "让我带你看看有什么变化。"])

        assert result.status == "failed_summary"
        row = test_db.execute(
            "SELECT asr_status, asr_text, asr_text_cn, asr_segments_cn FROM items WHERE id=?",
            (video_item,),
        ).fetchone()
        assert row[0] == "failed_summary"
        assert row[1] and len(row[1]) > 20          # 英文 transcript 在
        assert row[2] and "回归" in row[2]           # 中文翻译必须也在
        assert row[3] and len(json.loads(row[3])) == 2

    def test_success_path_still_translates(self, test_db, video_item, monkeypatch):
        """回归护栏: 摘要成功路径翻译照常(重排序不得破坏原语义)."""
        monkeypatch.setenv("DOUBAO_ASR_API_KEY", "dummy")
        good = "【精华速览】\n要点\n\n【全文拆解】\n1. 细节"
        result = self._run(video_item, summary_output=good,
                           cn_segments=["中文一。", "中文二。"])

        assert result.status == "success"
        row = test_db.execute(
            "SELECT asr_status, ai_summary, asr_text_cn FROM items WHERE id=?",
            (video_item,),
        ).fetchone()
        assert row[0] == "success"
        assert row[1] == good
        assert row[2] == "中文一。\n中文二。"
