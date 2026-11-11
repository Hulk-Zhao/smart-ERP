"""日切锚点调度（B-ANCHOR）：幂等、并发不覆盖、回拨不误报、追加后仍判对。"""

from __future__ import annotations

from datetime import UTC, date, datetime
from uuid import UUID, uuid4

import pytest
from audit_support import TENANT, TICKET, run
from sqlalchemy.exc import IntegrityError

from apps.audit.anchor import AuditAnchorService
from apps.audit.ledger import AuditLedger
from apps.audit.store import AuditAnchor, InMemoryAuditStore

DAY = date(2026, 11, 10)


class FakeClock:
    """可拨动的进程时钟：``now`` 给日标签，``monotonic`` 给退避计时。"""

    def __init__(self, now: datetime, monotonic: float = 1000.0) -> None:
        self.now_value = now
        self.monotonic_value = monotonic

    def now(self) -> datetime:
        return self.now_value

    def monotonic(self) -> float:
        return self.monotonic_value


def _append(ledger: AuditLedger, *, stamp: datetime, object_id: str):
    return run(
        ledger.append_image(
            tenant_id=TENANT,
            ticket_id=TICKET,
            step_id=uuid4(),
            system="fake",
            object_type="invoice_draft",
            object_id=object_id,
            before={"amount": "100.00", "status": "new"},
            after={"amount": "120.00", "status": "draft"},
            created_at=stamp,
        )
    )


def _service(ledger: AuditLedger, clock: FakeClock, **kwargs) -> AuditAnchorService:
    return AuditAnchorService(
        ledger=ledger,
        clock=clock.now,
        monotonic=clock.monotonic,
        tenants=[TENANT],
        **kwargs,
    )


def test_anchor_after_append_still_verifies():
    """硬要求：锚点写入后继续追加，链完整性校验必须判对。"""

    clock = FakeClock(datetime(2026, 11, 10, 23, 0, tzinfo=UTC))
    ledger = AuditLedger(clock=clock.now)
    _append(ledger, stamp=datetime(2026, 11, 10, 10, 0, tzinfo=UTC), object_id="D-1")
    _append(ledger, stamp=datetime(2026, 11, 10, 11, 0, tzinfo=UTC), object_id="D-2")
    tail = run(ledger.store.last_hash(TENANT))

    result = run(_service(ledger, clock).step())
    assert result is not None
    assert (result.record_count, result.last_hash) == (2, tail)

    anchor = run(ledger.store.get_anchor(TENANT, DAY))
    assert anchor is not None and anchor.record_count == 2

    # 锚点之后继续按业务追加两条（含跨日边界之后）
    _append(ledger, stamp=datetime(2026, 11, 10, 23, 30, tzinfo=UTC), object_id="D-3")
    _append(ledger, stamp=datetime(2026, 11, 11, 0, 5, tzinfo=UTC), object_id="D-4")

    assert run(ledger.verify_against_anchor(anchor)).ok is True
    assert run(ledger.verify(TENANT)).ok is True
    # 同一天再跑一次（新实例，没有内存标记）：库里已有锚点，只做前缀校验，不得重写
    assert run(_service(ledger, clock).step()) is not None
    assert run(ledger.store.get_anchor(TENANT, DAY)) == anchor


def test_second_tick_same_day_is_a_no_op():
    clock = FakeClock(datetime(2026, 11, 10, 12, 0, tzinfo=UTC))
    ledger = AuditLedger(clock=clock.now)
    _append(ledger, stamp=datetime(2026, 11, 10, 10, 0, tzinfo=UTC), object_id="D-1")
    service = _service(ledger, clock)
    assert run(service.step()) is not None

    clock.monotonic_value += 5.0
    assert run(service.step()) is None


def test_empty_tenant_list_writes_nothing():
    clock = FakeClock(datetime(2026, 11, 10, 12, 0, tzinfo=UTC))
    ledger = AuditLedger(clock=clock.now)
    service = AuditAnchorService(ledger=ledger, clock=clock.now, tenants=[])
    assert run(service.step()) is None
    assert run(ledger.store.get_anchor(TENANT, DAY)) is None


def test_failure_backs_off_then_retries():
    class FlakyAnchorStore(InMemoryAuditStore):
        def __init__(self) -> None:
            super().__init__()
            self.failures = 0

        async def record_anchor(self, anchor) -> None:
            if self.failures == 0:
                self.failures += 1
                raise RuntimeError("db down")
            await super().record_anchor(anchor)

    clock = FakeClock(datetime(2026, 11, 10, 12, 0, tzinfo=UTC), monotonic=100.0)
    ledger = AuditLedger(store=FlakyAnchorStore(), clock=clock.now)
    _append(ledger, stamp=datetime(2026, 11, 10, 10, 0, tzinfo=UTC), object_id="D-1")
    service = _service(ledger, clock, retry_seconds=60.0)

    with pytest.raises(RuntimeError):
        run(service.step())
    # 失败不留半成品：库内没有锚点行
    assert run(ledger.store.get_anchor(TENANT, DAY)) is None

    clock.monotonic_value = 130.0  # 仍在 60s 退避窗口内
    assert run(service.step()) is None
    assert run(ledger.store.get_anchor(TENANT, DAY)) is None

    clock.monotonic_value = 170.0  # 窗口过后重试成功
    result = run(service.step())
    assert result is not None and result.record_count == 1


