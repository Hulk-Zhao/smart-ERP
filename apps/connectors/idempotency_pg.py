"""连接器幂等的 PostgreSQL 实现（G-02 第二层键的真源）。

内存实现只在进程活着时有效：重灌数据库但不重启进程，同一 ``idem_key`` 会被判成
replay 而不发请求，外部系统被重置后查不到这笔写入（live 实测 F6 零写入失败）。
本实现把判定落到 ``erp.connector_idempotency``，重启后仍能从库里判定。

并发安全：主键 + 单条 ``INSERT ... ON CONFLICT DO NOTHING``，只有插入成功的一方
拿到 NEW；同键不同参、同键在途一律抛 ``E-IDEM-CONFLICT``（fail-closed）。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from sqlalchemy import func, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from apps.connectors.db.models import ConnectorIdempotency
from apps.connectors.errors import ConnectorError, ErrorKind
from apps.connectors.idempotency import IdemDecision, IdemStatus

# 记录保留期：供清理任务判断何时可删；判定本身不依赖它。
RECORD_TTL = "now() + interval '30 days'"


class PostgresIdempotencyStore:
    """按 ``idempotency_key`` 落库的连接器幂等存储；一个实例对应一个连接器。"""

    def __init__(
        self, session_factory: async_sessionmaker[AsyncSession], *, connector: str
    ) -> None:
        self._session_factory = session_factory
        self._connector = connector

    async def begin(
        self, key: str, fingerprint: str, *, action_key: str = ""
    ) -> IdemDecision:
        async with self._session_factory() as session, session.begin():
            claimed = await session.scalar(
                pg_insert(ConnectorIdempotency)
                .values(
                    idempotency_key=key,
                    connector=self._connector,
                    action_key=action_key or "unknown",
                    request_hash=fingerprint,
                    status="in_flight",
                    expires_at=text(RECORD_TTL),
                )
                .on_conflict_do_nothing(index_elements=["idempotency_key"])
                .returning(ConnectorIdempotency.idempotency_key)
            )
            if claimed is not None:
                return IdemDecision(IdemStatus.NEW)

            row = await session.get(ConnectorIdempotency, key)
            if row is None:  # 冲突行在别的隔离级别下不可见时也 fail-closed
                raise ConnectorError(
                    ErrorKind.IDEM_CONFLICT,
                    f"幂等键 {key} 已被占用但读不到记录，拒绝并发重复执行",
                )
            if row.request_hash != fingerprint:
                raise ConnectorError(
                    ErrorKind.IDEM_CONFLICT,
                    f"幂等键 {key} 已用于不同参数（G-02：同键不同参必须报障）",
                )
            if row.status == "in_flight":
                raise ConnectorError(
                    ErrorKind.IDEM_CONFLICT,
                    f"幂等键 {key} 的首次请求仍在途，拒绝并发重复执行",
                )
            if row.status == "succeeded" and row.response is not None:
                return IdemDecision(IdemStatus.REPLAY, dict(row.response))

            # 上次失败：同键同参允许重试，用条件更新抢占，抢不到说明被别人接管。
            taken = await session.scalar(
                update(ConnectorIdempotency)
                .where(
                    ConnectorIdempotency.idempotency_key == key,
                    ConnectorIdempotency.status == "failed",
                )
                .values(status="in_flight", response=None, updated_at=func.now())
                .returning(ConnectorIdempotency.idempotency_key)
            )
            if taken is None:
                raise ConnectorError(
                    ErrorKind.IDEM_CONFLICT,
                    f"幂等键 {key} 已被其他请求接管，拒绝并发重复执行",
                )
            return IdemDecision(IdemStatus.NEW)

    async def complete(self, key: str, response: Mapping[str, Any]) -> None:
        async with self._session_factory() as session, session.begin():
            updated = await session.scalar(
                update(ConnectorIdempotency)
                .where(
                    ConnectorIdempotency.idempotency_key == key,
                    ConnectorIdempotency.status == "in_flight",
                )
                .values(
                    status="succeeded", response=dict(response), updated_at=func.now()
                )
                .returning(ConnectorIdempotency.idempotency_key)
            )
            if updated is None:
                # 没有在途记录说明 begin 没成功或已被接管：宁可变红，也不落假成功。
                raise ConnectorError(
                    ErrorKind.IDEM_CONFLICT,
                    f"幂等键 {key} 没有在途记录，拒绝写成功结果（fail-closed）",
                )

    async def fail(self, key: str) -> None:
        async with self._session_factory() as session, session.begin():
            await session.execute(
                update(ConnectorIdempotency)
                .where(
                    ConnectorIdempotency.idempotency_key == key,
                    ConnectorIdempotency.status == "in_flight",
                )
                .values(status="failed", response=None, updated_at=func.now())
            )
