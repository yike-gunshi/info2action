"""
v12.2 Twitter 视频 ASR 后端 worker
从 ops/poc_twitter_asr.py 迁移,去除 __main__ 改为可 import 模块

管道: DB.media_json(mp4 URL) → 下载 mp4 → ffmpeg 抽 mp3 → 上传 OSS
      → signed URL → 豆包 submit/query → 写 DB.asr_text → MiniMax 重跑摘要

关键原则(user feedback):
- 绝不把 mp4 原文件给豆包(token 消耗大)
- 必须本地抽 mp3 16kHz 单声道再公网托管
- 失败分支各写独立 asr_status
"""
from __future__ import annotations

import asyncio
import json
import os
import ssl
import subprocess
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

import oss2

import remote_db

BASE = Path(__file__).resolve().parents[1]

DOUBAO_SUBMIT = "https://openspeech.bytedance.com/api/v3/auc/bigmodel/submit"
DOUBAO_QUERY = "https://openspeech.bytedance.com/api/v3/auc/bigmodel/query"
DOUBAO_RESOURCE_ID_DEFAULT = "volc.seedasr.auc"

# 豆包大模型版按秒计费,大模型录音文件识别 ¥1.75/h = ¥0.0291/min = ¥0.000486/sec
DOUBAO_PRICE_PER_SEC = 1.75 / 3600

# 进度事件发布器类型: 供 routes/asr.py 注入 SSE 事件总线
EventEmitter = Callable[[str, dict], Awaitable[None]]


@dataclass
class AsrResult:
    """转写结果 + 元信息."""
    status: str                  # success / failed_*
    transcript: Optional[str]
    duration_sec: Optional[int]
    cost_yuan: Optional[float]
    failed_reason: Optional[str] = None


# ---------- HTTP 工具 ----------

def _http_post_json(url: str, headers: dict, body: dict, timeout: int = 30) -> tuple[int, dict, dict]:
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    ctx = ssl.create_default_context()
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            hdrs = dict(resp.getheaders())
            try:
                return resp.status, hdrs, json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                return resp.status, hdrs, {"__raw__": raw}
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        hdrs = dict(e.headers) if e.headers else {}
        try:
            return e.code, hdrs, json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            return e.code, hdrs, {"__raw__": raw}


# ---------- 下载 / ffmpeg ----------

class NoAudioStreamError(RuntimeError):
    """Raised when a video has no extractable audio stream for ASR."""


def _ffmpeg_error_has_no_audio(stderr: str) -> bool:
    text = (stderr or "").lower()
    return (
        "output file does not contain any stream" in text
        or "stream map 'a" in text and "matches no streams" in text
        or "does not contain any audio stream" in text
    )


def download_mp4(url: str, dst: str) -> tuple[int, float]:
    """下载 Twitter mp4 到本地 /tmp. 返回 (bytes, elapsed_sec)."""
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    ctx = ssl.create_default_context()
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=120, context=ctx) as resp:
        with open(dst, "wb") as f:
            while True:
                chunk = resp.read(131072)
                if not chunk:
                    break
                f.write(chunk)
    return os.path.getsize(dst), time.time() - t0


def ffmpeg_extract_mp3(mp4_path: str, mp3_path: str, bitrate: str = "48k") -> tuple[int, float]:
    """抽 16kHz 单声道 mp3. 48k 码率足够 ASR, 体积约为 mp4 的 10%."""
    cmd = [
        "ffmpeg", "-y", "-i", mp4_path, "-vn",
        "-acodec", "libmp3lame", "-ar", "16000", "-ac", "1",
        "-b:a", bitrate, mp3_path,
    ]
    t0 = time.time()
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        if _ffmpeg_error_has_no_audio(r.stderr):
            raise NoAudioStreamError("no audio stream in video")
        raise RuntimeError(f"ffmpeg failed: {r.stderr[-400:]}")
    return os.path.getsize(mp3_path), time.time() - t0


def ffprobe_duration(path: str) -> float:
    """返回音频/视频时长(秒). 失败返回 0.0."""
    cmd = [
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", path,
    ]
    r = subprocess.run(cmd, capture_output=True, text=True)
    try:
        return float(r.stdout.strip())
    except Exception:
        return 0.0


# ---------- OSS 上传 ----------

def upload_to_oss(mp3_path: str, tweet_id: str, signed_expire_sec: int = 3600) -> tuple[str, str, float]:
    """上传 mp3 到阿里云 OSS, 返回 (signed_url, object_key, elapsed_sec).

    环境变量 ALIYUN_OSS_AK/SK/ENDPOINT/BUCKET 必须已设置.
    签名 URL 有效期默认 1 小时, 足够豆包拉取.
    """
    ak = os.environ["ALIYUN_OSS_AK"]
    sk = os.environ["ALIYUN_OSS_SK"]
    endpoint = os.environ.get("ALIYUN_OSS_ENDPOINT", "https://oss-cn-beijing.aliyuncs.com")
    bucket_name = os.environ.get("ALIYUN_OSS_BUCKET", "n8n-temp-images")
    t0 = time.time()
    auth = oss2.Auth(ak, sk)
    bucket = oss2.Bucket(auth, endpoint, bucket_name)
    object_key = f"asr-poc/{tweet_id}_{uuid.uuid4().hex[:8]}.mp3"
    bucket.put_object_from_file(object_key, mp3_path)
    url = bucket.sign_url("GET", object_key, signed_expire_sec, slash_safe=True)
    return url, object_key, time.time() - t0


