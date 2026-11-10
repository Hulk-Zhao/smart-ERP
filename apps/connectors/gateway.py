"""connector-gw 入口：``python -m apps.connectors.gateway``。

C13 的约束：网关**只**暴露健康检查，不提供任何「执行/写」端点——写只能发生在
进程内的 ``WriteGateway`` 调用链里，网关不是写入通道。

- ``GET /healthz``：进程自检，不查依赖（不连 U8、不连 PG），永远只反映「我起来了」；
- ``GET /readyz``：检查下游连接器可用性（熔断状态 + 一次轻量探测），不可用返回 503。

端口从 ``ERP_`` 前缀环境变量读（``ERP_CONNECTOR_GW_PORT``，兼容 ``ERP_GATEWAY_PORT``），
默认 8012；启动失败必须打印原因并以非 0 退出，不允许静默挂起。
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

SERVICE_NAME = "smart-erp-connector-gw"
DEFAULT_PORT = 8012
PORT_ENV_KEYS = ("ERP_CONNECTOR_GW_PORT", "ERP_GATEWAY_PORT")
HOST_ENV_KEY = "ERP_CONNECTOR_GW_HOST"


@dataclass
class RegisteredConnector:
    alias: str
    connector: Any
    probe: Callable[[], Awaitable[bool]] | None = None
    probe_timeout_s: float = 3.0


class GatewayRegistry:
    """网关持有的连接器卡片。只读视图，不含任何写入方法。"""

    def __init__(self) -> None:
        self._items: dict[str, RegisteredConnector] = {}

    def register(
        self,
        alias: str,
        connector: Any,
        *,
        probe: Callable[[], Awaitable[bool]] | None = None,
        probe_timeout_s: float = 3.0,
    ) -> None:
        self._items[alias] = RegisteredConnector(
            alias=alias, connector=connector, probe=probe, probe_timeout_s=probe_timeout_s
        )

    @property
    def aliases(self) -> tuple[str, ...]:
        return tuple(sorted(self._items))

    @property
    def actions(self) -> tuple[Any, ...]:
        return tuple(
            action
            for item in self._items.values()
            for action in item.connector.describe().actions
        )

    @property
    def allowlist(self) -> tuple[str, ...]:
        return tuple(sorted(action.key for action in self.actions))

    @property
    def allowlist_hash(self) -> str:
        raw = json.dumps(list(self.allowlist), ensure_ascii=False).encode("utf-8")
        return "sha256:" + hashlib.sha256(raw).hexdigest()

    def describe(self) -> list[dict[str, Any]]:
        cards: list[dict[str, Any]] = []
        for alias in self.aliases:
            item = self._items[alias]
            connector = item.connector
            meta = connector.meta
            caps = connector.describe()
            breaker_state = getattr(getattr(connector, "breaker", None), "state", None)
            cards.append(
                {
                    "alias": alias,
                    "system": meta.system,
                    "version": meta.version,
                    "access_level": meta.access_level,
                    "security_level": meta.security_level,
                    "experimental": meta.experimental,
                    "version_fingerprint": meta.version_fingerprint,
                    "objects": list(caps.objects),
                    "actions": [action.key for action in caps.actions],
                    "breaker_state": getattr(breaker_state, "value", str(breaker_state)),
                }
            )
        return cards

    async def readiness(self) -> tuple[bool, dict[str, Any]]:
        if not self._items:
            return False, {"status": "not_ready", "reason": "no_connector_registered", "connectors": {}}

        details: dict[str, Any] = {}
        all_ok = True
        for alias in self.aliases:
            item = self._items[alias]
            try:
                health = await item.connector.health()
                ok = bool(health.ok)
                detail: dict[str, Any] = {
                    "ready": ok,
                    "failure_rate": health.failure_rate,
                    "last_sync_at": health.last_sync_at,
                }
                if ok and item.probe is not None:
                    try:
                        probed = await asyncio.wait_for(
                            item.probe(), timeout=item.probe_timeout_s
                        )
                        detail["downstream"] = bool(probed)
                        ok = ok and bool(probed)
                    except Exception as exc:  # noqa: BLE001 - 探测失败即不可用
                        detail["downstream"] = False
                        detail["probe_error"] = f"{type(exc).__name__}: {exc}"
                        ok = False
                detail["ready"] = ok
            except Exception as exc:  # noqa: BLE001 - 健康检查不允许抛
                ok = False
                detail = {"ready": False, "error": f"{type(exc).__name__}: {exc}"}
            all_ok = all_ok and ok
            details[alias] = detail

        return all_ok, {
            "status": "ready" if all_ok else "not_ready",
            "connectors": details,
        }


def build_default_registry() -> GatewayRegistry:
    """装配默认连接器。

    默认只有 U8 假实现；``ERP_FILE_INBOX`` 指向存在的目录时才挂文件通道
    （只读导入，不声明任何写动作）。任何装配失败都直接抛出，由 main 报错退出。
    """

    from pathlib import Path

    from apps.connectors.u8.factory import build_u8_stack

    stack = build_u8_stack()
    registry = GatewayRegistry()

    async def probe() -> bool:
        try:
            await stack.transport.get("suppliers", {"limit": 1})
            return True
        except Exception:  # noqa: BLE001 - readyz 只关心可用/不可用
            return False

    registry.register("u8", stack.connector, probe=probe)

    inbox = os.environ.get("ERP_FILE_INBOX")
    if inbox and Path(inbox).is_dir():
        from apps.connectors.file.factory import build_file_stack

        file_stack = build_file_stack(root=Path(inbox))

        async def file_probe(root: str = inbox) -> bool:
            path = Path(root)
            return path.is_dir() and os.access(path, os.R_OK)

        registry.register("file", file_stack.connector, probe=file_probe)
    return registry


def create_app(registry: GatewayRegistry | None = None) -> Any:
    """构造 ASGI app。缺 fastapi 时给出明确原因，而不是裸 ImportError。"""

    try:
        from fastapi import FastAPI
        from fastapi.responses import JSONResponse
    except ImportError as exc:  # pragma: no cover - 镜像里必装
        raise RuntimeError("connector-gw 需要 fastapi（见 pyproject 依赖）") from exc

    registry = registry or build_default_registry()
    app = FastAPI(
        title=SERVICE_NAME,
        version="1.0.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    @app.get("/healthz")
    async def healthz() -> dict[str, Any]:
        # 不查依赖：/healthz 只回答「网关进程活着吗」。
        return {
            "status": "ok",
            "service": SERVICE_NAME,
            "registered_actions": len(registry.actions),
            "actions_allowlist_hash": registry.allowlist_hash,
            "connectors": registry.describe(),
        }

    @app.get("/readyz")
    async def readyz() -> Any:
        ok, payload = await registry.readiness()
        return JSONResponse(payload, status_code=200 if ok else 503)

    return app


def port_from_env() -> int:
    for key in PORT_ENV_KEYS:
        raw = os.environ.get(key)
        if raw is None or raw.strip() == "":
            continue
        try:
            port = int(raw)
        except ValueError as exc:
            raise ValueError(f"{key}={raw!r} 不是合法端口") from exc
        if not 1 <= port <= 65535:
            raise ValueError(f"{key}={port} 超出 1..65535")
        return port
    return DEFAULT_PORT


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m apps.connectors.gateway")
    parser.add_argument("--host", default=os.environ.get(HOST_ENV_KEY, "0.0.0.0"))
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument(
        "--check",
        action="store_true",
        help="只做启动自检（装配 + 建 app）后退出，用于部署探针",
    )
    args = parser.parse_args(argv)

    try:
        port = args.port if args.port is not None else port_from_env()
        if not 1 <= port <= 65535:
            raise ValueError(f"端口 {port} 超出 1..65535")
    except ValueError as exc:
        print(f"connector-gw 启动失败: {exc}", file=sys.stderr)
        return 2

    try:
        registry = build_default_registry()
        app = create_app(registry)
    except Exception as exc:  # noqa: BLE001 - 启动失败必须可见
        print(
            f"connector-gw 启动失败: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return 2

    if args.check:
        print(
            f"connector-gw 自检通过: actions={len(registry.actions)} "
            f"connectors={registry.aliases} port={port}"
        )
        return 0

    try:
        import uvicorn
    except ImportError:
        print("connector-gw 启动失败: 缺少 uvicorn（见 pyproject 依赖）", file=sys.stderr)
        return 2

    try:
        uvicorn.run(app, host=args.host, port=port, log_level="info")
    except Exception as exc:  # noqa: BLE001 - 端口占用等
        print(f"connector-gw 启动失败: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
