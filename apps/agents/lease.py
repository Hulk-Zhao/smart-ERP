"""执行租约：防双跑的第一道防线（§3.1.3）。

租约不是「先查后写」：``acquire`` 是一条带条件的 ``UPDATE``，靠 PG 行锁与
``WHERE lease_expires_at < now()`` 保证同一时刻只有一个 owner。
没有租约就不许执行；过期租约可被接管（at-least-once，靠幂等收敛）。
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from apps.platform.contracts.errors import ErrorCode

_ACQUIRE_SQL = text(
    """
    UPDATE erp.ticket
       SET lease_owner = :owner,
           lease_expires_at = now() + make_interval(secs => :ttl)
     WHERE id = :ticket_id
       AND (
            lease_owner IS NULL
            OR lease_expires_at IS NULL
            OR lease_expires_at < now()
            OR lease_owner = :owner
       )
    """
)

_RENEW_SQL = text(
    """
    UPDATE erp.ticket
       SET lease_expires_at = now() + make_interval(secs => :ttl)
     WHERE id = :ticket_id
       AND lease_owner = :owner
       AND lease_expires_at > now()
    """
)

_RELEASE_SQL = text(
    """
    UPDATE erp.ticket
       SET lease_owner = NULL,
           lease_expires_at = NULL
     WHERE id = :ticket_id
       AND lease_owner = :owner
    """
)

_READ_SQL = text(
    "SELECT lease_owner, lease_expires_at FROM erp.ticket WHERE id = :ticket_id"
)


class LeaseNotHeld(Exception):
    """未持租约却尝试执行（F7 并发冲突，不覆盖他人工作）。"""

    code = ErrorCode.CONFLICT_VERSION


class LeaseLost(LeaseNotHeld):
    """执行中途租约被接管（过期后另一 worker 接手）。"""


class LeaseManager:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sf = session_factory

    async def acquire(self, ticket_id: UUID, owner: str, *, ttl_seconds: int = 30) -> bool:
        async with self._sf() as session, session.begin():
            result = await session.execute(
                _ACQUIRE_SQL,
                {"ticket_id": ticket_id, "owner": owner, "ttl": float(ttl_seconds)},
            )
            return result.rowcount == 1

    async def renew(self, ticket_id: UUID, owner: str, *, ttl_seconds: int = 30) -> bool:
        async with self._sf() as session, session.begin():
            result = await session.execute(
                _RENEW_SQL,
                {"ticket_id": ticket_id, "owner": owner, "ttl": float(ttl_seconds)},
            )
            return result.rowcount == 1

    async def release(self, ticket_id: UUID, owner: str) -> bool:
        async with self._sf() as session, session.begin():
            result = await session.execute(
                _RELEASE_SQL, {"ticket_id": ticket_id, "owner": owner}
            )
            return result.rowcount == 1

    async def holder(self, ticket_id: UUID) -> tuple[str | None, datetime | None]:
        async with self._sf() as session:
            row = (
                await session.execute(_READ_SQL, {"ticket_id": ticket_id})
            ).mappings().one_or_none()
        if row is None:
            return None, None
        return row["lease_owner"], row["lease_expires_at"]

    async def assert_held(self, ticket_id: UUID, owner: str) -> None:
        holder, _ = await self.holder(ticket_id)
        if holder != owner:
            raise LeaseLost(f"工单 {ticket_id} 的租约已不属于 {owner!r}（当前 {holder!r}）")
