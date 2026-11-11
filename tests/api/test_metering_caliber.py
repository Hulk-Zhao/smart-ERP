"""计量表三个行数的口径：总对账行 / 有效对账行 / 计费行数 各说各的。

默认参数下三个数会恰好相等（已确认错放行/客户原因无数据源、auto_released 桶为空），
「互相代称」在那种数据上测不出来。本文件刻意构造 有效对账行 2 ≠ 计费行数 1：
把一条已匹配行判成 ``auto_released``（自动放行但不计费），即扣减源接入后的形态。
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

from apps.api import metering
from apps.api.metering import BillingBucket, MeteringPolicy, build_statement
from apps.api.recon_snapshot import BatchSnapshot, SnapshotLine

DEDUCTED_STATUS = "已匹配但被扣减"


def _line(line_no: int, status: str) -> SnapshotLine:
    return SnapshotLine(
        line_no=line_no,
        side="supplier",
        supplier_code="SUP-A",
        supplier_ref=f"ST-{line_no}#1",
        our_ref=f"PO-{line_no}#1",
        status=status,
        match_type="exact",
        confidence="1",
        needs_manual=False,
        diff_types=(),
        diff_facts=(),
        owner=None,
        evidence=(),
        source_refs=(),
    )


def _snapshot(lines: list[SnapshotLine]) -> BatchSnapshot:
    return BatchSnapshot(
        batch_id=uuid4(),
        tenant_id=uuid4(),
        org_id="org-1",
        period="2026-10",
        system_ids=("SYS", "ST"),
        suppliers=("SUP-A",),
        status="已匹配",
        conclusive=True,
        status_reason="",
        metrics={"supplier_lines": len(lines)},
        input_fingerprint="fp",
        rerun_of=None,
        rerun_reason=None,
        created_by=uuid4(),
        created_at=datetime.now(UTC),
        lines=tuple(lines),
    )


def test_valid_lines_and_billable_lines_are_independently_identified(monkeypatch) -> None:
    """有效对账行 2、计费行数 1 时，两个数字与两个名称都必须各归各位。"""

    original = metering.classify_billing_bucket

    def classify(*, status: str, needs_manual: bool) -> BillingBucket:
        # 模拟扣减源接入后的唯一形态：自动放行但被扣减，进 auto_released 桶。
        if status == DEDUCTED_STATUS:
            return BillingBucket.AUTO_RELEASED
        return original(status=status, needs_manual=needs_manual)

    monkeypatch.setattr(metering, "classify_billing_bucket", classify)

    statement = build_statement(
        _snapshot([_line(1, "已匹配"), _line(2, DEDUCTED_STATUS)]),
        MeteringPolicy(unit_price=Decimal("0.30")),
    )

    # 三个数各自独立，且有效 ≠ 计费。
    assert statement.line_count == 2
    assert statement.auto_released_lines == 2
    assert statement.billable_lines == 1
    assert statement.auto_released_lines != statement.billable_lines
    assert {line.billing_bucket for line in statement.lines} == {
        BillingBucket.BILLABLE.value,
        BillingBucket.AUTO_RELEASED.value,
    }
    # 金额只按计费行数算，不按有效对账行算。
    assert statement.period_amount == "0.3000"

    steps = statement.calculation
    total_step = next(item for item in steps if item.startswith("总对账行数"))
    valid_step = next(item for item in steps if "有效对账行数（auto_released_lines" in item)
    billable_step = next(
        item for item in steps if item.startswith("计费行数（billable_lines")
    )
    amount_step = next(item for item in steps if item.startswith("本期金额"))

    assert total_step.endswith("= 2")
    assert valid_step.endswith("= 2")
    assert billable_step.startswith("计费行数（billable_lines，billable 桶行数）= 1")
    assert "auto_released 桶 1" in valid_step

    # 反向：计费行数那句不得叫「有效对账行」；有效那句也不得冒出计费行数的 1。
    assert "有效对账行" not in billable_step
    assert "= 1" not in valid_step
    assert "计费行数 1 ×" in amount_step
