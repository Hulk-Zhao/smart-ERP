"""连接器幂等落 PG（B-IDEM-INMEMORY-ONLY）：判定不依赖进程寿命。

用 ``ERP_TEST_DATABASE_URL`` 指向已 ``alembic upgrade head`` 的库。每条用例换新的
store/engine 实例来模拟「进程重启」，同时复用同一个 mock 外部系统，用 POST 次数
证明「没有第二次外部写入」。
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from apps.connectors.db.models import ConnectorIdempotency
from apps.connectors.errors import ConnectorError, ErrorKind
from apps.connectors.idempotency_pg import PostgresIdempotencyStore
from apps.connectors.u8.actions import PO_CREATE_DRAFT
from apps.connectors.u8.mock import MockU8Backend
from apps.connectors.u8.factory import U8Stack
from tests.connectors.support import make_backend, make_stack, po_payload, post_calls, run

_DSN = os.environ.get("ERP_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not _DSN, reason="未设置 ERP_TEST_DATABASE_URL，跳过连接器幂等 PG 用例"
)

_IDEM_KEY = "demo-demo-w2-write-9001"
_POST_PATH = "/u8/api/purchase-order-drafts"


def _engine() -> AsyncEngine:
    return create_async_engine(_DSN or "", poolclass=NullPool)


def _sessions(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


@asynccontextmanager
async def _clean() -> AsyncIterator[None]:
    """每条用例先清表：幂等表按「空库首跑」起步，旧数据兼容见单独用例。"""

    engine = _engine()
    try:
        async with engine.begin() as conn:
            await conn.execute(text("TRUNCATE erp.connector_idempotency"))
        yield
    finally:
        await engine.dispose()


def _stack(backend: MockU8Backend, engine: AsyncEngine) -> tuple[U8Stack, MockU8Backend]:
    _, stack = make_stack(
        backend,
        sleep=_no_sleep,
        idem=PostgresIdempotencyStore(_sessions(engine), connector="u8"),
    )
    return stack, backend


async def _row(engine: AsyncEngine, key: str) -> dict[str, Any] | None:
    sessions = _sessions(engine)
    async with sessions() as session:
        row = await session.get(ConnectorIdempotency, key)
        if row is None:
            return None
        return {
            "connector": row.connector,
            "action_key": row.action_key,
            "request_hash": row.request_hash,
            "status": row.status,
            "response": dict(row.response) if row.response is not None else None,
        }


async def _no_sleep(_seconds: float) -> None:
    return None


def test_replay_after_restart_comes_from_db_without_second_post() -> None:
    """换掉进程内存（新 store 实例）后同键同参仍 replay，且不再打外部系统。"""

    async def scenario() -> None:
        backend = make_backend()

        engine_first = _engine()
        first_stack, _ = _stack(backend, engine_first)
        first = await first_stack.connector.write(
            PO_CREATE_DRAFT, po_payload(), ticket_id="T-1", idem_key=_IDEM_KEY
        )
        assert first.external_id
        assert len(post_calls(backend)) == 1
        first_row = await _row(engine_first, _IDEM_KEY)
        await engine_first.dispose()

        assert first_row is not None
        assert first_row["status"] == "succeeded"
        assert first_row["connector"] == "u8"
        assert first_row["action_key"] == PO_CREATE_DRAFT.key
        assert str(first_row["request_hash"]).startswith("sha256:")
        assert first_row["response"]["external_id"] == first.external_id

        # 模拟重启：新 engine + 新 store；外部系统还是同一个（POST 次数累计）
        engine_second = _engine()
        second_stack, _ = _stack(backend, engine_second)
        second = await second_stack.connector.write(
            PO_CREATE_DRAFT, po_payload(), ticket_id="T-1", idem_key=_IDEM_KEY
        )
        await engine_second.dispose()

        assert second.external_id == first.external_id
        assert len(post_calls(backend)) == 1

    run(_with_clean(scenario()))


def test_same_key_different_payload_conflicts_and_does_not_post() -> None:
    """同键不同载荷必须显式冲突，不能静默返回旧结果。"""

    async def scenario() -> None:
        backend = make_backend()
        engine = _engine()
        stack, _ = _stack(backend, engine)
        try:
            await stack.connector.write(
                PO_CREATE_DRAFT, po_payload(), ticket_id="T-1", idem_key=_IDEM_KEY
            )
            assert len(post_calls(backend)) == 1

            with pytest.raises(ConnectorError) as caught:
                await stack.connector.write(
                    PO_CREATE_DRAFT,
                    po_payload(memo="换了载荷但复用同一个幂等键"),
                    ticket_id="T-1",
                    idem_key=_IDEM_KEY,
                )
            assert caught.value.kind is ErrorKind.IDEM_CONFLICT
            assert caught.value.retryable is False
            assert len(post_calls(backend)) == 1
        finally:
            await engine.dispose()

    run(_with_clean(scenario()))


def test_concurrent_same_key_executes_once() -> None:
    """两个进程同时抢同一个键：只有一方执行外部写入，另一方 fail-closed。"""

    async def scenario() -> None:
        backend = make_backend()
        engine_a, engine_b = _engine(), _engine()
        stack_a, _ = _stack(backend, engine_a)
        stack_b, _ = _stack(backend, engine_b)
        try:
            results = await asyncio.gather(
                stack_a.connector.write(
                    PO_CREATE_DRAFT, po_payload(), ticket_id="T-1", idem_key=_IDEM_KEY
                ),
                stack_b.connector.write(
                    PO_CREATE_DRAFT, po_payload(), ticket_id="T-2", idem_key=_IDEM_KEY
                ),
                return_exceptions=True,
            )
            ok = [item for item in results if not isinstance(item, BaseException)]
            conflicts = [
                item
                for item in results
                if isinstance(item, ConnectorError)
                and item.kind is ErrorKind.IDEM_CONFLICT
            ]
            assert len(ok) == 1
            assert len(conflicts) == 1
            assert len(post_calls(backend)) == 1
        finally:
            await engine_a.dispose()
            await engine_b.dispose()

    run(_with_clean(scenario()))


def test_empty_table_is_first_execution_and_failed_row_can_retry() -> None:
    """空表按首次执行；上次失败的行（无响应）允许同键同参重试。"""

    async def scenario() -> None:
        backend = make_backend()
        engine = _engine()
        stack, _ = _stack(backend, engine)
        try:
            assert await _row(engine, _IDEM_KEY) is None

            backend.inject_failure(_POST_PATH, 503, times=3)
            with pytest.raises(ConnectorError):
                await stack.connector.write(
                    PO_CREATE_DRAFT, po_payload(), ticket_id="T-1", idem_key=_IDEM_KEY
                )
            failed = await _row(engine, _IDEM_KEY)
            assert failed is not None
            assert failed["status"] == "failed"
            assert failed["response"] is None

            result = await stack.connector.write(
                PO_CREATE_DRAFT, po_payload(), ticket_id="T-1", idem_key=_IDEM_KEY
            )
            assert result.external_id
            succeeded = await _row(engine, _IDEM_KEY)
            assert succeeded is not None
            assert succeeded["status"] == "succeeded"
            assert succeeded["response"]["external_id"] == result.external_id
        finally:
            await engine.dispose()

    run(_with_clean(scenario()))


async def _with_clean(scenario: Any) -> None:
    async with _clean():
        await scenario
