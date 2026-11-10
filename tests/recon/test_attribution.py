"""七类归因与置信度闸门（FR-104 / PRD §5.0）。"""

from __future__ import annotations

from decimal import Decimal

from apps.recon import match_lines, reconcile
from apps.recon.attribution import attribute_pair, build_uom_diff
from apps.recon.models import (
    BatchStatus,
    DiffItem,
    DiffOwner,
    DiffType,
    DocLine,
    Money,
)


def _attribution(request):
    outcome = match_lines(
        list(request.our_lines), list(request.supplier_lines), request.tolerance
    )
    assert len(outcome.pairs) == 1
    pair = outcome.pairs[0]
    return attribute_pair(pair, request.tolerance, pair.supplier.qty)


def test_unit_price_diff_amount_and_owner(make_request, make_line):
    request = make_request(
        [make_line(qty="10", unit_price="5", amount="50")],
        [
            make_line(
                system="STATEMENT", qty="10", unit_price="6", amount="60"
            )
        ],
    )
    attribution = _attribution(request)

    assert [diff.type for diff in attribution.diffs] == [DiffType.UNIT_PRICE]
    diff = attribution.diffs[0]
    assert diff.amount.amount == Decimal(10)
    assert diff.owner is DiffOwner.SUPPLIER
    assert diff.confidence >= Decimal("0.70")
    assert diff.needs_manual is False
    assert len(diff.evidence) == 2


def test_qty_diff_amount(make_request, make_line):
    request = make_request(
        [make_line(qty="10", unit_price="5", amount="50")],
        [
            make_line(
                system="STATEMENT", qty="12", unit_price="5", amount="60"
            )
        ],
    )
    attribution = _attribution(request)

    by_type = {diff.type: diff for diff in attribution.diffs}
    assert set(by_type) == {DiffType.QTY}
    assert by_type[DiffType.QTY].amount.amount == Decimal(10)
    assert by_type[DiffType.QTY].confidence == Decimal("0.88")


def test_scrap_not_deducted(make_request, make_line):
    request = make_request(
        [make_line(qty="97", unit_price="5", amount="485", scrap_qty="3")],
        [
            make_line(
                system="STATEMENT", qty="100", unit_price="5", amount="500"
            )
        ],
    )
    attribution = _attribution(request)

    by_type = {diff.type: diff for diff in attribution.diffs}
    assert set(by_type) == {DiffType.SCRAP}
    assert by_type[DiffType.SCRAP].amount.amount == Decimal(15)
    assert by_type[DiffType.SCRAP].owner is DiffOwner.SUPPLIER


def test_tax_rate_diff(make_request, make_line):
    request = make_request(
        [make_line(qty="10", unit_price="5", amount="50", tax_rate="0.13")],
        [
            make_line(
                system="STATEMENT",
                qty="10",
                unit_price="5",
                amount="53",
                tax_rate="0.16",
            )
        ],
    )
    attribution = _attribution(request)

    by_type = {diff.type: diff for diff in attribution.diffs}
    assert set(by_type) == {DiffType.TAX}
    assert by_type[DiffType.TAX].amount.amount == Decimal(3)


def test_unexplained_amount_is_not_forced_into_seven_types(make_request, make_line):
    request = make_request(
        [make_line(qty="10", unit_price="5", amount="50", tax_rate=None)],
        [
            make_line(
                system="STATEMENT",
                qty="10",
                unit_price="5",
                amount="55",
                tax_rate=None,
            )
        ],
    )
    attribution = _attribution(request)

    assert attribution.diffs == []
    assert attribution.unattributed is not None
    assert attribution.needs_manual is True

    result = reconcile(request)
    assert result.batch_status is BatchStatus.NEEDS_REVIEW
    assert result.conclusive is False
    assert len(result.unattributed) == 1


def test_duplicate_billing_is_intercepted(make_request, make_line):
    request = make_request(
        [make_line(doc_no="PO-1", qty="10", unit_price="5", amount="50")],
        [
            make_line(system="STATEMENT", doc_no="PO-1", qty="10", amount="50"),
            make_line(
                system="STATEMENT", doc_no="INV-9", qty="10", amount="50"
            ),
        ],
    )
    result = reconcile(request)

    types = [diff.type for diff in result.diffs]
    assert DiffType.DUPLICATE in types
    duplicate = next(diff for diff in result.diffs if diff.type is DiffType.DUPLICATE)
    assert duplicate.amount.amount == Decimal(50)
    assert duplicate.owner is DiffOwner.SUPPLIER
    assert any(line.status.value == "重复计费" for line in result.lines)
    # 重复占计费额 50%，判定为疑似数据异常并阻断自动处置
    assert result.metrics.suspected_anomaly is True
    assert result.metrics.auto_disposal_allowed is False


def test_confidence_below_threshold_requires_manual():
    low = DiffItem(
        type=DiffType.UNIT_PRICE,
        amount=Money(Decimal(1)),
        owner=DiffOwner.SUPPLIER,
        suggestion="待人工",
        confidence=Decimal("0.69"),
        evidence=(),
        ref="ERP:PO-1#1",
    )
    high = DiffItem(
        type=DiffType.UNIT_PRICE,
        amount=Money(Decimal(1)),
        owner=DiffOwner.SUPPLIER,
        suggestion="可自动",
        confidence=Decimal("0.70"),
        evidence=(),
        ref="ERP:PO-1#1",
    )
    assert low.needs_manual is True
    assert high.needs_manual is False

    uom = build_uom_diff(
        DocLine(
            system="STATEMENT",
            doc_no="INV-1",
            line_no=1,
            supplier_code="SUP-A",
            item_code="IT-A",
            qty=Decimal(1),
            unit="箱",
        ),
        "箱→PCS 缺换算",
    )
    assert uom.type is DiffType.UOM
    assert uom.confidence < Decimal("0.70")
    assert uom.needs_manual is True
