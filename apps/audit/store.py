"""审计存储：只追加的存储抽象 + 内存实现 + PG 实现。

不可改由三处共同保证（与 §3.4.1 对应）：

1. 接口只有 ``append / list_images / chain / last_hash``，没有 update/delete；
2. ``append`` 校验 ``prev_hash`` 必须等于当前链尾，防并发追加分叉；
3. PG 侧由 ``MIGRATION_NEEDS.md`` 里的触发器与授权兜底（应用账号仅 INSERT/SELECT）。

内存实现只用于单测与本地走查；生产走 ``SqlAuditStore``。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Protocol
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from apps.audit.models import AuditAnchorRow, AuditImageRow
from apps.platform.contracts.audit import AuditImage


class AuditStoreError(Exception):
    """存储层拒绝（分叉追加、锚点不一致等）。"""


@dataclass(frozen=True, slots=True)
class AuditQuery:
    """台账检索条件。工单 / 任务（step）/ 时间范围是审计最常用的三个入口。"""

    tenant_id: UUID
    ticket_id: UUID | None = None
    step_id: UUID | None = None
    system: str | None = None
    object_id: str | None = None
    since: datetime | None = None
    until: datetime | None = None
    offset: int = 0
    limit: int = 100

    def __post_init__(self) -> None:
        if self.limit <= 0 or self.limit > 1000:
            raise ValueError("limit 必须在 1..1000 之间（分页与导出分离）")
        if self.offset < 0:
            raise ValueError("offset 不得为负")
        if self.since and self.until and self.since > self.until:
            raise ValueError("since 不得晚于 until")


@dataclass(frozen=True, slots=True)
class AuditAnchor:
    """每日锚点：截至 anchor_date 的链尾 hash 与记录数。"""

    tenant_id: UUID
    anchor_date: date
    last_hash: str
    record_count: int


class AuditStore(Protocol):
    async def append(self, image: AuditImage) -> None: ...

    async def list_images(self, query: AuditQuery) -> list[AuditImage]: ...

    async def chain(self, tenant_id: UUID) -> list[AuditImage]: ...

    async def last_hash(self, tenant_id: UUID) -> str: ...

    async def record_anchor(self, anchor: AuditAnchor) -> None: ...

    async def get_anchor(self, tenant_id: UUID, anchor_date: date) -> AuditAnchor | None: ...


class InMemoryAuditStore:
    """按租户单链的内存实现；只暴露追加与读取。"""

    def __init__(self) -> None:
        self._rows: dict[UUID, list[AuditImage]] = {}
        self._anchors: dict[tuple[UUID, date], AuditAnchor] = {}

    async def append(self, image: AuditImage) -> None:
        rows = self._rows.setdefault(image.tenant_id, [])
        expected = rows[-1].hash if rows else ""
        if image.prev_hash != expected:
            raise AuditStoreError(
                f"审计链分叉：prev_hash={image.prev_hash!r}，链尾={expected!r}"
            )
        rows.append(image)

    async def list_images(self, query: AuditQuery) -> list[AuditImage]:
        rows = self._rows.get(query.tenant_id, [])
        hits = [image for image in rows if self._matches(image, query)]
        return hits[query.offset : query.offset + query.limit]

    async def chain(self, tenant_id: UUID) -> list[AuditImage]:
        return list(self._rows.get(tenant_id, []))

    async def last_hash(self, tenant_id: UUID) -> str:
        rows = self._rows.get(tenant_id, [])
        return rows[-1].hash if rows else ""

    async def record_anchor(self, anchor: AuditAnchor) -> None:
        key = (anchor.tenant_id, anchor.anchor_date)
        existing = self._anchors.get(key)
        if existing is not None and existing != anchor:
            raise AuditStoreError(f"锚点 {anchor.anchor_date} 已存在且不一致，拒绝覆盖")
        self._anchors[key] = anchor

    async def get_anchor(self, tenant_id: UUID, anchor_date: date) -> AuditAnchor | None:
        return self._anchors.get((tenant_id, anchor_date))

    @staticmethod
    def _matches(image: AuditImage, query: AuditQuery) -> bool:
        if query.ticket_id is not None and image.ticket_id != query.ticket_id:
            return False
        if query.step_id is not None and image.step_id != query.step_id:
            return False
        if query.system is not None and image.system != query.system:
            return False
        if query.object_id is not None and image.object_id != query.object_id:
            return False
        if query.since is not None and image.created_at < query.since:
            return False
        if query.until is not None and image.created_at > query.until:
            return False
        return True


class SqlAuditStore:
    """PG 实现（``audit_image`` / ``audit_anchor``）。

    只发 INSERT / SELECT；UPDATE/DELETE 即使被写出也会被迁移里的触发器与账号授权拒绝。
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def append(self, image: AuditImage) -> None:
        row = AuditImageRow(
            id=image.id,
            tenant_id=image.tenant_id,
            ticket_id=image.ticket_id,
            step_id=image.step_id,
            system=image.system,
            object_type=image.object_type,
            object_id=image.object_id,
            before_ref=image.before_ref,
            after_ref=image.after_ref,
            field_diff=[entry.model_dump(mode="json") for entry in image.field_diff],
            diff_missing=image.diff_missing,
            prev_hash=image.prev_hash,
            hash=image.hash,
            created_at=image.created_at,
        )
        async with self._session_factory() as session:
            session.add(row)
            await session.commit()

    async def list_images(self, query: AuditQuery) -> list[AuditImage]:
        stmt = select(AuditImageRow).where(AuditImageRow.tenant_id == query.tenant_id)
        if query.ticket_id is not None:
            stmt = stmt.where(AuditImageRow.ticket_id == query.ticket_id)
        if query.step_id is not None:
            stmt = stmt.where(AuditImageRow.step_id == query.step_id)
        if query.system is not None:
            stmt = stmt.where(AuditImageRow.system == query.system)
        if query.object_id is not None:
            stmt = stmt.where(AuditImageRow.object_id == query.object_id)
        if query.since is not None:
            stmt = stmt.where(AuditImageRow.created_at >= query.since)
        if query.until is not None:
            stmt = stmt.where(AuditImageRow.created_at <= query.until)
        stmt = (
            stmt.order_by(AuditImageRow.chain_seq)
            .offset(query.offset)
            .limit(query.limit)
        )
        async with self._session_factory() as session:
            rows = (await session.execute(stmt)).scalars().all()
        return [_to_image(row) for row in rows]

    async def chain(self, tenant_id: UUID) -> list[AuditImage]:
        stmt = (
            select(AuditImageRow)
            .where(AuditImageRow.tenant_id == tenant_id)
            .order_by(AuditImageRow.chain_seq)
        )
        async with self._session_factory() as session:
            rows = (await session.execute(stmt)).scalars().all()
        return [_to_image(row) for row in rows]

    async def last_hash(self, tenant_id: UUID) -> str:
        stmt = (
            select(AuditImageRow.hash)
            .where(AuditImageRow.tenant_id == tenant_id)
            .order_by(AuditImageRow.chain_seq.desc())
            .limit(1)
        )
        async with self._session_factory() as session:
            value = (await session.execute(stmt)).scalar_one_or_none()
        return value or ""

    async def record_anchor(self, anchor: AuditAnchor) -> None:
        row = AuditAnchorRow(
            tenant_id=anchor.tenant_id,
            anchor_date=anchor.anchor_date,
            last_hash=anchor.last_hash,
            record_count=anchor.record_count,
        )
        async with self._session_factory() as session:
            session.add(row)
            await session.commit()

    async def get_anchor(self, tenant_id: UUID, anchor_date: date) -> AuditAnchor | None:
        stmt = select(AuditAnchorRow).where(
            AuditAnchorRow.tenant_id == tenant_id,
            AuditAnchorRow.anchor_date == anchor_date,
        )
        async with self._session_factory() as session:
            row = (await session.execute(stmt)).scalar_one_or_none()
        if row is None:
            return None
        return AuditAnchor(
            tenant_id=row.tenant_id,
            anchor_date=row.anchor_date,
            last_hash=row.last_hash,
            record_count=row.record_count,
        )


def _to_image(row: AuditImageRow) -> AuditImage:
    return AuditImage.model_validate(
        {
            "id": row.id,
            "tenant_id": row.tenant_id,
            "ticket_id": row.ticket_id,
            "step_id": row.step_id,
            "system": row.system,
            "object_type": row.object_type,
            "object_id": row.object_id,
            "before_ref": row.before_ref,
            "after_ref": row.after_ref,
            "field_diff": list(row.field_diff or []),
            "diff_missing": row.diff_missing,
            "prev_hash": row.prev_hash,
            "hash": row.hash,
            "created_at": row.created_at,
        }
    )


def _jsonable(value: Any) -> Any:
    """把快照/diff 值归一成 JSONB 可存的形态（与哈希口径的 ``default=str`` 一致）。"""

    from datetime import date as _date
    from decimal import Decimal

    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (datetime, _date)):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_jsonable(item) for item in value]
    return str(value)
