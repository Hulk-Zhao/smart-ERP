"""审计链：可校验、不可改、可按工单/任务/时间检索（FR-1301/1307）。"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from audit_support import TENANT, TICKET, run
from sqlalchemy.exc import IntegrityError

from apps.audit.ledger import (
    AuditChainError,
    AuditLedger,
    AuditQuery,
    InMemorySnapshotStore,
)
from apps.audit.models import AuditImageRow
from apps.audit.store import AuditStoreError, InMemoryAuditStore, _to_image
from apps.platform.contracts.audit import (
    AuditImage,
    compute_image_hash,
    verify_chain,
)


def _append(ledger: AuditLedger, *, ticket: UUID = TICKET, hour: int = 10, step: UUID | None = None):
    return run(
        ledger.append_image(
            tenant_id=TENANT,
            ticket_id=ticket,
            step_id=step or uuid4(),
            system="fake",
            object_type="invoice_draft",
            object_id=f"D-{hour}",
            before={"amount": "100.00", "status": "new"},
            after={"amount": "120.00", "status": "draft"},
            created_at=datetime(2026, 11, 10, hour, 0, tzinfo=UTC),
        )
    )


def test_hash_chain_is_verifiable_and_matches_contract():
    ledger = AuditLedger(clock=lambda: datetime(2026, 11, 10, tzinfo=UTC))
    first = _append(ledger, hour=10)
    second = _append(ledger, hour=11)
    third = _append(ledger, hour=12)

    assert first.prev_hash == ""
    assert second.prev_hash == first.hash
    assert third.prev_hash == second.hash
    assert verify_chain([first, second, third]) is True

    verification = run(ledger.verify(TENANT))
    assert verification.ok is True
    assert verification.checked == 3
    assert run(ledger.assert_intact(TENANT)).ok is True


def test_field_diff_and_snapshots_are_stored():
    snapshots = InMemorySnapshotStore()
    ledger = AuditLedger(snapshots=snapshots)
    image = _append(ledger)

    fields = {entry.field: entry for entry in image.field_diff}
    assert fields["amount"].delta == "20.00"
    assert fields["status"].before == "new"
    assert fields["status"].after == "draft"

    assert image.before_ref is not None and image.after_ref is not None
    assert snapshots.get(image.before_ref)["status"] == "new"
    assert snapshots.get(image.after_ref)["status"] == "draft"


def test_db_roundtrip_preserves_hash_for_decimal_and_datetime_values():
    ledger = AuditLedger(clock=lambda: datetime(2026, 11, 10, 12, tzinfo=UTC))
    image = run(
        ledger.append_image(
            tenant_id=TENANT,
            ticket_id=TICKET,
            step_id=uuid4(),
            system="fake",
            object_type="invoice_draft",
            object_id="D-ROUND",
            before={"amount": Decimal("100.00"), "due": datetime(2026, 12, 1)},
            after={"amount": Decimal("120.00"), "due": datetime(2026, 12, 2)},
        )
    )
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
    reloaded = _to_image(row)
    assert compute_image_hash(reloaded.prev_hash, reloaded.model_dump(mode="python")) == image.hash


def test_missing_before_snapshot_marks_diff_missing():
    ledger = AuditLedger()
    image = run(
        ledger.append_image(
            tenant_id=TENANT,
            ticket_id=TICKET,
            step_id=uuid4(),
            system="fake",
            object_type="invoice_draft",
            object_id="D-X",
            before=None,
            after={"status": "draft"},
        )
    )
    assert image.diff_missing is True
    assert image.field_diff == []
    assert image.before_ref is None


def test_search_by_ticket_step_and_time_range():
    ledger = AuditLedger()
    other_ticket = uuid4()
    step = uuid4()
    _append(ledger, ticket=TICKET, hour=10, step=step)
    _append(ledger, ticket=TICKET, hour=12)
    _append(ledger, ticket=other_ticket, hour=11)

    by_ticket = run(ledger.list_images(AuditQuery(tenant_id=TENANT, ticket_id=TICKET)))
    assert len(by_ticket) == 2
    assert {image.ticket_id for image in by_ticket} == {TICKET}

    by_step = run(ledger.list_images(AuditQuery(tenant_id=TENANT, step_id=step)))
    assert len(by_step) == 1

    by_time = run(
        ledger.list_images(
            AuditQuery(
                tenant_id=TENANT,
                since=datetime(2026, 11, 10, 10, 30, tzinfo=UTC),
                until=datetime(2026, 11, 10, 11, 30, tzinfo=UTC),
            )
        )
    )
    assert len(by_time) == 1
    assert by_time[0].ticket_id == other_ticket

    # 租户之间链与检索都隔离
    other_tenant = uuid4()
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
        )
    )
    assert run(ledger.verify(other_tenant)).checked == 1
    assert len(run(ledger.list_images(AuditQuery(tenant_id=other_tenant)))) == 1


class TamperingStore(InMemoryAuditStore):
    """模拟有人绕过应用直接改库（真实环境由触发器/授权拒绝）。"""

    def __init__(self, mutate) -> None:
        super().__init__()
        self._mutate = mutate

    async def chain(self, tenant_id: UUID) -> list[AuditImage]:
        rows = await super().chain(tenant_id)
        return self._mutate(rows)


def test_tampered_record_is_detected_and_not_repaired():
    def mutate(rows: list[AuditImage]) -> list[AuditImage]:
        target = rows[1]
        forged = target.model_copy(
            update={
                "field_diff": [
                    target.field_diff[0].model_copy(update={"after": "1.00"})
                ]
            }
        )
        return [rows[0], forged, rows[2]]

    ledger = AuditLedger(store=TamperingStore(mutate))
    _append(ledger, hour=10)
    _append(ledger, hour=11)
    _append(ledger, hour=12)

    verification = run(ledger.verify(TENANT))
    assert verification.ok is False
    assert verification.broken_at == 1
    assert "篡改" in verification.detail

    with pytest.raises(AuditChainError):
        run(ledger.assert_intact(TENANT))

    # 校验不自动修复：原始数据仍在，后续写入继续追加
    assert len(run(ledger.list_images(AuditQuery(tenant_id=TENANT)))) == 3


def test_append_is_immutable_and_fork_is_rejected():
    ledger = AuditLedger()
    image = _append(ledger)

    assert not hasattr(ledger, "update_image")
    assert not hasattr(ledger, "delete_image")
    assert not hasattr(ledger.store, "update")
    assert not hasattr(ledger.store, "delete")

    forged = image.model_copy(update={"prev_hash": "f" * 64})
    with pytest.raises(AuditStoreError):
        run(ledger.store.append(forged))


def test_daily_anchor_detects_truncation():
    fixed = datetime(2026, 11, 10, 23, 0, tzinfo=UTC)
    ledger = AuditLedger(clock=lambda: fixed)
    _append(ledger, hour=10)
    _append(ledger, hour=11)

    anchor = run(ledger.anchor_day(TENANT, date(2026, 11, 10)))
    assert anchor.record_count == 2
    assert run(ledger.verify_against_anchor(anchor)).ok is True
    assert run(ledger.anchor_day(TENANT, date(2026, 11, 10))) == anchor

    # 模拟链尾被删（绕过只追加授权）
    ledger.store._rows[TENANT].pop()
    verification = run(ledger.verify_against_anchor(anchor))
    assert verification.ok is False
    assert verification.checked == 1

    with pytest.raises(AuditChainError):
        run(ledger.anchor_day(TENANT, date(2026, 11, 10)))


def test_anchor_boundary_survives_cross_day_clock_rollback():
    """跨日时钟回拨：晚写的行带着昨天的 created_at 落库，次日重算不得报错。

    旧实现每次调用都用 ``created_at.date() <= day`` 重新筛前缀，于是这条晚写的
    行会在下一次调用时被补进旧日前缀，``record_count`` 与已存锚点不一致 →
    ``AuditChainError``（安全告警被时钟回拨变成噪声，看板 B-ANCHOR）。
    新实现把边界固化成写入时刻的 ``(record_count, last_hash)``，之后只按存储值比较。

    方向差异：内存实现的边界是列表长度（下标 0 起），PG 实现是 ``chain_seq``
    （全局 identity，跨租户跳过）；两者都表示「链的前缀长度」，语义同向，
    但 PG 的游标值本身不能拿来当 record_count，也不能跨实现复用。
    """

    def append(stamp: datetime, object_id: str) -> AuditImage:
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

    ledger = AuditLedger(clock=lambda: datetime(2026, 11, 11, 0, 0, 10, tzinfo=UTC))
    first = append(datetime(2026, 11, 10, 23, 59, 30, tzinfo=UTC), "D-1")

    anchor = run(ledger.anchor_day(TENANT, date(2026, 11, 10)))
    assert (anchor.record_count, anchor.last_hash) == (1, first.hash)

    # 真实时间已过零点 10 秒，进程时钟被回拨到昨天 23:59:50：这条晚写的行盖的是旧日期。
    append(datetime(2026, 11, 10, 23, 59, 50, tzinfo=UTC), "D-2")

    assert run(ledger.anchor_day(TENANT, date(2026, 11, 10))) == anchor
    assert run(ledger.verify_against_anchor(anchor)).ok is True


class RacedOnceStore(InMemoryAuditStore):
    """模拟并发：第一次追加时链尾已被别人占了（唯一约束触发 IntegrityError）。"""

    def __init__(self, winner_hash: str) -> None:
        super().__init__()
        self._winner_hash = winner_hash
        self._lost = False
        self.attempts: list[str] = []
        self.accepted: list[AuditImage] = []

    async def last_hash(self, tenant_id: UUID) -> str:
        return self._winner_hash if self._lost else ""

    async def append(self, image: AuditImage) -> None:
        self.attempts.append(image.prev_hash)
        if not self._lost:
            self._lost = True
            raise IntegrityError("INSERT INTO audit_image", {}, Exception("uq_audit_image_parent"))
        self.accepted.append(image)


def test_concurrent_append_retries_once_after_integrity_error():
    """唯一约束触发后必须重读链尾重试一次，而不是把并发变成 500。"""

    winner = "a" * 64
    store = RacedOnceStore(winner)
    ledger = AuditLedger(store=store)

    image = _append(ledger)

    assert store.attempts == ["", winner]
    assert image.prev_hash == winner
    assert [row.hash for row in store.accepted] == [image.hash]


def test_second_integrity_error_is_not_swallowed():
    """重试一次仍冲突就必须抛出——不能无限重试掩盖真正的分叉。"""

    class AlwaysConflicts(InMemoryAuditStore):
        async def append(self, image: AuditImage) -> None:
            raise IntegrityError("INSERT", {}, Exception("uq_audit_image_parent"))

    ledger = AuditLedger(store=AlwaysConflicts())
    with pytest.raises(IntegrityError):
        _append(ledger)
