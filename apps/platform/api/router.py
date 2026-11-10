"""业务路由统一挂载点。

新流只写业务 ``APIRouter``，然后在自己的模块顶层加一行：

```python
from apps.platform.api.router import api_router

api_router.include_router(orders_router)
```

业务模块再登记到 ``BUSINESS_ROUTER_MODULES``；``install_platform`` 会在挂载
``api_router`` 之前导入这些模块，把路由接进启动路径。新流不需要碰
``apps/main.py`` 里的中间件装配，也不需要自己拼请求 ID / trace_id / 超时。
"""

from __future__ import annotations

import importlib

from fastapi import APIRouter

api_router: APIRouter = APIRouter()

# 被导入时即向 api_router 注册业务路由的模块。
BUSINESS_ROUTER_MODULES: tuple[str, ...] = ("apps.api",)


def register_business_routers() -> None:
    """装配缝：导入即向 api_router 注册；幂等、与 import 顺序无关。

    必须是函数体内延迟导入：``apps.api`` 会 import 本模块，顶层导入会循环。
    importlib 自带模块缓存，重复调用不会重复注册。
    """

    for name in BUSINESS_ROUTER_MODULES:
        importlib.import_module(name)
