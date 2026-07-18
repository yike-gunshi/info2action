"""v12.2 F50 - Twitter 视频帖按需 ASR 端点.

路由:
  POST /api/items/{item_id}/asr          - 触发 ASR 任务 (Semaphore(3) per user)
  GET  /api/items/{item_id}/asr          - 查询 ASR 状态 (含僵尸任务检测)
  GET  /api/items/{item_id}/asr/stream   - SSE 进度推送 (Connection: close)
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from time_utils import parse_datetime

from starlette.concurrency import run_in_threadpool
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse

import db
import asr_worker
import remote_db
from authz import can_access_all

router = APIRouter()

ASR_CONCURRENT_LIMIT = 3
ZOMBIE_THRESHOLD_MIN = 30

# 保留后台 ASR 任务的强引用,避免 asyncio.create_task 返回的 task 被 GC 中途回收
# (CPython 文档明示的 footgun);任务完成后由 done_callback 自动移除。
_ASR_BG_TASKS: set[asyncio.Task] = set()


def _get_user_id(request: Request) -> Optional[int]:
    user = getattr(request.state, "user", None)
    return user["id"] if user else None


# 稳定性加固(2026-07-10): 这两个 app.state dict 原来只增不删——每个曾触发/订阅
# ASR 的 user_id / item_id 各留一个 Semaphore / Queue 常驻进程,C 端放量后随用户与
# item 基数单调增长,持续侵蚀 1GB 小机(有 OOM 前科)的余量。改为有上限的 LRU:
# 插入新 key 时若超过上限就淘汰最旧的一个(dict 保持插入序);事件总线在生产者结束
# 时另有主动回收(见 trigger_asr._run 的 finally)。上限可用 env 调。
def _asr_state_cap(name: str, default: int) -> int:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        n = int(raw)
        return n if n > 0 else default
    except (ValueError, TypeError):
        return default


_USER_SEM_CAP = _asr_state_cap("INFO2ACTION_ASR_USER_SEM_CAP", 512)
_EVENT_BUS_CAP = _asr_state_cap("INFO2ACTION_ASR_EVENT_BUS_CAP", 256)


def _evict_oldest_if_full(store: dict, cap: int) -> None:
    while len(store) >= cap:
        try:
            oldest = next(iter(store))
        except StopIteration:
            return
        store.pop(oldest, None)


def _get_or_create_user_sem(request: Request, user_id: int) -> asyncio.Semaphore:
    """Lazy 创建 user 级 Semaphore(3),存在 app.state 中(有上限 LRU)."""
    sems: dict[int, asyncio.Semaphore] = request.app.state.user_asr_sems
    existing = sems.get(user_id)
    if existing is not None:
        return existing
    _evict_oldest_if_full(sems, _USER_SEM_CAP)
    sems[user_id] = asyncio.Semaphore(ASR_CONCURRENT_LIMIT)
    return sems[user_id]


def _get_or_create_event_bus(request: Request, item_id: str) -> asyncio.Queue:
    buses: dict[str, asyncio.Queue] = request.app.state.asr_event_buses
    existing = buses.get(item_id)
    if existing is not None:
        return existing
    _evict_oldest_if_full(buses, _EVENT_BUS_CAP)
    buses[item_id] = asyncio.Queue(maxsize=200)
    return buses[item_id]


def _discard_event_bus(request: Request, item_id: str) -> None:
    """生产者结束后主动回收事件总线(消费者持有本地引用,不受字典删除影响)."""
    try:
        request.app.state.asr_event_buses.pop(item_id, None)
    except Exception:  # noqa: BLE001
        pass


async def _fetch_item_asr_state_async(conn, item_id: str) -> Optional[dict]:
    """BE-1: remote 模式远程往返离开事件循环;本地 sqlite 连接线程绑定,原线程执行(微秒级)。"""
    if conn is None:
        return await run_in_threadpool(_fetch_item_asr_state, None, item_id)
    return _fetch_item_asr_state(conn, item_id)


def _fetch_item_asr_state(conn, item_id: str) -> Optional[dict]:
    if remote_db.app_state_to_remote():
        return remote_db.get_item_asr_state_remote(item_id)
    row = conn.execute("""
        SELECT id, user_id, platform, asr_text, asr_status, asr_duration_sec, asr_cost_yuan,
               asr_attempted_at, asr_failed_reason, asr_provider, ai_summary,
               asr_segments, asr_text_cn, asr_segments_cn
        FROM items WHERE id = ?
    """, (item_id,)).fetchone()
    if not row:
        return None
    state = dict(row)
    # v12.3: segments JSON → list 解析,前端直接用
    for col in ("asr_segments", "asr_segments_cn"):
        raw = state.get(col)
        if raw and isinstance(raw, str):
            try:
                state[col] = json.loads(raw)
            except (ValueError, TypeError):
                state[col] = None
    return state


def _can_access_item_asr(request: Request, state: dict) -> bool:
    if state.get("platform") != "manual":
        return True
    if can_access_all(request):
        return True
    user_id = _get_user_id(request)
    return bool(user_id and str(state.get("user_id")) == str(user_id))


def _zombie_check(conn, item_id: str, state: dict) -> dict:
    """僵尸任务检测: asr_status='running' 且 attempted_at > 30min 前 -> 降级 failed_asr."""
    if state.get("asr_status") != "running":
        return state
    attempted = state.get("asr_attempted_at")
    if not attempted:
        return state
    # BF-0705-4: 统一走 parse_datetime(返回 aware UTC;兼容 Z 后缀——py3.10 的
    # fromisoformat 不认 Z,生产上曾因此静默失效;legacy naive 按项目约定 +8 解释)。
    t = parse_datetime(attempted)
    if t is None:
        return state
    if datetime.now(timezone.utc) - t < timedelta(minutes=ZOMBIE_THRESHOLD_MIN):
        return state
    if remote_db.app_state_to_remote():
        remote_db.update_item_asr_fields_remote(
            item_id,
            asr_status="failed_asr",
            asr_failed_reason="worker_timeout",
        )
        state["asr_status"] = "failed_asr"
        state["asr_failed_reason"] = "worker_timeout"
        return state
    conn.execute("""
        UPDATE items SET asr_status = 'failed_asr', asr_failed_reason = 'worker_timeout'
        WHERE id = ?
    """, (item_id,))
    conn.commit()
    state["asr_status"] = "failed_asr"
    state["asr_failed_reason"] = "worker_timeout"
    return state


def _write_route_asr_status(conn, item_id: str, **fields: Any) -> None:
    """Write ASR status from the route layer.

    `asr_worker._write_asr_status` owns local/remote field serialization. The
    route sometimes has no local connection (for background crash handling), so
    open one only for that narrow local fallback.
    """
    if remote_db.app_state_to_remote():
        asr_worker._write_asr_status(None, item_id, **fields)
        return
    owned_conn = conn is None
    local_conn = conn or db.get_conn()
    try:
        asr_worker._write_asr_status(local_conn, item_id, **fields)
    finally:
        if owned_conn:
            local_conn.close()


# ── POST /api/items/{item_id}/asr ─────────────────────────────

@router.post("/api/items/{item_id}/asr")
async def trigger_asr(request: Request, item_id: str,
                      skip_transcript: bool = Query(False)):
    """触发 ASR 任务. 需登录. Semaphore(3) per user 限流."""
    user_id = _get_user_id(request)
    if not user_id:
        raise HTTPException(status_code=401, detail="login required")

    conn = None if remote_db.app_state_to_remote() else db.get_conn()
    try:
        # BF-0705-1: remote 模式远程往返外移线程,不阻塞事件循环(对齐 GET 的 BE-1)
        state = await _fetch_item_asr_state_async(conn, item_id)
        if not state or not _can_access_item_asr(request, state):
            raise HTTPException(status_code=404, detail="item not found")

        if conn is None:
            state = await run_in_threadpool(_zombie_check, None, item_id, state)
        else:
            state = _zombie_check(conn, item_id, state)

        # 缓存命中: success 且非 skip_transcript 直接返回
        if state["asr_status"] == "success" and not skip_transcript:
            return {
                "task_id": None,
                "status": "success",
                "asr_text": state["asr_text"],
                "asr_segments": state.get("asr_segments"),
                "asr_text_cn": state.get("asr_text_cn"),
                "asr_segments_cn": state.get("asr_segments_cn"),
                "asr_cost_yuan": state.get("asr_cost_yuan"),
                "ai_summary": state["ai_summary"],
                "asr_duration_sec": state["asr_duration_sec"],
            }

        # 正在跑: 直接返回现有状态(前端应切 SSE)
        if state["asr_status"] == "running":
            return {"task_id": item_id, "status": "running"}

        # 并发限制
        sem = _get_or_create_user_sem(request, user_id)
        if sem.locked() and sem._value <= 0:
            raise HTTPException(
                status_code=429,
                detail={
                    "error": "concurrent_limit_exceeded",
                    "current": ASR_CONCURRENT_LIMIT - sem._value,
                    "limit": ASR_CONCURRENT_LIMIT,
                },
            )

        # 持久化 running 要早于 worker 内部下载/抽音频阶段:
        # 用户关闭弹窗再打开、刷新页面或换端访问时,都应该看到"转写中"而不是重新可点。
        if conn is None:
            await run_in_threadpool(
                _write_route_asr_status, None, item_id,
                asr_status="running", asr_failed_reason=None,
            )
        else:
            _write_route_asr_status(
                conn,
                item_id,
                asr_status="running",
                asr_failed_reason=None,
            )
    finally:
        if conn is not None:
            conn.close()

    # 启动异步任务
    bus = _get_or_create_event_bus(request, item_id)

    async def _emit(event: str, payload: dict) -> None:
        try:
            await bus.put({"event": event, "data": payload})
        except asyncio.QueueFull:
            pass  # 丢进度事件不致命

    async def _run():
        async with sem:
            try:
                await asr_worker.transcribe_and_summarize(
                    item_id, user_id, emit=_emit, skip_transcript=skip_transcript,
                )
            except Exception as e:  # noqa: BLE001
                message = str(e)[:200]
                try:
                    # conn=None: 本地模式在线程内自建连接,remote 模式为 HTTP 往返,均可外移
                    await run_in_threadpool(
                        _write_route_asr_status, None, item_id,
                        asr_status="failed_asr",
                        asr_failed_reason=f"worker_crash: {message}",
                    )
                except Exception:
                    pass
                await _emit("error", {"code": "worker_crash", "message": message})
            finally:
                await _emit("__done__", {})  # 哨兵: 结束 SSE 流
                # 生产者结束,主动回收事件总线(消费者已持有本地引用,pop 不影响其读取;
                # 迟到的消费者会在 stream 入口从 DB state 直接拿到结果,不依赖总线)。
                _discard_event_bus(request, item_id)

    # 保留 task 引用避免被 GC 中途回收(CPython footgun);完成后自动移除。
    # 仅对真实 asyncio.Task 生效(生产路径);测试里 create_task 可能被 monkeypatch
    # 成非 Task 的桩对象(不可 hash、无 add_done_callback),此时跳过保留即可。
    task = asyncio.create_task(_run())
    if isinstance(task, asyncio.Task):
        _ASR_BG_TASKS.add(task)
        task.add_done_callback(_ASR_BG_TASKS.discard)
    return {"task_id": item_id, "status": "running"}


# ── POST /api/items/{item_id}/asr/translate (v12.3) ───────────

@router.post("/api/items/{item_id}/asr/translate")
async def retry_translate(request: Request, item_id: str):
    """v12.3 E4: 独立重跑 transcript 中文翻译(不触发 ASR)."""
    user_id = _get_user_id(request)
    if not user_id:
        raise HTTPException(status_code=401, detail="login required")

    conn = None if remote_db.app_state_to_remote() else db.get_conn()
    state = await _fetch_item_asr_state_async(conn, item_id)  # BF-0705-1
    if not state or not _can_access_item_asr(request, state):
        raise HTTPException(status_code=404, detail="item not found")
    if not state.get("asr_text"):
        raise HTTPException(status_code=400, detail="no transcript to translate")

    segments = state.get("asr_segments")
    # MiniMax 长文本翻译可能 10s+,用 to_thread 避阻塞 event loop
    if segments:
        cn_segments = await asyncio.to_thread(asr_worker.translate_segments_cn, segments)
        if cn_segments and len(cn_segments) == len(segments):
            text_cn = "\n".join(s for s in cn_segments if s)
            if remote_db.app_state_to_remote():
                await run_in_threadpool(
                    remote_db.update_item_asr_fields_remote,
                    item_id,
                    asr_text_cn=text_cn,
                    asr_segments_cn=cn_segments,
                )
            else:
                conn.execute(
                    "UPDATE items SET asr_text_cn = ?, asr_segments_cn = ? WHERE id = ?",
                    (text_cn, json.dumps(cn_segments, ensure_ascii=False), item_id),
                )
                conn.commit()
            return {"asr_text_cn": text_cn, "asr_segments_cn": cn_segments}

    # 无 segments 或逐段翻译失败 → fallback 整段
    text_cn = await asyncio.to_thread(asr_worker.translate_transcript_cn, state["asr_text"])
    if not text_cn:
        raise HTTPException(status_code=502, detail="translation failed")
    if remote_db.app_state_to_remote():
        await run_in_threadpool(remote_db.update_item_asr_fields_remote,
                                item_id, asr_text_cn=text_cn)
    else:
        conn.execute("UPDATE items SET asr_text_cn = ? WHERE id = ?", (text_cn, item_id))
        conn.commit()
    return {"asr_text_cn": text_cn, "asr_segments_cn": None}


# ── GET /api/items/{item_id}/asr ──────────────────────────────

@router.get("/api/items/{item_id}/asr")
async def get_asr_status(request: Request, item_id: str):
    """查询当前 ASR 状态 (含僵尸任务检测,30min timeout 自动降级 failed_asr)."""
    conn = None if remote_db.app_state_to_remote() else db.get_conn()
    state = await _fetch_item_asr_state_async(conn, item_id)
    if not state or not _can_access_item_asr(request, state):
        raise HTTPException(status_code=404, detail="item not found")
    if conn is None:
        state = await run_in_threadpool(_zombie_check, None, item_id, state)  # BE-1
    else:
        state = _zombie_check(conn, item_id, state)
    return state


# ── GET /api/items/{item_id}/asr/stream (SSE) ─────────────────

@router.get("/api/items/{item_id}/asr/stream")
async def stream_asr(request: Request, item_id: str):
    """SSE 进度推送. 严格 Connection: close (项目硬规则)."""
    conn = None if remote_db.app_state_to_remote() else db.get_conn()
    try:
        state = await _fetch_item_asr_state_async(conn, item_id)  # BE-1
        if not state or not _can_access_item_asr(request, state):
            raise HTTPException(status_code=404, detail="item not found")
    finally:
        if conn is not None:
            conn.close()

    bus = _get_or_create_event_bus(request, item_id)

    async def event_generator():
        # 进场时先发一个 snapshot (便于重连后直接定位到当前状态)
        # v12.3 B3 fix: 回放所有已有数据事件,不只 success 态
        # (running + asr_text 有值 = ASR 完成,translate 还在跑,前端应直接显示 ready)
        conn = None if remote_db.app_state_to_remote() else db.get_conn()
        state = await _fetch_item_asr_state_async(conn, item_id)  # BE-1
        if state and state.get("asr_text"):
            transcript_payload = {
                "text": state["asr_text"],
                "segments": state.get("asr_segments"),
                "duration_sec": state.get("asr_duration_sec"),
                "char_count": len(state["asr_text"]),
                "cost_yuan": state.get("asr_cost_yuan"),
            }
            yield f"event: transcript\ndata: {json.dumps(transcript_payload, ensure_ascii=False)}\n\n"
            if state.get("ai_summary"):
                yield f"event: summary_updated\ndata: {json.dumps({'ai_summary': state['ai_summary']}, ensure_ascii=False)}\n\n"
            if state.get("asr_text_cn"):
                cn_payload = {
                    "text": state["asr_text_cn"],
                    "segments_cn": state.get("asr_segments_cn"),
                }
                yield f"event: transcript_cn\ndata: {json.dumps(cn_payload, ensure_ascii=False)}\n\n"
            if state.get("asr_status") == "success":
                yield f"event: done\ndata: {json.dumps({'status': 'success'})}\n\n"
                return
            # running 态:继续挂在 bus 等 translate / done 事件
        # 从 bus 流式推送
        while True:
            try:
                msg = await asyncio.wait_for(bus.get(), timeout=30.0)
            except asyncio.TimeoutError:
                # 空闲心跳, 防代理断连
                yield f": ping {int(time.time())}\n\n"
                continue
            if msg.get("event") == "__done__":
                return
            event = msg["event"]
            data = json.dumps(msg["data"], ensure_ascii=False)
            yield f"event: {event}\ndata: {data}\n\n"

    headers = {
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
        "Connection": "close",  # 项目硬规则 (memory: feedback_sse_connection_close)
    }
    return StreamingResponse(event_generator(), media_type="text/event-stream", headers=headers)