# ---------- 豆包 ASR ----------

def doubao_submit(audio_url: str, api_key: str, resource_id: str = DOUBAO_RESOURCE_ID_DEFAULT,
                  audio_format: str = "mp3") -> tuple[Optional[str], Optional[dict]]:
    """提交 ASR 任务. 成功返回 (request_id, None); 失败返回 (None, error_dict)."""
    request_id = str(uuid.uuid4())
    headers = {
        "Content-Type": "application/json",
        "x-api-key": api_key,
        "X-Api-Resource-Id": resource_id,
        "X-Api-Request-Id": request_id,
        "X-Api-Sequence": "-1",
    }
    body = {
        "user": {"uid": "info2action"},
        "audio": {
            "url": audio_url,
            "format": audio_format,
            "codec": "raw",
            "rate": 16000,
            "bits": 16,
            "channel": 1,
        },
        "request": {
            "model_name": "bigmodel",
            "enable_itn": True,
            "enable_punc": True,
            "enable_ddc": True,
            "enable_speaker_info": False,
            "enable_channel_split": False,
            "show_utterances": True,  # v12.3: 保留时间戳分段,前端联动用
        },
    }
    status, hdrs, body_resp = _http_post_json(DOUBAO_SUBMIT, headers, body)
    api_status = hdrs.get("X-Api-Status-Code", "")
    if api_status == "20000000":
        return request_id, None
    return None, {
        "http": status,
        "api_status": api_status,
        "msg": hdrs.get("X-Api-Message", ""),
        "body": body_resp,
    }


def doubao_query(request_id: str, api_key: str, resource_id: str = DOUBAO_RESOURCE_ID_DEFAULT) -> tuple[str, dict]:
    """查询 ASR 任务状态. 返回 (api_status_code, response_body)."""
    headers = {
        "Content-Type": "application/json",
        "x-api-key": api_key,
        "X-Api-Resource-Id": resource_id,
        "X-Api-Request-Id": request_id,
    }
    _, hdrs, body = _http_post_json(DOUBAO_QUERY, headers, {})
    return hdrs.get("X-Api-Status-Code", ""), body


