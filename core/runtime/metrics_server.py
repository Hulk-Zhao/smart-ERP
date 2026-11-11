"""调度进程的 ``/metrics`` 监听（独立内网端口，不复用业务端口）。

scheduler 是纯 asyncio 循环，没有 ASGI 应用；这里用最小的 FastAPI 壳挂
``observability.metrics.install_metrics``——端点实现、文本格式、指标白名单
全部复用现成的一套，不另写一份暴露逻辑。

只监听容器内网（compose 只 ``expose`` 不 ``ports``）：``/metrics`` 不允许
随业务端口对外发布，与 README §6.0 的约束一致。
"""

from __future__ import annotations

import asyncio
import logging

import uvicorn
from fastapi import FastAPI

from observability.metrics import MetricsRegistry, install_metrics

logger = logging.getLogger("erp.scheduler.metrics")

DEFAULT_METRICS_HOST = "0.0.0.0"
DEFAULT_METRICS_PORT = 8011
#: 端口已启动的判定轮询上限（秒）；绑定失败由任务异常抛出，不做无限等待。
_START_TIMEOUT_SECONDS = 5.0


class MetricsServer:
    def __init__(
        self,
        registry: MetricsRegistry,
        *,
        host: str = DEFAULT_METRICS_HOST,
        port: int = DEFAULT_METRICS_PORT,
    ) -> None:
        self._registry = registry
        self._host = host
        self._port = port
        self._server: uvicorn.Server | None = None
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        app = FastAPI(
            title="smart-erp scheduler metrics",
            docs_url=None,
            redoc_url=None,
            openapi_url=None,
        )
        installed = install_metrics(app, self._registry, enabled=True)
        if not installed:  # pragma: no cover - enabled=True 必定挂载
            raise RuntimeError("scheduler metrics 端点未挂载")

        # log_config=None：不要用 uvicorn 的日志配置覆盖 scheduler 既有 handler
        config = uvicorn.Config(
            app,
            host=self._host,
            port=self._port,
            log_level="warning",
            access_log=False,
            lifespan="off",
            log_config=None,
        )
        server = uvicorn.Server(config)
        # scheduler 自己处理 SIGTERM/SIGINT；uvicorn 再装一套会抢走信号
        server.install_signal_handlers = lambda: None  # type: ignore[method-assign]
        self._server = server
        self._task = asyncio.create_task(server.serve())

        deadline = asyncio.get_running_loop().time() + _START_TIMEOUT_SECONDS
        while not server.started:
            if self._task.done():
                # 端口占用等绑定错误在这里显形，避免「进程在跑但 /metrics 永远 404」
                self._task.result()
                raise RuntimeError("scheduler metrics 监听启动失败")
            if asyncio.get_running_loop().time() >= deadline:
                raise RuntimeError(
                    f"scheduler metrics 监听 {self._host}:{self._port} 启动超时"
                )
            await asyncio.sleep(0.05)
        logger.info("scheduler /metrics 已监听 %s:%s", self._host, self._port)

    async def stop(self) -> None:
        if self._server is not None:
            self._server.should_exit = True
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=_START_TIMEOUT_SECONDS)
            except TimeoutError:  # pragma: no cover - 退出兜底
                self._task.cancel()
            except Exception:  # pragma: no cover - 已记过启动异常
                logger.warning("scheduler metrics 停止时异常", exc_info=True)
        self._server = None
        self._task = None
