"""平台层 API 装配入口（中间件 + 统一错误包装 + 业务路由挂载）。

新流接入方式：业务模块里 ``api_router.include_router(业务 router)`` 一行，
``apps/main.py`` 只需 import 该模块；中间件与异常处理由本模块统一装配。
"""

from __future__ import annotations

from fastapi import FastAPI

from apps.platform.api.errors import (
    ProblemError,
    install_error_handlers,
    problem_response,
)
from apps.platform.api.middleware import (
    DEFAULT_TIMEOUT_S,
    REQUEST_ID_HEADER,
    TRACE_ID_HEADER,
    install_middleware,
)
from apps.platform.api.router import api_router

__all__ = [
    "DEFAULT_TIMEOUT_S",
    "REQUEST_ID_HEADER",
    "TRACE_ID_HEADER",
    "ProblemError",
    "api_router",
    "install_platform",
    "problem_response",
]


def install_platform(app: FastAPI, *, timeout_s: float = DEFAULT_TIMEOUT_S) -> FastAPI:
    """装配请求上下文、超时、problem+json 错误处理，并挂载 ``api_router``。

    幂等：同一 app 重复调用不会重复加中间件或路由。
    """

    if getattr(app.state, "_erp_platform_installed", False):
        return app

    install_error_handlers(app)
    install_middleware(app, timeout_s=timeout_s)
    app.include_router(api_router)
    app.state._erp_platform_installed = True
    return app
