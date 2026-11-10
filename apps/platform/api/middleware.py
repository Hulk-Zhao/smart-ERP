"""请求上下文与超时中间件。

- ``RequestContextMiddleware``：生成/透传 ``X-Request-ID`` 与 ``X-Trace-ID``，写入
  ``ContextVar`` 并把两者回写到响应头（错误体里的 ``trace_id`` 来自同一来源）。
- ``TimeoutMiddleware``：单请求超时兜底，超时返回 problem+json（不把长任务拖死进程）。
"""

from __future__ import annotations

import asyncio

from fastapi import FastAPI
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp

from apps.platform.api.context import (
    bind_trace_id,
    new_request_id,
    reset_trace_id,
)
from apps.platform.api.errors import problem_response
from apps.platform.contracts.errors import ErrorCode

REQUEST_ID_HEADER = "X-Request-ID"
TRACE_ID_HEADER = "X-Trace-ID"
DEFAULT_TIMEOUT_S = 30.0


class RequestContextMiddleware(BaseHTTPMiddleware):
    """请求 ID / trace_id 注入，响应头回写。"""

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        request_id = request.headers.get(REQUEST_ID_HEADER) or new_request_id()
        trace_id = request.headers.get(TRACE_ID_HEADER) or request_id

        token = bind_trace_id(trace_id)
        request.state.request_id = request_id
        request.state.trace_id = trace_id
        try:
            response = await call_next(request)
        finally:
            reset_trace_id(token)

        response.headers[REQUEST_ID_HEADER] = request_id
        response.headers[TRACE_ID_HEADER] = trace_id
        return response


class TimeoutMiddleware(BaseHTTPMiddleware):
    """超过预算直接返回 504 + problem+json。"""

    def __init__(self, app: ASGIApp, *, timeout_s: float = DEFAULT_TIMEOUT_S) -> None:
        super().__init__(app)
        self.timeout_s = timeout_s

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        try:
            return await asyncio.wait_for(call_next(request), timeout=self.timeout_s)
        except TimeoutError:
            return problem_response(
                ErrorCode.CONN_UNAVAILABLE,
                f"请求处理超过 {self.timeout_s:g} 秒被中止",
                status_code=504,
            )


def install_middleware(app: FastAPI, *, timeout_s: float = DEFAULT_TIMEOUT_S) -> None:
    """装配中间件；最后添加的最先执行，因此请求上下文包在超时外层。"""

    if getattr(app.state, "_erp_middleware_installed", False):
        return
    app.add_middleware(TimeoutMiddleware, timeout_s=timeout_s)
    app.add_middleware(RequestContextMiddleware)
    app.state._erp_middleware_installed = True
