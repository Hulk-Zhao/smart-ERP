"""业务路由统一挂载点。

新流只写业务 ``APIRouter``，然后在自己的模块顶层加一行：

```python
from apps.platform.api.router import api_router

api_router.include_router(orders_router)
```

``apps.main`` 只要 import 该业务模块，``install_platform`` 就会把 ``api_router`` 一次性
挂到应用上；新流不需要碰 ``apps/main.py`` 里的中间件装配，也不需要自己拼请求 ID /
trace_id / 超时。
"""

from __future__ import annotations

from fastapi import APIRouter

api_router: APIRouter = APIRouter()
