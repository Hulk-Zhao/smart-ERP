"""日切锚点调度：把 :meth:`AuditLedger.anchor_day` 接到 ``core.runtime.scheduler``。

看板 B-ANCHOR 的验收口径（表里有真实锚点行、锚点之后追加仍判对、回拨不误报）在此落地：

* **频率**：调度每 tick 调用 :meth:`AuditAnchorService.step`，但按租户记住「今天已经
  写过」——成功后当天不再读链、不再写库；失败按 ``retry_seconds`` 退避，不在每个
  tick 上重试。租户列表取自 ``audit_image``，没有审计影像的租户不产生锚点
  （demo 档零噪声）。
* **幂等**：真正防重的是 ``erp.audit_anchor`` 主键 ``(tenant_id, anchor_date)`` 与
  ``AuditLedger.anchor_day`` 的「先查后写」；本模块的内存标记只是省掉重复读链。
* **并发**：多副本同时写同一天时，后提交者拿到 ``IntegrityError``；``anchor_day``
  重读已有锚点并按链前缀校验，一致即视为成功——**绝不覆盖已写锚点**（写重了比没写
  更糟，它会成为后续校验的基准）。
* **回拨**：``anchor_date`` 只是日标签。时钟回拨到已锚日之前时跳过并告警一次，
  不补写过去日的锚点；已有锚点按链前缀比较，不会因回拨抛 ``AuditChainError``。
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from uuid import UUID

from apps.audit.ledger import AuditLedger

__all__ = ["AnchorResult", "AuditAnchorService"]

logger = logging.getLogger("erp.audit.anchor")


def _utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class AnchorResult:
    """一次成功写入（或确认已存在）的锚点。"""

    tenant_id: UUID
    anchor_date: date
    record_count: int
    last_hash: str
    elapsed_ms: float


@dataclass
class AuditAnchorService:
    ledger: AuditLedger
    # 时钟只用来取「日标签」，不参与边界推导（边界在首次写入时固化为链前缀）。
    clock: Callable[[], datetime] = _utc_now
    monotonic: Callable[[], float] = time.monotonic
    retry_seconds: float = 60.0
    tenant_refresh_seconds: float = 600.0
    # 显式指定租户时不再自动发现（测试隔离 / 运维定点补写用）。
    tenants: list[UUID] | None = None

    _done: dict[UUID, date] = field(default_factory=dict)
    _tenants: list[UUID] = field(default_factory=list)
    _tenants_at: float | None = None
    _next_tenant: int = 0
    _retry_at: float | None = None
    _rollback_warned: set[UUID] = field(default_factory=set)

    def __post_init__(self) -> None:
        if self.retry_seconds < 0:
            raise ValueError("retry_seconds 不得为负")
        if self.tenant_refresh_seconds < 0:
            raise ValueError("tenant_refresh_seconds 不得为负")

    async def step(self) -> AnchorResult | None:
        """尝试给一个「今天还没写锚点」的租户写锚点；没有活可干返回 ``None``。

        异常一律向上抛（由调度侧记 stats/日志），但抛出前已设置退避时间，
        下一轮 tick 不会立刻再打一次同样的失败。
        """

        now = self.monotonic()
        if self._retry_at is not None and now < self._retry_at:
            return None
        day = self._today()
        tenants = await self._tenants_snapshot(now)
        for offset in range(len(tenants)):
            index = (self._next_tenant + offset) % len(tenants)
            tenant_id = tenants[index]
            anchored = self._done.get(tenant_id)
            if anchored is not None:
                if day == anchored:
                    continue
                if day < anchored:
                    # 时钟回拨跨日：不补写过去日的锚点（那会多出一行与既有锚点
                    # 重叠的记录），也不拿 created_at 重算前缀——只告警一次。
                    if tenant_id not in self._rollback_warned:
                        self._rollback_warned.add(tenant_id)
                        logger.warning(
                            "检测到进程时钟回拨：今日标签 %s 早于已锚日 %s，跳过锚点 tenant=%s",
                            day,
                            anchored,
                            tenant_id,
                        )
                    continue
            result = await self._anchor(tenant_id, day, now)
            self._next_tenant = (index + 1) % len(tenants)
            return result
        return None

    def _today(self) -> date:
        value = self.clock()
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.astimezone(UTC).date()

    async def _anchor(
        self, tenant_id: UUID, day: date, now: float
    ) -> AnchorResult:
        started = time.perf_counter()
        try:
            anchor = await self.ledger.anchor_day(tenant_id, day)
        except Exception:
            # 失败不留「半成功」标记：不记 _done，退避后重试。
            self._retry_at = now + self.retry_seconds
            raise
        self._retry_at = None
        self._done[tenant_id] = day
        result = AnchorResult(
            tenant_id=tenant_id,
            anchor_date=anchor.anchor_date,
            record_count=anchor.record_count,
            last_hash=anchor.last_hash,
            elapsed_ms=(time.perf_counter() - started) * 1000.0,
        )
        logger.info(
            "审计锚点就绪 tenant=%s date=%s record_count=%s tail=%s elapsed_ms=%.1f",
            tenant_id,
            anchor.anchor_date,
            anchor.record_count,
            anchor.last_hash[:12],
            result.elapsed_ms,
        )
        return result

    async def _tenants_snapshot(self, now: float) -> list[UUID]:
        if self.tenants is not None:
            return self.tenants
        stale = (
            self._tenants_at is None
            or now - self._tenants_at >= self.tenant_refresh_seconds
        )
        if stale:
            self._tenants = await self.ledger.tenant_ids()
            self._tenants_at = now
            self._next_tenant = 0
        return self._tenants
