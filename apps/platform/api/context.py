"""请求级 trace_id 的上下文存储。

中间件写入、错误处理器读取：两者不在同一个调用栈上（异常处理器由 Starlette
内部调用），用 ``ContextVar`` 传递比给每个路由塞参数更稳妥。
"""

from __future__ import annotations

import uuid
from contextvars import ContextVar, Token

_TRACE_ID: ContextVar[str] = ContextVar("erp_trace_id", default="")


def new_request_id() -> str:
    return uuid.uuid4().hex


def bind_trace_id(value: str) -> Token[str]:
    return _TRACE_ID.set(value)


def reset_trace_id(token: Token[str]) -> None:
    _TRACE_ID.reset(token)


def current_trace_id() -> str:
    return _TRACE_ID.get()
