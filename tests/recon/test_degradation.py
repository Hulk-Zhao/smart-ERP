"""降级分支：缺关键主数据/范围 → 无法对账 + 缺失清单，绝不输出带猜测的结论。"""

from __future__ import annotations

from decimal import Decimal

import pytest

from apps.recon import conversion_factor, reconcile
from apps.recon.models import BatchStatus, MissingKind


def _assert_blocked(result):
    assert result.batch_status is BatchStatus.NO_CONCLUSION
    assert result.conclusive is False
    assert result.lines == ()
    assert result.diffs == ()
    assert result.unattributed == ()
    assert result.missing_items
    assert result.metrics is None


def test_missing_org_scope_blocks(make_request, make_line):
    request = make_request(
        [make_line()],
        [make_line(system="STATEMENT")],
        scope={"org_id": "", "period": "2026-10", "system_ids": ["ERP"]},
    )
    result = reconcile(request)

    _assert_blocked(result)
    assert MissingKind.SCOPE in {item.kind for item in result.missing_items}


@pytest.mark.parametrize(
    "scope",
    [
        {"org_id": "ORG-TEST", "period": "", "system_ids": ["ERP"]},
        {"org_id": "ORG-TEST", "period": "2026-10", "system_ids": []},
    ],
)
def test_incomplete_scope_blocks(make_request, make_line, scope):
    request = make_request(
        [make_line()], [make_line(system="STATEMENT")], scope=scope
    )
    result = reconcile(request)

    _assert_blocked(result)
    assert MissingKind.SCOPE in {item.kind for item in result.missing_items}


def test_unbound_supplier_blocks(make_request, make_line):
    request = make_request(
        [make_line()],
        [make_line(system="STATEMENT")],
        suppliers=("SUP-A", "SUP-X"),
        master_suppliers=("SUP-A",),
    )
    result = reconcile(request)

    _assert_blocked(result)
    supplier_gaps = [
        item for item in result.missing_items if item.kind is MissingKind.SUPPLIER
    ]
    assert [gap.code for gap in supplier_gaps] == ["SUP-X"]


def test_unbound_item_blocks_and_is_deduplicated(make_request, make_line):
    request = make_request(
        [make_line(item_code="IT-Z")],
        [make_line(system="STATEMENT", item_code="IT-Z")],
    )
    result = reconcile(request)

    _assert_blocked(result)
    item_gaps = [item for item in result.missing_items if item.kind is MissingKind.ITEM]
    assert [gap.code for gap in item_gaps] == ["IT-Z"]


def test_missing_qty_is_not_treated_as_zero(make_request, make_line):
    request = make_request(
        [make_line(qty=None, amount="50")],
        [make_line(system="STATEMENT", qty="10", amount="50")],
    )
    result = reconcile(request)

    _assert_blocked(result)
    assert MissingKind.LINE_DATA in {item.kind for item in result.missing_items}


def test_missing_base_unit_blocks(make_request, make_line):
    request = make_request(
        [make_line()],
        [make_line(system="STATEMENT")],
        items=[{"code": "IT-A", "name": "无基础单位物料"}],
    )
    result = reconcile(request)

    _assert_blocked(result)
    assert MissingKind.BASE_UNIT in {item.kind for item in result.missing_items}


def test_missing_unit_conversion_blocks(make_request, make_line):
    request = make_request(
        [make_line(qty="100", unit="PCS", unit_price="5", amount="500")],
        [
            make_line(
                system="STATEMENT", qty="1", unit="箱", unit_price="500", amount="500"
            )
        ],
        unit_conversions=(),
    )
    result = reconcile(request)

    _assert_blocked(result)
    assert MissingKind.UNIT_CONVERSION in {
        item.kind for item in result.missing_items
    }


def test_unit_conversion_present_normalizes_qty(make_request, make_line):
    request = make_request(
        [make_line(doc_no="PO-1", qty="100", unit="PCS", unit_price="5", amount="500")],
        [
            make_line(
                system="STATEMENT",
                doc_no="PO-1",
                qty="1",
                unit="箱",
                unit_price="500",
                amount="500",
            )
        ],
        unit_conversions=[
            {"item": "IT-A", "from_unit": "箱", "to_unit": "PCS", "factor": "100"}
        ],
    )
    result = reconcile(request)

    assert result.batch_status is BatchStatus.RECONCILED
    line = result.lines[0]
    assert line.qty_supplier == Decimal("100.0000")
    assert line.diffs == ()
    # 箱价 / 100 = 件价，不应误判为单价不一致
    assert result.diffs == ()


def test_conversion_factor_supports_inverse(make_request):
    request = make_request(
        [],
        [],
        unit_conversions=[
            {"item": "IT-A", "from_unit": "箱", "to_unit": "PCS", "factor": "100"}
        ],
    )
    master = request.master_data
    assert conversion_factor(master, "IT-A", "PCS", "箱") == Decimal("0.01")
    assert conversion_factor(master, "IT-A", "箱", "PCS") == Decimal("100")
    assert conversion_factor(master, "IT-B", "箱", "PCS") is None