async def doubao_poll_until_done(
    request_id: str, api_key: str, resource_id: str,
    emit_progress: Optional[EventEmitter] = None,
    max_wait_sec: int = 1800, poll_interval: int = 8,
) -> tuple[Optional[dict], int, Optional[str]]:
    """轮询直到完成, 每次发 progress 事件. 返回 (result_body, elapsed_sec, err_code)."""
    start = time.monotonic()
    base_percent = 30  # submit 完成后起点
    while time.monotonic() - start < max_wait_sec:
        await asyncio.sleep(poll_interval)
        api_status, body = doubao_query(request_id, api_key, resource_id)
        elapsed = int(time.monotonic() - start)
        if api_status == "20000000":
            return body, elapsed, None
        if api_status.startswith("2000000"):  # 处理中
            if emit_progress:
                bumped = min(85, base_percent + (elapsed // poll_interval) * 2)
                await emit_progress("progress", {
                    "phase": "asr_poll",
                    "message": "AI 识别中",
                    "percent": bumped,
                })
            continue
        return None, elapsed, api_status
    return None, int(time.monotonic() - start), "timeout"


# ---------- MiniMax 摘要(复用 generate_summaries.call_minimax)----------

def load_minimax_config() -> dict:
    with open(BASE / "config" / "config.json") as f:
        return json.load(f)["ai_summary"]


def regenerate_summary_from_transcript(transcript: str, tweet_title: str, tweet_text: str,
                                       duration_min: float) -> str:
    """基于 transcript 重跑 MiniMax 摘要, 复用项目现有 prompt."""
    from prompt_loader import load_prompt  # 延迟 import 避免循环

    prompt_tpl = load_prompt("02_summary_breakdown.md", category="AI工具")
    if not prompt_tpl:
        prompt_tpl = "你是信息精选助手。请对以下视频转录内容生成【精华速览】+【全文拆解】结构化摘要。"
    content = (
        f"原始 tweet 标题: {tweet_title or '(无)'}\n"
        f"原始 tweet 正文: {tweet_text or '(无)'}\n\n"
        f"以下是这条推文所含视频的 AI 语音转写(ASR transcript, {duration_min:.1f} 分钟):\n\n"
        f"{transcript}"
    )
    cfg = load_minimax_config()
    from generate_summaries import call_minimax
    return call_minimax(
        cfg["api_key"], cfg["api_base"], cfg["model"],
        prompt_tpl, content, max_tokens=cfg.get("max_tokens", 4096),
    )


# ---------- 主流程 ----------

def _is_valid_summary(text: str) -> bool:
    """LLM 输出写 DB 前格式校验(MEMORY: feedback_llm_output_validate_before_db)"""
    return bool(text and ("【精华速览】" in text or "【全文拆解】" in text))


_ASR_JSON_FIELDS = {"asr_segments", "asr_segments_cn"}


def _update_asr_fields(conn, item_id: str, **fields) -> None:
    """Update ASR item fields on the configured authoritative backend."""
    if not fields:
        return
    if remote_db.app_state_to_remote():
        remote_db.update_item_asr_fields_remote(item_id, **fields)
        return
    local_fields = {
        key: json.dumps(value, ensure_ascii=False)
        if key in _ASR_JSON_FIELDS and isinstance(value, (list, dict))
        else value
        for key, value in fields.items()
    }
    sets = ", ".join(f"{k} = ?" for k in local_fields.keys())
    vals = list(local_fields.values()) + [item_id]
    conn.execute(f"UPDATE items SET {sets} WHERE id = ?", vals)
    conn.commit()


def _write_asr_status(conn, item_id: str, **fields) -> None:
    """把 ASR 相关字段一把写入 items 表."""
    fields["asr_attempted_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    _update_asr_fields(conn, item_id, **fields)


def _utterances_to_segments(utterances: list) -> list:
    """v12.3: 豆包 utterances[{start_time, end_time, text, ...}] → segments[{start_ms, end_ms, text}].

    豆包返回字段命名变体覆盖: start_time/end_time(ms) 或 start/end(ms) 都兼容。
    不做合并/分句,保留豆包原粒度。
    """
    segs = []
    for u in utterances or []:
        text = (u.get("text") or "").strip()
        if not text:
            continue
        start_ms = u.get("start_time")
        if start_ms is None:
            start_ms = u.get("start")
        end_ms = u.get("end_time")
        if end_ms is None:
            end_ms = u.get("end")
        if start_ms is None or end_ms is None:
            continue
        segs.append({
            "start_ms": int(start_ms),
            "end_ms": int(end_ms),
            "text": text,
        })
    return segs


_RETRYABLE_HTTP = frozenset({408, 429, 500, 502, 503, 504})


def _call_minimax_translate(user_content: str, system_prompt: str,
                            max_retries: int = 3) -> Optional[str]:
    """独立 urllib 调 MiniMax Messages API,长 timeout + 大 max_tokens。

    BF-0420-11: YouTube 提交无中文翻译的根因 — 原实现单次失败返 None,
    对 300+ 段视频只要一次瞬时抖动/限流就整条失败。加自适应 retry:
    - 可重试(408/429/5xx、URLError、TimeoutError)→ 指数退避 + jitter
    - 不可重试(4xx 其他)→ 立即返 None
    - max_retries=3 即 3 次尝试(第 1 次 + 2 次重试)
    """
    import random
    import socket
    import ssl
    import time
    import urllib.error
    import urllib.request

    cfg = load_minimax_config()
    url = f"{cfg['api_base']}/messages"
    payload = json.dumps({
        "model": cfg["model"],
        "system": system_prompt,
        "max_tokens": 65536,
        "messages": [{"role": "user", "content": user_content}],
    }).encode("utf-8")
    headers = {
        "x-api-key": cfg["api_key"],
        "anthropic-version": "2023-06-01",
        "Content-Type": "application/json",
    }
    ctx = ssl.create_default_context()

    for attempt in range(max_retries):
        req = urllib.request.Request(url, data=payload, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=900, context=ctx) as resp:
                result = json.loads(resp.read().decode("utf-8"))
            for block in result.get("content", []):
                if block.get("type") == "text":
                    text = (block.get("text") or "").strip()
                    return text or None
            print("[asr_worker] minimax translate: no text block in response",
                  flush=True)
            return None
        except urllib.error.HTTPError as e:
            body_preview = ""
            try:
                body_preview = e.read().decode("utf-8", errors="replace")[:300]
            except Exception:
                pass
            err_desc = f"HTTP {e.code}: {body_preview}"
            if e.code in _RETRYABLE_HTTP and attempt < max_retries - 1:
                backoff = (2 ** attempt) + random.uniform(0, 1)
                print(f"[asr_worker] minimax translate {err_desc} "
                      f"(retry {attempt + 1}/{max_retries - 1} after {backoff:.1f}s)",
                      flush=True)
                time.sleep(backoff)
                continue
            print(f"[asr_worker] minimax translate {err_desc} (giving up)",
                  flush=True)
            return None
        except (urllib.error.URLError, socket.timeout, TimeoutError) as e:
            err_desc = f"{type(e).__name__}: {e}"
            if attempt < max_retries - 1:
                backoff = (2 ** attempt) + random.uniform(0, 1)
                print(f"[asr_worker] minimax translate {err_desc} "
                      f"(retry {attempt + 1}/{max_retries - 1} after {backoff:.1f}s)",
                      flush=True)
                time.sleep(backoff)
                continue
            print(f"[asr_worker] minimax translate {err_desc} (giving up)",
                  flush=True)
            return None
        except Exception as e:  # noqa: BLE001
            print(f"[asr_worker] minimax translate unrecoverable: {e}", flush=True)
            return None

    return None


def _is_mostly_chinese(text: str, threshold: float = 0.85) -> bool:
    """BF-0419-15: 判断文本是否主要为中文(CJK 字符占比 > threshold)。

    用于跳过"翻译中文到中文"的冗余调用 — v12.3 translate_segments_cn 不做语言检测,
    中文输入 MiniMax 返回接近原样 → 写入 asr_segments_cn → UI 双语夹杂冗余。

    Review 修:阈值 0.5 → 0.85。中英夹杂科技内容(中文 60% + 英文术语 40%)
    用户仍需要英文术语的翻译;只在几乎纯中文时才跳过翻译。
    """
    if not text:
        return False
    cjk = sum(1 for c in text if '\u4e00' <= c <= '\u9fff')
    meaningful = sum(1 for c in text if c.isalnum() or '\u4e00' <= c <= '\u9fff')
    if meaningful == 0:
        return False
    return cjk / meaningful > threshold


def translate_transcript_cn(transcript: str) -> Optional[str]:
    """v12.3 整段翻译(未用 segments 时的 fallback / 历史兼容)。"""
    if not transcript or not transcript.strip():
        return None
    system_prompt = (
        "你是专业中英翻译。请将用户提供的视频转写文本翻译成自然流畅的简体中文。\n"
        "规则:\n"
        "1. 保留原文换行结构(按行对齐)\n"
        "2. 专有名词/产品名/人名保留英文\n"
        "3. 不要改写、不要概括、不要加解释\n"
        "4. 只输出翻译结果,不要任何前后缀\n"
    )
    return _call_minimax_translate(transcript, system_prompt)


def translate_segments_cn(segments: list) -> Optional[list]:
    """v12.3 方案 B:按段带标号翻译,parse 回 list[str] 长度等同 segments。

    prompt 要求模型输出 `[N] 中文` 结构,返回时 regex parse。若标号缺失或数量不对,
    fallback None(前端降级整段展示或显示 failed)。
    """
    import re
    if not segments:
        return None
    # 构造带标号的输入
    numbered = "\n".join(f"[{i + 1}] {seg.get('text', '').strip()}"
                        for i, seg in enumerate(segments) if seg.get("text"))
    if not numbered:
        return None
    system_prompt = (
        "你是专业中英翻译。以下是视频转写的分段文本,每段前带 [N] 标号。\n"
        "规则:\n"
        "1. **严格保持 [N] 标号和段落数量**(输入 M 段 必须输出 M 段)\n"
        "2. 每段独占一行,格式 `[N] 中文译文`\n"
        "3. 专有名词/产品名/人名保留英文\n"
        "4. 不要合并段落、不要拆分段落、不要加解释前后缀\n"
        "5. 若原段本身是中文,直接复制该段\n"
    )
    out = _call_minimax_translate(numbered, system_prompt)
    if not out:
        return None
    # parse: [N] 中文内容
    result = [None] * len(segments)
    pattern = re.compile(r"^\[(\d+)\]\s*(.+?)$", re.MULTILINE)
    for m in pattern.finditer(out):
        idx = int(m.group(1)) - 1
        if 0 <= idx < len(segments):
            result[idx] = m.group(2).strip()
    # 缺失过多视为失败(>30% 没对上就返回 None)
    missing = sum(1 for x in result if x is None)
    if missing / len(segments) > 0.3:
        print(f"[asr_worker] translate_segments_cn parse miss {missing}/{len(segments)}, falling back", flush=True)
        return None
    # 补 None 为空字符串,前端显示空段仍占行
    return [x or "" for x in result]


def _extract_mp4_url(media_json: Optional[str]) -> Optional[str]:
    if not media_json:
        return None
    try:
        media = json.loads(media_json) if isinstance(media_json, str) else media_json
    except json.JSONDecodeError:
        return None
    if not isinstance(media, list):
        return None
    for m in media:
        if m.get("type") == "video":
            return m.get("url")
    return None


# ---------- v13.0: 同步版本 (可被 ingest/submit 直接 import) ----------

def doubao_poll_until_done_sync(
    request_id: str, api_key: str, resource_id: str,
    max_wait_sec: int = 900, poll_interval: int = 8,
) -> tuple[Optional[dict], int, Optional[str]]:
    """同步版豆包轮询 - 用于 ingest 批次并发场景。

    max_wait_sec 默认 900s (15min) 兜底,再套一层 ingest 级 `min(dur*3+60, 900)` 硬上限。
    不发 progress 事件(ingest 路径无 SSE bus)。
    """
    start = time.monotonic()
    while time.monotonic() - start < max_wait_sec:
        time.sleep(poll_interval)
        api_status, body = doubao_query(request_id, api_key, resource_id)
        if api_status == "20000000":
            return body, int(time.monotonic() - start), None
        if api_status.startswith("2000000"):
            continue
        return None, int(time.monotonic() - start), api_status
    return None, int(time.monotonic() - start), "timeout"


def _find_media_url_for_asr(conn, item_id: str) -> Optional[str]:
    """从 items.media_json 抽 mp4 URL。返回 None 表示无视频(或 JSON 坏)。"""
    if remote_db.app_state_to_remote():
        return _extract_mp4_url(remote_db.get_item_media_json_remote(item_id))
    row = conn.execute(
        "SELECT media_json FROM items WHERE id = ?", (item_id,)
    ).fetchone()
    if not row:
        return None
    return _extract_mp4_url(row["media_json"])


def _fetch_asr_worker_item(conn, item_id: str) -> Optional[dict]:
    """Fetch item context for the async ASR path from local or remote storage."""
    if remote_db.app_state_to_remote():
        return remote_db.get_asr_worker_item_remote(item_id)
    row = conn.execute(
        "SELECT id, title, content, ai_summary, media_json, url, asr_text, asr_duration_sec "
        "FROM items WHERE id = ?", (item_id,)
    ).fetchone()
    return dict(row) if row else None


def _check_asr_quota(conn, duration_sec: int, user_id=0) -> tuple[bool, dict]:
    if remote_db.app_state_to_remote():
        return remote_db.check_asr_quota_remote(None, duration_sec, user_id=user_id)
    import db

    return db.check_asr_quota(conn, duration_sec, user_id=user_id)


def _consume_asr_quota(conn, duration_sec: int, user_id=0) -> dict:
    if remote_db.app_state_to_remote():
        return remote_db.consume_asr_quota_remote(None, duration_sec, user_id=user_id)
    import db

    return db.consume_asr_quota(conn, duration_sec, user_id=user_id)


def run_asr_inline(
    item_id: str,
    bypass_quota: bool = False,
    conn=None,
    audio_source: Optional[dict] = None,
    max_wait_sec: Optional[int] = None,
    user_id=0,
) -> AsrResult:
    """v13.0: 同步 ASR 接口 — 供 ingest / submit / manual 触发直接 import 使用。

    **不要**和 `transcribe_and_summarize` 混用(后者带 asyncio + SSE emit)。

    Args:
        item_id:      items.id 主键
        bypass_quota: False → 先查配额,不足则 asr_status='skipped_quota' 返回(不消费秒)
                      True  → 跳过硬拦截(手动触发路径),但仍调用 consume_asr_quota 计入
        conn:         调用方可传入已创建连接;None 则内部新建
        audio_source: 用于 YouTube fallback 场景:
                      {'local_mp3': '/tmp/yt_xxx.mp3'} 直接用本地 mp3,不再走 media_json/mp4 下载
                      None 走 Twitter 默认(从 media_json 抽 mp4 URL)
        max_wait_sec: 豆包 query 轮询上限秒数;None 则默认 900s
                      ingest 路径会传入 `min(max(duration*3+60, 120), 900)` 做自适应
        user_id:      触发用户的配额桶;cron/ingest 默认走 legacy 0 桶

    Returns: AsrResult(status='success' / 'skipped_quota' / 'failed_*', ...)
    """
    own_conn = conn is None
    if own_conn and not remote_db.app_state_to_remote():
        import db
        conn = db.get_conn()

    api_key = os.environ.get("DOUBAO_ASR_API_KEY")
    resource_id = os.environ.get("DOUBAO_ASR_RESOURCE_ID", DOUBAO_RESOURCE_ID_DEFAULT)
    if not api_key:
        if own_conn and conn is not None:
            conn.close()
        raise RuntimeError("DOUBAO_ASR_API_KEY not set")

    mp4_url = None
    local_mp3 = None
    if audio_source and audio_source.get("local_mp3"):
        local_mp3 = audio_source["local_mp3"]
        if not os.path.exists(local_mp3):
            _write_asr_status(conn, item_id, asr_status="failed_download",
                              asr_failed_reason=f"local_mp3 missing: {local_mp3}")
            if own_conn and conn is not None: conn.close()
            return AsrResult("failed_download", None, None, None, "local mp3 missing")
    else:
        mp4_url = _find_media_url_for_asr(conn, item_id)
        if not mp4_url:
            _write_asr_status(conn, item_id, asr_status="failed_empty",
                              asr_failed_reason="no video in media_json")
            if own_conn and conn is not None: conn.close()
            return AsrResult("failed_empty", None, None, None, "no video")

    # 1. 准备音频文件 + 拿到 duration。本地 mp3 走 ffprobe。
    mp4_tmp = f"/tmp/asr_{item_id}.mp4"
    mp3_tmp = f"/tmp/asr_{item_id}.mp3"
    duration_sec: int = 0
    try:
        if local_mp3:
            mp3_tmp = local_mp3
            duration_sec = int(ffprobe_duration(local_mp3))
        else:
            try:
                download_mp4(mp4_url, mp4_tmp)
            except Exception as e:
                _write_asr_status(conn, item_id, asr_status="failed_download",
                                  asr_failed_reason=str(e)[:200])
                if own_conn and conn is not None: conn.close()
                return AsrResult("failed_download", None, None, None, str(e)[:200])
            try:
                ffmpeg_extract_mp3(mp4_tmp, mp3_tmp)
                duration_sec = int(ffprobe_duration(mp3_tmp))
            except NoAudioStreamError as e:
                _write_asr_status(conn, item_id, asr_status="failed_empty",
                                  asr_failed_reason=str(e))
                if own_conn and conn is not None: conn.close()
                return AsrResult("failed_empty", None, None, None, str(e))
            except Exception as e:
                _write_asr_status(conn, item_id, asr_status="failed_extract",
                                  asr_failed_reason=str(e)[:200])
                if own_conn and conn is not None: conn.close()
                return AsrResult("failed_extract", None, None, None, str(e)[:200])

        # 2. 配额检查(只在有 duration 后)— bypass_quota=True 跳过拦截
        if not bypass_quota and duration_sec > 0:
            allowed, _usage = _check_asr_quota(conn, duration_sec, user_id=user_id)
            if not allowed:
                _write_asr_status(conn, item_id, asr_status="skipped_quota",
                                  asr_duration_sec=duration_sec,
                                  asr_failed_reason="daily quota exhausted")
                if own_conn and conn is not None: conn.close()
                return AsrResult("skipped_quota", None, duration_sec, None, "daily quota")

        # mark running
        _write_asr_status(conn, item_id, asr_status="running",
                          asr_duration_sec=duration_sec)

        # 3. 上传 OSS
        try:
            audio_url, _oss_key, _up_sec = upload_to_oss(mp3_tmp, item_id)
        except Exception as e:
            _write_asr_status(conn, item_id, asr_status="failed_upload",
                              asr_failed_reason=str(e)[:200])
            if own_conn and conn is not None: conn.close()
            return AsrResult("failed_upload", None, duration_sec, None, str(e)[:200])

        # 4. 豆包 submit
        req_id, err = doubao_submit(audio_url, api_key, resource_id)
        if not req_id:
            _write_asr_status(conn, item_id, asr_status="failed_asr",
                              asr_failed_reason=f"submit: {err}")
            if own_conn and conn is not None: conn.close()
            return AsrResult("failed_asr", None, duration_sec, None, f"submit: {err}")

        # 5. 同步轮询
        wait = max_wait_sec or 900
        body, asr_sec, err_code = doubao_poll_until_done_sync(
            req_id, api_key, resource_id, max_wait_sec=wait,
        )
        if not body:
            _write_asr_status(conn, item_id, asr_status="failed_asr",
                              asr_failed_reason=f"poll: {err_code}")
            if own_conn and conn is not None: conn.close()
            return AsrResult("failed_asr", None, duration_sec, None, f"poll: {err_code}")

        result = body.get("result") or {}
        transcript = result.get("text") or ""
        utterances = result.get("utterances") or []
        if not transcript and utterances:
            transcript = "\n".join(u.get("text", "") for u in utterances if u.get("text"))
        segments = _utterances_to_segments(utterances)
        cost_yuan = round(duration_sec * DOUBAO_PRICE_PER_SEC, 4)

        if len(transcript) < 20:
            _write_asr_status(conn, item_id, asr_status="failed_empty",
                              asr_duration_sec=duration_sec, asr_cost_yuan=cost_yuan,
                              asr_failed_reason="transcript too short")
            # 配额仍然计入(已实际调用豆包)
            try:
                _consume_asr_quota(conn, duration_sec, user_id=user_id)
            except Exception as _e:
                print(f"[run_asr_inline] consume_asr_quota (failed_empty) non-fatal: {_e}", flush=True)
            if own_conn and conn is not None: conn.close()
            return AsrResult("failed_empty", None, duration_sec, cost_yuan, "too short")

        # 写 transcript + segments
        _update_asr_fields(
            conn,
            item_id,
            asr_text=transcript,
            asr_segments=segments,
            asr_duration_sec=duration_sec,
            asr_cost_yuan=cost_yuan,
            asr_provider="doubao-seedasr-bigmodel",
        )

        # 6. MiniMax 段级翻译(非致命)
        # BF-0419-15: 原文已是中文则跳过翻译(避免 MiniMax 中译中冗余)
        try:
            if _is_mostly_chinese(transcript):
                print(f"[run_asr_inline] skip translate for chinese item {item_id}", flush=True)
                cn_segments = None
            else:
                cn_segments = translate_segments_cn(segments) if segments else None
            if cn_segments and len(cn_segments) == len(segments):
                transcript_cn = "\n".join(s for s in cn_segments if s)
                _update_asr_fields(
                    conn,
                    item_id,
                    asr_text_cn=transcript_cn,
                    asr_segments_cn=cn_segments,
                )
        except Exception as _e:
            print(f"[run_asr_inline] translate non-fatal for {item_id}: {_e}", flush=True)

        _write_asr_status(conn, item_id, asr_status="success",
                          asr_failed_reason=None,
                          asr_duration_sec=duration_sec, asr_cost_yuan=cost_yuan)

        # 7. 配额扣减(bypass 路径也扣)
        try:
            _consume_asr_quota(conn, duration_sec, user_id=user_id)
        except Exception as _e:
            print(f"[run_asr_inline] consume_asr_quota non-fatal: {_e}", flush=True)

        return AsrResult("success", transcript, duration_sec, cost_yuan)

    finally:
        # 清理临时文件(memory: run_asr_inline MUST 释放 /tmp/{id}.mp3/mp4)
        for p in (mp4_tmp, mp3_tmp):
            if local_mp3 and p == mp3_tmp:
                continue  # caller 拥有 local_mp3 的生命周期,不清理
            try:
                if os.path.exists(p):
                    os.remove(p)
            except OSError:
                pass
        if own_conn and conn is not None:
            try: conn.close()
            except Exception: pass


# ---------- 异步版本(保留 HTTP/SSE 路径使用) ----------

async def transcribe_and_summarize(
    item_id: str,
    user_id: int,
    emit: Optional[EventEmitter] = None,
    skip_transcript: bool = False,
) -> AsrResult:
    """主流程: 按需 ASR + 摘要重跑.

    skip_transcript=True 时只重跑摘要(用于 failed_summary 态重试).
    emit 是 SSE 事件发布器, 可为 None.
    """
    async def _emit(event: str, payload: dict) -> None:
        if emit:
            await emit(event, payload)

    conn = None
    if not remote_db.app_state_to_remote():
        import db
        conn = db.get_conn()
    row = _fetch_asr_worker_item(conn, item_id)
    if not row:
        raise ValueError(f"item {item_id} not found")

    api_key = os.environ.get("DOUBAO_ASR_API_KEY")
    resource_id = os.environ.get("DOUBAO_ASR_RESOURCE_ID", DOUBAO_RESOURCE_ID_DEFAULT)
    if not api_key:
        raise RuntimeError("DOUBAO_ASR_API_KEY not set")

    # skip_transcript 路径: 跳过 ASR, 只基于已有 transcript 重跑摘要
    if skip_transcript:
        transcript = row["asr_text"]
        if not transcript:
            return AsrResult("failed_summary", None, None, None, "no existing transcript")
        _write_asr_status(conn, item_id, asr_status="running")
        try:
            duration = row["asr_duration_sec"] or 0
            summary = regenerate_summary_from_transcript(
                transcript, row["title"] or "", row["content"] or "",
                duration_min=duration / 60.0 if duration else 0.0,
            )
            if not _is_valid_summary(summary):
                _write_asr_status(conn, item_id, asr_status="failed_summary",
                                  asr_failed_reason="summary format invalid")
                return AsrResult("failed_summary", transcript, duration, None, "format invalid")
            _update_asr_fields(conn, item_id, ai_summary=summary)
            _write_asr_status(conn, item_id, asr_status="success", asr_failed_reason=None)
            await _emit("summary_updated", {"ai_summary": summary})
            await _emit("done", {"status": "success"})
            return AsrResult("success", transcript, duration, None)
        except Exception as e:
            _write_asr_status(conn, item_id, asr_status="failed_summary",
                              asr_failed_reason=str(e)[:200])
            await _emit("error", {"code": "summary_failed", "message": str(e)[:200]})
            return AsrResult("failed_summary", transcript, row["asr_duration_sec"], None, str(e)[:200])

    mp4_url = _extract_mp4_url(row["media_json"])
    if not mp4_url:
        _write_asr_status(conn, item_id, asr_status="failed_empty",
                          asr_failed_reason="no video in media_json")
        return AsrResult("failed_empty", None, None, None, "no video")

    _write_asr_status(conn, item_id, asr_status="running")

    mp4_tmp = f"/tmp/asr_{item_id}.mp4"
    mp3_tmp = f"/tmp/asr_{item_id}.mp3"

    # 1. 下载 mp4
    await _emit("progress", {"phase": "download", "message": "下载视频中", "percent": 5})
    try:
        download_mp4(mp4_url, mp4_tmp)
    except Exception as e:
        _write_asr_status(conn, item_id, asr_status="failed_download",
                          asr_failed_reason=str(e)[:200])
        await _emit("error", {"code": "download_failed", "message": str(e)[:200]})
        return AsrResult("failed_download", None, None, None, str(e)[:200])

    # 2. ffmpeg 抽 mp3
    await _emit("progress", {"phase": "extract", "message": "提取音频中", "percent": 15})
    try:
        ffmpeg_extract_mp3(mp4_tmp, mp3_tmp)
        duration_sec = int(ffprobe_duration(mp3_tmp))
    except NoAudioStreamError as e:
        _write_asr_status(conn, item_id, asr_status="failed_empty",
                          asr_failed_reason=str(e))
        await _emit("error", {"code": "empty_transcript", "message": "视频无语音内容"})
        return AsrResult("failed_empty", None, None, None, str(e))
    except Exception as e:
        _write_asr_status(conn, item_id, asr_status="failed_extract",
                          asr_failed_reason=str(e)[:200])
        await _emit("error", {"code": "extract_failed", "message": str(e)[:200]})
        return AsrResult("failed_extract", None, None, None, str(e)[:200])

    # 3. 上传 OSS
    await _emit("progress", {"phase": "upload", "message": "上传音频中", "percent": 25})
    try:
        audio_url, _oss_key, _up_sec = upload_to_oss(mp3_tmp, item_id)
    except Exception as e:
        _write_asr_status(conn, item_id, asr_status="failed_upload",
                          asr_failed_reason=str(e)[:200])
        await _emit("error", {"code": "upload_failed", "message": str(e)[:200]})
        return AsrResult("failed_upload", None, duration_sec, None, str(e)[:200])

    # 4. 豆包 submit
    await _emit("progress", {"phase": "asr_submit", "message": "AI 识别中", "percent": 30})
    req_id, err = doubao_submit(audio_url, api_key, resource_id)
    if not req_id:
        _write_asr_status(conn, item_id, asr_status="failed_asr",
                          asr_failed_reason=f"submit: {err}")
        await _emit("error", {"code": "submit_failed", "message": str(err)[:200]})
        return AsrResult("failed_asr", None, duration_sec, None, f"submit: {err}")

    # 5. 轮询 query
    body, asr_sec, err_code = await doubao_poll_until_done(
        req_id, api_key, resource_id, emit_progress=_emit,
    )
    if not body:
        _write_asr_status(conn, item_id, asr_status="failed_asr",
                          asr_failed_reason=f"poll: {err_code}")
        await _emit("error", {"code": "poll_failed", "message": f"poll: {err_code}"})
        return AsrResult("failed_asr", None, duration_sec, None, f"poll: {err_code}")

    result = body.get("result") or {}
    transcript = result.get("text") or ""
    utterances = result.get("utterances") or []
    if not transcript and utterances:
        transcript = "\n".join(u.get("text", "") for u in utterances if u.get("text"))

    # v12.3: utterances → asr_segments JSON
    segments = _utterances_to_segments(utterances)

    cost_yuan = round(duration_sec * DOUBAO_PRICE_PER_SEC, 4)

    if len(transcript) < 20:
        _write_asr_status(conn, item_id, asr_status="failed_empty",
                          asr_duration_sec=duration_sec, asr_cost_yuan=cost_yuan,
                          asr_failed_reason="transcript too short")
        # BF-0419-10: failed_empty 态豆包已计费,配额必须计入
        try:
            _consume_asr_quota(conn, duration_sec, user_id=user_id)
        except Exception as _e:
            print(f"[transcribe_and_summarize] consume_asr_quota (failed_empty) non-fatal: {_e}", flush=True)
        await _emit("error", {"code": "empty_transcript",
                              "message": "视频无语音内容(可能是音乐/静音)"})
        return AsrResult("failed_empty", None, duration_sec, cost_yuan, "too short")

    # 写 transcript + segments
    _update_asr_fields(
        conn,
        item_id,
        asr_text=transcript,
        asr_segments=segments,
        asr_duration_sec=duration_sec,
        asr_cost_yuan=cost_yuan,
    )

    # BF-0419-10: 手动触发路径(本函数)也必须计入配额消耗(非致命)
    # 违反 PRD F52/R5.3:"手动触发计入但不拦,余额可负"
    try:
        _consume_asr_quota(conn, duration_sec, user_id=user_id)
    except Exception as _e:
        print(f"[transcribe_and_summarize] consume_asr_quota non-fatal: {_e}", flush=True)

    await _emit("transcript", {
        "text": transcript,
        "segments": segments,
        "duration_sec": duration_sec,
        "char_count": len(transcript),
    })

    # 6. MiniMax 重跑摘要
    await _emit("progress", {"phase": "summary", "message": "摘要更新中", "percent": 95})
    try:
        summary = regenerate_summary_from_transcript(
            transcript, row["title"] or "", row["content"] or "",
            duration_min=duration_sec / 60.0,
        )
        if not _is_valid_summary(summary):
            _write_asr_status(conn, item_id, asr_status="failed_summary",
                              asr_failed_reason="summary format invalid")
            await _emit("error", {"code": "summary_format_invalid", "message": "摘要格式异常"})
            return AsrResult("failed_summary", transcript, duration_sec, cost_yuan, "format invalid")
        _update_asr_fields(conn, item_id, ai_summary=summary)
        _write_asr_status(conn, item_id, asr_status="success", asr_failed_reason=None)
        await _emit("summary_updated", {"ai_summary": summary})

        # v12.3 方案 B: 逐段带标号翻译,前端 zh Tab 可复用 segments 时间戳渲染
        # 翻译失败非致命,保持 ASR success
        # BF-0419-15: 原文已是中文则跳过翻译(避免 MiniMax 中译中冗余)
        await _emit("progress", {"phase": "translate", "message": "翻译中", "percent": 98})
        try:
            if _is_mostly_chinese(transcript):
                print(f"[transcribe_and_summarize] skip translate for chinese item {item_id}", flush=True)
                cn_segments = None
            else:
                cn_segments = translate_segments_cn(segments) if segments else None
            if cn_segments and len(cn_segments) == len(segments):
                transcript_cn = "\n".join(s for s in cn_segments if s)
                _update_asr_fields(
                    conn,
                    item_id,
                    asr_text_cn=transcript_cn,
                    asr_segments_cn=cn_segments,
                )
                await _emit("transcript_cn", {
                    "text": transcript_cn,
                    "segments_cn": cn_segments,
                })
            else:
                # 无 segments(极罕见)或逐段翻译失败 → 回退整段翻译
                transcript_cn = translate_transcript_cn(transcript)
                if transcript_cn:
                    _update_asr_fields(conn, item_id, asr_text_cn=transcript_cn)
                    await _emit("transcript_cn", {"text": transcript_cn, "segments_cn": None})
        except Exception as e:
            print(f"[asr_worker] translate non-fatal: {e}", flush=True)

        await _emit("done", {"status": "success"})
        return AsrResult("success", transcript, duration_sec, cost_yuan)
    except Exception as e:
        _write_asr_status(conn, item_id, asr_status="failed_summary",
                          asr_failed_reason=str(e)[:200])
        await _emit("error", {"code": "summary_failed", "message": str(e)[:200]})
        return AsrResult("failed_summary", transcript, duration_sec, cost_yuan, str(e)[:200])
