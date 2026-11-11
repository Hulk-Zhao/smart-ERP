"""``config_change`` 哈希链的 PostgreSQL 落点与验链（C6 审计）。

哈希口径（冻结，与 ``audit_image`` 同源）：``sha256(prev_hash + canonical_json(除
hash 外全字段))``，直接复用 ``apps.platform.contracts.audit``；时间字段一律先归
一到 UTC，再按 ``YYYY-MM-DDTHH:MM:SS.ffffff+00:00`` 序列化，禁止依赖
``str(datetime)``（同一行数据写时与读回时的 tzinfo 可能不同）。

参与哈希的字段（冻结）：``id`` / ``tenant_id`` / ``kind`` / ``target`` /
``before`` / ``after`` / ``actor_id`` / ``approver_id`` / ``ticket_id`` /
``emergency`` / ``prev_hash`` / ``created_at``。
``chain_seq`` 只是存储序号，不参与哈希（与 ``audit_image`` 一致）。

只追加：本模块只发 INSERT / SELECT；表上的 append-only 触发器与 ``erp_app``
授权（0002 / 0012）会拒绝 UPDATE / DELETE。

同步/异步边界：``ConfigChangeService.apply`` 是同步 API，而 asyncpg 写入必须
``await``。生产调用方应使用 ``await service.apply_async(...)`` 并把本类作为
``recorder`` 注入；``apply()`` 遇到本类会明确报错，不会静默丢审计。
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from apps.platform.contracts.audit import canonical_json
from apps.security.config_access import ConfigAuditEntry
from apps.security.models import ConfigChangeRow

#: 并发追加冲突时的最大重试次数；超过即失败，不静默分叉。
MAX_APPEND_ATTEMPTS = 3


class ConfigChangeChainError(Exception):
    """``config_change`` 链追加或校验失败：按安全事件处理，不自动修复。"""


@dataclass(frozen=True, slots=True)
class ConfigChangeChainCheck:
    """一次整链校验的结果；``broken_at`` 是首个断裂行的 ``chain_seq``。"""

    tenant_id: UUID
    ok: bool
    checked: int
    broken_at: int | None = None
    detail: str = ""


def change_payload(
    *,
    change_id: UUID,
    tenant_id: UUID,
    kind: str,
    target: str,
    before: Mapping[str, Any],
    after: Mapping[str, Any],
    actor_id: UUID,
    approver_id: UUID | None,
    ticket_id: UUID | None,
    emergency: bool,
    prev_hash: str,
    created_at: datetime,
) -> dict[str, Any]:
    """构造参与哈希的载荷；字段集合一旦变更就是口径变更，必须同步文档。"""

    return {
        "id": change_id,
        "tenant_id": tenant_id,
        "kind": kind,
        "target": target,
        "before": dict(before),
        "after": dict(after),
        "actor_id": actor_id,
        "approver_id": approver_id,
        "ticket_id": ticket_id,
        "emergency": emergency,
        "prev_hash": prev_hash,
        "created_at": created_at,
    }


def compute_change_hash(prev_hash: str, payload: Mapping[str, Any]) -> str:
    """``payload`` 必须已排除 ``hash`` 字段本身。"""

    body = {key: value for key, value in payload.items() if key != "hash"}
    return hashlib.sha256((prev_hash + canonical_json(body)).encode("utf-8")).hexdigest()


def _row_payload(row: ConfigChangeRow) -> dict[str, Any]:
    return change_payload(
        change_id=row.id,
        tenant_id=row.tenant_id,
        kind=row.kind,
        target=row.target,
        before=row.before,
        after=row.after,
        actor_id=row.actor_id,
        approver_id=row.approver_id,
        ticket_id=row.ticket_id,
        emergency=row.emergency,
        prev_hash=row.prev_hash,
        created_at=row.created_at,
    )


class SqlConfigChangeRecorder:
    """``ConfigAuditRecorder`` 的 PG 实现（异步，只暴露 ``record_async``）。"""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def record_async(self, entry: ConfigAuditEntry) -> str:
        last_error: IntegrityError | None = None
        for _ in range(MAX_APPEND_ATTEMPTS):
            created_at = datetime.now(UTC)
            async with self._session_factory() as session:
                tail = (
                    await session.execute(
                        select(ConfigChangeRow.hash, ConfigChangeRow.chain_seq)
                        .where(ConfigChangeRow.tenant_id == entry.tenant_id)
                        .order_by(ConfigChangeRow.chain_seq.desc())
                        .limit(1)
                    )
                ).first()
                prev_hash = tail.hash if tail is not None else ""
                payload = change_payload(
                    change_id=entry.change_id,
                    tenant_id=entry.tenant_id,
                    kind=entry.kind,
                    target=entry.target,
                    before=entry.before,
                    after=entry.after,
                    actor_id=entry.actor_id,
                    approver_id=entry.approver_id,
                    ticket_id=entry.ticket_id,
                    emergency=entry.emergency,
                    prev_hash=prev_hash,
                    created_at=created_at,
                )
                digest = compute_change_hash(prev_hash, payload)
                session.add(
                    ConfigChangeRow(
                        id=entry.change_id,
                        tenant_id=entry.tenant_id,
                        kind=entry.kind,
                        target=entry.target,
                        before=dict(entry.before),
                        after=dict(entry.after),
                        actor_id=entry.actor_id,
                        approver_id=entry.approver_id,
                        ticket_id=entry.ticket_id,
                        emergency=entry.emergency,
                        prev_hash=prev_hash,
                        hash=digest,
                        created_at=created_at,
                    )
                )
                try:
                    await session.commit()
                except IntegrityError as exc:
                    # 并发追加读到同一链尾：唯一约束 (tenant_id, prev_hash) 让后
                    # 提交方失败，重读链尾后重试，而不是静默留下两条同父行。
                    await session.rollback()
                    last_error = exc
                    continue
                return digest
        raise ConfigChangeChainError(
            f"config_change 追加连续 {MAX_APPEND_ATTEMPTS} 次冲突，拒绝静默分叉"
        ) from last_error


async def verify_config_change_chain(
    session_factory: async_sessionmaker[AsyncSession], tenant_id: UUID
) -> ConfigChangeChainCheck:
    """按 ``chain_seq`` 重放整条链，返回首个断裂位置（不修复、不跳过）。"""

    async with session_factory() as session:
        rows = (
            (
                await session.execute(
                    select(ConfigChangeRow)
                    .where(ConfigChangeRow.tenant_id == tenant_id)
                    .order_by(ConfigChangeRow.chain_seq)
                )
            )
            .scalars()
            .all()
        )

    prev = ""
    for index, row in enumerate(rows):
        if row.prev_hash != prev:
            return ConfigChangeChainCheck(
                tenant_id=tenant_id,
                ok=False,
                checked=index,
                broken_at=row.chain_seq,
                detail=f"chain_seq={row.chain_seq} 的 prev_hash 与链尾不连续",
            )
        if compute_change_hash(prev, _row_payload(row)) != row.hash:
            return ConfigChangeChainCheck(
                tenant_id=tenant_id,
                ok=False,
                checked=index,
                broken_at=row.chain_seq,
                detail=f"chain_seq={row.chain_seq} 的内容与 hash 不一致（疑似篡改）",
            )
        prev = row.hash
    return ConfigChangeChainCheck(
        tenant_id=tenant_id, ok=True, checked=len(rows)
    )


__all__ = [
    "MAX_APPEND_ATTEMPTS",
    "ConfigChangeChainCheck",
    "ConfigChangeChainError",
    "SqlConfigChangeRecorder",
    "change_payload",
    "compute_change_hash",
    "verify_config_change_chain",
]
