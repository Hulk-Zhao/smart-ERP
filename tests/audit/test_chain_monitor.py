"""审计链巡检的指标写入语义：断链=1、完整=0、校验失败不得写 0。"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from audit_support import TENANT, TICKET, run

from apps.audit.ledger import AuditLedger
from apps.audit.monitor import AuditChainMonitor
from apps.audit.store import InMemoryAuditStore
from observability.metrics import MetricsRegistry


def _seed(ledger: AuditLedger, *, rows: int = 3) -> None:
    for hour in range(10, 10 + rows):
        run(
            ledger.append_image(
                tenant_id=TENANT,
                ticket_id=TICKET,
                step_id=uuid4(),
                system="fake",
                object_type="invoice_draft",
                object_id=f"D-{hour}",
                before={"amount": "100.00", "status": "new"},
                after={"amount": "120.00", "status": "draft"},
                created_at=datetime(2026, 11, 10, hour, 0, tzinfo=UTC),
            )
        )


def _monitor(ledger: AuditLedger, metrics: MetricsRegistry) -> AuditChainMonitor:
    # interval=0 + 固定时钟：让每个 step 都真的跑一次，不依赖真实时间。
    return AuditChainMonitor(
        ledger=ledger,
        sink=metrics,
        rows_per_run=100,
        page_size=50,
        interval_seconds=0.0,
        clock=lambda: 0.0,
    )


def test_tampered_row_sets_metric_to_one() -> None:
    store = InMemoryAuditStore()
    ledger = AuditLedger(store=store)
    _seed(ledger)
    # 直接改库里第 2 条的 hash，模拟绕过应用的篡改（hash 与内容不再对得上）。
    store._rows[TENANT][1] = store._rows[TENANT][1].model_copy(
        update={"hash": "0" * 64}
    )
    metrics = MetricsRegistry()
    monitor = _monitor(ledger, metrics)

    result = run(monitor.step())

    assert result is not None
    assert result.broken_at == 1
    assert metrics.audit_chain_broken.value() == 1.0


def test_intact_chain_sets_metric_to_zero() -> None:
    ledger = AuditLedger(store=InMemoryAuditStore())
    _seed(ledger)
    metrics = MetricsRegistry()
    monitor = _monitor(ledger, metrics)

    result = run(monitor.step())

    assert result is not None
    assert result.broken_at is None
    assert result.cycle_completed is True
    assert metrics.audit_chain_broken.value() == 0.0


def test_scan_failure_must_not_write_zero() -> None:
    class BrokenStore(InMemoryAuditStore):
        async def chain_page(self, tenant_id, *, cursor, limit):  # type: ignore[override]
            raise RuntimeError("数据库瞬断")

    ledger = AuditLedger(store=BrokenStore())
    _seed(ledger)
    metrics = MetricsRegistry()
    # 先造一个「上次结论是断链」的状态，再看失败会不会把 1 悄悄翻成 0。
    metrics.set_audit_chain_broken(True)
    monitor = _monitor(ledger, metrics)

    with pytest.raises(RuntimeError):
        run(monitor.step())

    assert metrics.audit_chain_broken.value() == 1.0
