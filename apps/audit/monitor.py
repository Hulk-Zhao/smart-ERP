"""审计链周期巡检：把 ``AuditLedger.verify()`` 的结论接到 ``smart_erp_audit_chain_broken``。

设计要点（与 `docs/product/tech/ledger/S4.jsonl` seq 27 一致）：

* **一次只扫一段**：按租户轮转，每次最多 ``rows_per_run`` 行、每页 ``page_size`` 行，
  避免把整条链读进 Python（scheduler 容器只有 96MiB）。
* **水位线滚动续扫**：每个租户记 ``cursor``（存储层单调位置）与 ``prev_hash``，
  扫到链尾即从 0 重新开始——没有单独的「全量/增量」两套模式，等价于持续滚动的全量校验。
* **指标只在下结论时写**：断链立即写 1；只有把某个租户从 0 扫到链尾且未发现断裂，
  才允许写 0；本页校验途中不写任何值（避免把「才扫了一页」当成「全链没问题」）。
* **失败不写 0**：``step`` 内部不吞异常，调度侧记录失败并保持上次指标值——
  「不知道」不能被写成「没问题」。
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol
from uuid import UUID

from apps.audit.ledger import AuditLedger

__all__ = ["AuditChainMonitor", "ChainBrokenSink", "ScanResult"]


class ChainBrokenSink(Protocol):
    """只依赖这一个写入口，避免 apps/audit 反向依赖 observability 实现。"""

    def set_audit_chain_broken(self, broken: bool) -> None: ...


@dataclass(frozen=True, slots=True)
class ScanResult:
    tenant_id: UUID | None
    rows: int
    broken_at: int | None
    detail: str
    cycle_completed: bool
    elapsed_ms: float


@dataclass
class _TenantState:
    cursor: int = 0
    prev_hash: str = ""
    verified: int = 0
    # 断链是粘性的：只在本租户随后完整扫完一轮且未再断裂时才清除，
    # 否则下一次「本页正常」会把 P0 指标翻回 0。
    broken: bool = False


@dataclass
class AuditChainMonitor:
    ledger: AuditLedger
    sink: ChainBrokenSink
    rows_per_run: int = 2000
    page_size: int = 200
    interval_seconds: float = 60.0
    tenant_refresh_seconds: float = 600.0
    clock: Callable[[], float] = time.monotonic
    # 显式指定租户时不再自动发现（运维定点排查 / 测试隔离用）。
    tenants: list[UUID] | None = None

    _states: dict[UUID, _TenantState] = field(default_factory=dict)
    _broken: set[UUID] = field(default_factory=set)
    _tenants: list[UUID] = field(default_factory=list)
    _tenants_at: float | None = None
    _next_tenant: int = 0
    _last_run: float | None = None

    def __post_init__(self) -> None:
        if self.rows_per_run <= 0:
            raise ValueError("rows_per_run 必须 > 0")
        if self.page_size <= 0:
            raise ValueError("page_size 必须 > 0")

    @property
    def broken_tenants(self) -> frozenset[UUID]:
        return frozenset(self._broken)

    async def step(self) -> ScanResult | None:
        """跑一个时间片；未到周期返回 ``None``。异常一律向上抛，由调用方保持指标原值。"""

        now = self.clock()
        if self._last_run is not None and now - self._last_run < self.interval_seconds:
            return None
        # 先占住本周期：失败后不要在每个 tick 上重试，等下一个周期再来。
        self._last_run = now

        started = time.perf_counter()
        tenants = await self._tenants_snapshot(now)
        if not tenants:
            return ScanResult(
                tenant_id=None,
                rows=0,
                broken_at=None,
                detail="无审计影像，跳过一次",
                cycle_completed=True,
                elapsed_ms=0.0,
            )

        tenant_id = tenants[self._next_tenant % len(tenants)]
        self._next_tenant = (self._next_tenant + 1) % len(tenants)
        state = self._states.setdefault(tenant_id, _TenantState())

        rows = 0
        while rows < self.rows_per_run:
            limit = min(self.page_size, self.rows_per_run - rows)
            page = await self.ledger.verify_page(
                tenant_id,
                cursor=state.cursor,
                prev_hash=state.prev_hash,
                limit=limit,
                start_index=state.verified,
            )
            rows += page.checked

            if page.verification is not None:
                state.broken = True
                self._broken.add(tenant_id)
                self.sink.set_audit_chain_broken(True)
                self._reset_cycle(state)
                return ScanResult(
                    tenant_id=tenant_id,
                    rows=rows,
                    broken_at=page.verification.broken_at,
                    detail=page.verification.detail,
                    cycle_completed=False,
                    elapsed_ms=(time.perf_counter() - started) * 1000.0,
                )

            state.prev_hash = page.last_hash
            state.verified += page.checked
            state.cursor = page.next_cursor

            if page.exhausted:
                if state.broken:
                    state.broken = False
                    self._broken.discard(tenant_id)
                verified = state.verified
                # 整条链（从 0 到链尾）通过，才是可以写 0 的结论。
                self.sink.set_audit_chain_broken(bool(self._broken))
                self._reset_cycle(state)
                return ScanResult(
                    tenant_id=tenant_id,
                    rows=rows,
                    broken_at=None,
                    detail=f"完整校验通过（{verified} 条）",
                    cycle_completed=True,
                    elapsed_ms=(time.perf_counter() - started) * 1000.0,
                )

        return ScanResult(
            tenant_id=tenant_id,
            rows=rows,
            broken_at=None,
            detail="本时间片预算用尽，水位线续扫",
            cycle_completed=False,
            elapsed_ms=(time.perf_counter() - started) * 1000.0,
        )

    async def _tenants_snapshot(self, now: float) -> list[UUID]:
        if self.tenants is not None:
            return self.tenants
        stale = self._tenants_at is None or now - self._tenants_at >= self.tenant_refresh_seconds
        if stale:
            self._tenants = await self.ledger.tenant_ids()
            self._tenants_at = now
            # 租户集合变了，轮转下标没有意义，从头开始。
            self._next_tenant = 0
        return self._tenants

    @staticmethod
    def _reset_cycle(state: _TenantState) -> None:
        state.cursor = 0
        state.prev_hash = ""
        state.verified = 0
