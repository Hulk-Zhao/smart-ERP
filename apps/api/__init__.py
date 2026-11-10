"""产品 API 流（C16）：``/erp/api/v1`` 的工单、对账、审批、审计四组路由。

挂载方式遵循平台底座约定：本模块被导入时把 ``v1_router`` 注册进全局
``api_router``，由 ``apps.main.create_app()`` 的 ``install_platform`` 统一挂载；
本流不修改 ``apps/main.py``。
"""

from __future__ import annotations

from apps.api.container import (
    ApiContainer,
    SessionDirectory,
    configure_container,
    get_container,
    reset_container,
)
from apps.api.routes import v1_router
from apps.platform.api.router import api_router

api_router.include_router(v1_router)

__all__ = [
    "ApiContainer",
    "SessionDirectory",
    "configure_container",
    "get_container",
    "reset_container",
    "v1_router",
]
