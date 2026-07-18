"""Reject oversized request bodies before route handlers buffer them.

稳定性加固(2026-07-10): 写端点普遍用 `await request.json()` 把整个 body 读进内存
再校验。没有全局 body 上限时,单个登录用户 POST 一个几百 MB 的 JSON body(或深嵌套
JSON 触发 RecursionError)就能把内存打爆——一次廉价请求,持久伤害。这里在路由解析
body 之前按 Content-Length 拦截超限请求。multipart/form-data(文件上传)另有各自的
流式处理,不在此拦截。上限可用 env 调。
"""
from __future__ import annotations

import os

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse


def _max_body_bytes() -> int:
    raw = (os.environ.get("INFO2ACTION_MAX_REQUEST_BODY_BYTES") or "").strip()
    if not raw:
        return 2 * 1024 * 1024  # 2MB: 任何合理 JSON 写都够;挡住 body 炸弹
    try:
        n = int(raw)
        return n if n > 0 else 2 * 1024 * 1024
    except (ValueError, TypeError):
        return 2 * 1024 * 1024


class MaxBodySizeMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, max_bytes: int | None = None):
        super().__init__(app)
        self.max_bytes = max_bytes if max_bytes is not None else _max_body_bytes()

    async def dispatch(self, request: Request, call_next):
        content_length = request.headers.get("content-length")
        if content_length:
            content_type = (request.headers.get("content-type") or "").lower()
            # 文件上传走流式处理,不适用 JSON body 上限。
            if not content_type.startswith("multipart/"):
                try:
                    declared = int(content_length)
                except (ValueError, TypeError):
                    declared = 0
                if declared > self.max_bytes:
                    return JSONResponse(
                        {"detail": "request body too large"},
                        status_code=413,
                    )
        return await call_next(request)
