"""容器健康探针：``python -m tools.health worker|scheduler``。

compose 的 healthcheck 直接调它（默认 8011），只读数据库里的心跳/租约表：

- ``worker``：``erp.worker_heartbeat`` 近 ``--stale-seconds``（默认 300s）内有
  新鲜心跳即 200；顺带回队列深度，便于判断「进程活着但队列堆了」；
- ``scheduler``：``erp.scheduler_lease`` 当前 owner 心跳新鲜且 ``expires_at`` 未过期；
- 任何 DB 异常 → 503（探针不能把「查不到」当健康）。

一次性模式 ``--check`` 打印 JSON 并以 0/1 退出，供 CI / 手工排查使用。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import socket
import sys
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Sequence

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from apps.platform.config import get_settings

logger = logging.getLogger("erp.health")

DEFAULT_PORT = 8011
DEFAULT_STALE_SECONDS = 300.0
QUERY_TIMEOUT_SECONDS = 5.0

WORKER_SQL = """
    SELECT worker_id, state, heartbeat_at,
           extract(epoch FROM now() - heartbeat_at) AS age_seconds,
           jobs_done, jobs_failed
      FROM erp.worker_heartbeat
     WHERE heartbeat_at > now() - make_interval(secs => :stale_seconds)
       {prefix_filter}
     ORDER BY heartbeat_at DESC
     LIMIT 5
    """

QUEUE_DEPTH_SQL = """
    SELECT queue, count(*) AS queued
      FROM erp.job_queue
     WHERE state = 'queued'
       {queue_filter}
     GROUP BY queue
     ORDER BY queue
    """

SCHEDULER_SQL = text(
    """
    SELECT owner, expires_at, heartbeat_at,
           extract(epoch FROM now() - heartbeat_at) AS age_seconds,
           (expires_at IS NOT NULL AND expires_at > now()) AS lease_valid
      FROM erp.scheduler_lease
     WHERE scope = :scope
    """
)


@dataclass(frozen=True)
class HealthConfig:
    target: str
    database_url: str
    stale_seconds: float = DEFAULT_STALE_SECONDS
    worker_prefix: str | None = None
    queues: tuple[str, ...] | None = None
    scope: str = "scheduler"


@dataclass(frozen=True)
class HealthResult:
    ok: bool
    payload: dict[str, Any]


async def _check_worker(conn: Any, config: HealthConfig) -> HealthResult:
    worker_params: dict[str, Any] = {"stale_seconds": config.stale_seconds}
    prefix_filter = ""
    if config.worker_prefix:
        prefix_filter = "AND worker_id LIKE :prefix"
        worker_params["prefix"] = config.worker_prefix
    rows = (
        await conn.execute(
            text(WORKER_SQL.format(prefix_filter=prefix_filter)), worker_params
        )
    ).mappings().all()
    queue_params: dict[str, Any] = {}
    queue_filter = ""
    if config.queues:
        queue_filter = "AND queue = ANY(:queues)"
        queue_params["queues"] = list(config.queues)
    depth = (
        await conn.execute(
            text(QUEUE_DEPTH_SQL.format(queue_filter=queue_filter)), queue_params
        )
    ).mappings().all()
    workers = [
        {
            "worker_id": row["worker_id"],
            "state": row["state"],
            "age_seconds": round(float(row["age_seconds"]), 1),
            "jobs_done": int(row["jobs_done"]),
            "jobs_failed": int(row["jobs_failed"]),
        }
        for row in rows
    ]
    queued = {row["queue"]: int(row["queued"]) for row in depth}
    payload: dict[str, Any] = {
        "target": "worker",
        "alive_workers": workers,
        "queue_depth": queued,
    }
    if not workers:
        payload["reason"] = (
            f"{int(config.stale_seconds)}s 内没有 {config.worker_prefix or '*'} 的新鲜心跳"
        )
        return HealthResult(False, payload)
    return HealthResult(True, payload)


async def _check_scheduler(conn: Any, config: HealthConfig) -> HealthResult:
    row = (
        await conn.execute(SCHEDULER_SQL, {"scope": config.scope})
    ).mappings().one_or_none()
    if row is None:
        return HealthResult(
            False,
            {
                "target": "scheduler",
                "scope": config.scope,
                "reason": "租约行不存在（scheduler 从未启动）",
            },
        )
    lease = {
        "scope": config.scope,
        "owner": row["owner"],
        "age_seconds": None
        if row["age_seconds"] is None
        else round(float(row["age_seconds"]), 1),
        "lease_valid": bool(row["lease_valid"]),
        "expires_at": None if row["expires_at"] is None else str(row["expires_at"]),
    }
    if row["owner"] is None:
        at_least_lease_time = "owner 为空（已释放）"
    elif not row["lease_valid"]:
        at_least_lease_time = "租约已过期（持有者可能已崩溃，等待接管）"
    elif row["age_seconds"] is None or float(row["age_seconds"]) > config.stale_seconds:
        at_least_lease_time = f"心跳超过 {int(config.stale_seconds)}s 未更新"
    else:
        return HealthResult(True, {"target": "scheduler", "lease": lease})
    return HealthResult(
        False,
        {"target": "scheduler", "lease": lease, "reason": at_least_lease_time},
    )


async def _check(config: HealthConfig) -> HealthResult:
    engine = create_async_engine(config.database_url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            if config.target == "worker":
                return await asyncio.wait_for(
                    _check_worker(conn, config), timeout=QUERY_TIMEOUT_SECONDS
                )
            return await asyncio.wait_for(
                _check_scheduler(conn, config), timeout=QUERY_TIMEOUT_SECONDS
            )
    except Exception as exc:  # noqa: BLE001 - 探针失败必须是 503，不能抛出
        return HealthResult(
            False,
            {"target": config.target, "reason": f"数据库检查失败: {type(exc).__name__}: {exc}"},
        )
    finally:
        await engine.dispose()


async def check_once(config: HealthConfig) -> HealthResult:
    """异步一次性检查（API / 定时任务 / 测试可直接 await）。"""

    return await _check(config)


def run_check(config: HealthConfig) -> HealthResult:
    """同步一次性检查（CLI / 容器探针）；禁止在已运行的事件循环里调用。"""

    return asyncio.run(_check(config))


def _make_handler(config: HealthConfig) -> type[BaseHTTPRequestHandler]:
    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - http.server 约定
            path = self.path.split("?", 1)[0]
            if path not in ("/healthz", "/readyz", "/"):
                self.send_error(404, "not found")
                return
            result = run_check(config)
            body = json.dumps(
                {"ok": result.ok, **result.payload}, ensure_ascii=False
            ).encode()
            self.send_response(200 if result.ok else 503)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt: str, *args: Any) -> None:
            logger.debug("%s - %s", self.address_string(), fmt % args)

    return _Handler


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="ERP 常驻进程健康探针")
    parser.add_argument("target", choices=("worker", "scheduler"))
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--stale-seconds", type=float, default=DEFAULT_STALE_SECONDS)
    parser.add_argument(
        "--worker-id",
        default=None,
        help="只认这个 worker_id 的心跳；默认按容器 hostname 前缀过滤",
    )
    parser.add_argument(
        "--queues", default=None, help="worker 队列过滤，逗号分隔，例如 agent,io"
    )
    parser.add_argument("--scope", default="scheduler")
    parser.add_argument(
        "--check", action="store_true", help="只检查一次，打印 JSON 并以 0/1 退出"
    )
    return parser.parse_args(argv)


def build_config(args: argparse.Namespace) -> HealthConfig:
    if args.worker_id:
        prefix = args.worker_id
    else:
        prefix = f"{socket.gethostname()}:%"
    queues = tuple(
        item.strip() for item in (args.queues or "").split(",") if item.strip()
    )
    return HealthConfig(
        target=args.target,
        database_url=get_settings().database_url,
        stale_seconds=args.stale_seconds,
        worker_prefix=prefix if args.target == "worker" else None,
        queues=queues or None,
        scope=args.scope,
    )


def main(argv: Sequence[str] | None = None) -> None:
    logging.basicConfig(
        level=os.environ.get("ERP_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    args = _parse_args(argv)
    config = build_config(args)
    if args.check:
        result = run_check(config)
        print(json.dumps({"ok": result.ok, **result.payload}, ensure_ascii=False))
        sys.exit(0 if result.ok else 1)
    server = ThreadingHTTPServer(("0.0.0.0", args.port), _make_handler(config))
    logger.info(
        "health 探针监听 :%s target=%s stale=%ss",
        args.port,
        config.target,
        int(config.stale_seconds),
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:  # pragma: no cover - 容器 SIGTERM 走这里
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