def test_cross_day_rollback_does_not_duplicate_or_raise():
    """进程时钟跨日回拨：不少算、不重复、不抛 ``AuditChainError``。"""

    clock = FakeClock(datetime(2026, 11, 10, 23, 59, 30, tzinfo=UTC))
    ledger = AuditLedger(clock=clock.now)
    _append(
        ledger,
        stamp=datetime(2026, 11, 10, 23, 59, 30, tzinfo=UTC),
        object_id="D-1",
    )
    service = _service(ledger, clock)
    assert run(service.step()) is not None

    # 正常跨日：11-11 追加一行并锚定
    clock.now_value = datetime(2026, 11, 11, 0, 0, 10, tzinfo=UTC)
    _append(
        ledger,
        stamp=datetime(2026, 11, 11, 0, 0, 10, tzinfo=UTC),
        object_id="D-2",
    )
    clock.monotonic_value += 5.0
    day_two = run(service.step())
    assert day_two is not None and day_two.anchor_date == date(2026, 11, 11)

    # 时钟回拨到昨天 23:59:50：这条晚写的行盖的是旧日期（旧实现会把前缀重算成 3 条）
    clock.now_value = datetime(2026, 11, 10, 23, 59, 50, tzinfo=UTC)
    _append(
        ledger,
        stamp=datetime(2026, 11, 10, 23, 59, 50, tzinfo=UTC),
        object_id="D-3",
    )
    clock.monotonic_value += 5.0
    assert run(service.step()) is None  # 跳过，不补写过去日锚点

    first = run(ledger.store.get_anchor(TENANT, date(2026, 11, 10)))
    second = run(ledger.store.get_anchor(TENANT, date(2026, 11, 11)))
    assert first is not None and first.record_count == 1
    assert second is not None and second.record_count == 2
    # 已存锚点按存储值比较：不抛错，且两条锚点都仍然成立
    assert run(ledger.anchor_day(TENANT, date(2026, 11, 10))) == first
    assert run(ledger.verify_against_anchor(first)).ok is True
    assert run(ledger.verify_against_anchor(second)).ok is True
    assert run(ledger.verify(TENANT)).ok is True

    # 时钟回到 11-11：当天已锚，不再重复写
    clock.now_value = datetime(2026, 11, 11, 0, 1, 0, tzinfo=UTC)
    clock.monotonic_value += 5.0
    assert run(service.step()) is None
    assert run(ledger.store.get_anchor(TENANT, date(2026, 11, 11))) == second


def test_conflicting_insert_reads_winner_without_overwrite():
    """主键冲突（另一副本抢先写入）时重读赢家并按前缀校验，绝不覆盖。"""

    clock = FakeClock(datetime(2026, 11, 10, 12, 0, tzinfo=UTC))

    class LoserStore(InMemoryAuditStore):
        winner: AuditAnchor | None = None
        reads = 0
        writes = 0

        async def get_anchor(self, tenant_id, anchor_date):
            self.reads += 1
            # 第一次是写入前的查重（看不到），冲突后这次才看得到赢家写下的行
            return None if self.reads == 1 else self.winner

        async def record_anchor(self, anchor) -> None:
            self.writes += 1
            raise IntegrityError(
                "INSERT INTO erp.audit_anchor", {}, Exception("uq_audit_anchor_pkey")
            )

    store = LoserStore()
    ledger = AuditLedger(store=store, clock=clock.now)
    _append(ledger, stamp=datetime(2026, 11, 10, 10, 0, tzinfo=UTC), object_id="D-1")
    _append(ledger, stamp=datetime(2026, 11, 10, 11, 0, tzinfo=UTC), object_id="D-2")
    winner = AuditAnchor(TENANT, DAY, run(store.last_hash(TENANT)), 2)
    store.winner = winner

    assert run(ledger.anchor_day(TENANT, DAY)) == winner
    assert store.writes == 1  # 只尝试写一次，没有覆盖重写
    assert run(ledger.verify_against_anchor(winner)).ok is True


def test_tenant_isolation_in_service():
    other_tenant = UUID("33333333-3333-3333-3333-333333333333")
    clock = FakeClock(datetime(2026, 11, 10, 12, 0, tzinfo=UTC))
    ledger = AuditLedger(clock=clock.now)
    _append(ledger, stamp=datetime(2026, 11, 10, 10, 0, tzinfo=UTC), object_id="D-1")
    run(
        ledger.append_image(
            tenant_id=other_tenant,
            ticket_id=TICKET,
            step_id=uuid4(),
            system="fake",
            object_type="invoice_draft",
            object_id="D-OTHER",
            before={"status": "new"},
            after={"status": "draft"},
            created_at=datetime(2026, 11, 10, 11, 0, tzinfo=UTC),
        )
    )
    service = AuditAnchorService(
        ledger=ledger, clock=clock.now, tenants=[TENANT, other_tenant]
    )
    assert run(service.step()) is not None
    clock.monotonic_value += 5.0
    assert run(service.step()) is not None
    assert run(ledger.store.get_anchor(TENANT, DAY)).record_count == 1
    assert run(ledger.store.get_anchor(other_tenant, DAY)).record_count == 1
    clock.monotonic_value += 5.0
    assert run(service.step()) is None
