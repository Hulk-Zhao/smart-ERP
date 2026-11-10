"""统一数据模型映射：缺失降级、不猜、金额容差、反向映射。"""

from __future__ import annotations

from decimal import Decimal

import pytest

from apps.connectors.canonical.mapping import MappingContext, date_only
from apps.connectors.canonical.models import Document, DocumentLine, Item, Party
from apps.connectors.u8.mapping import (
    build_po_draft_body,
    map_arrival_vouch,
    map_item,
    map_purchase_order,
    map_supplier,
    to_u8_item,
    to_u8_supplier,
)
from apps.connectors.u8.models import (
    U8ArrivalVouch,
    U8Item,
    U8PurchaseOrder,
    U8Supplier,
)
from apps.platform.contracts.canonical import Money
from tests.connectors.support import load_fixture

CTX = MappingContext(connector="u8", authority_system="erp", currency="CNY")


def _items() -> dict[str, Item]:
    result: dict[str, Item] = {}
    for row in load_fixture("items.json"):
        outcome = map_item(U8Item.model_validate(row), CTX)
        assert outcome.record is not None
        result[outcome.record.code] = outcome.record
    return result


# --------------------------------------------------------------------- 供应商


def test_supplier_maps_with_provenance() -> None:
    outcome = map_supplier(
        U8Supplier(cVenCode="V001", cVenName="苏州精密铸造有限公司", cVenAbbName="苏州精密"),
        CTX,
    )

    assert outcome.blocked is False
    assert outcome.completeness == "complete"
    party = outcome.record
    assert isinstance(party, Party)
    assert party.code == "V001"
    assert party.aliases == ["苏州精密"]
    assert party.source_refs == {"u8": "V001"}
    assert party.authority_system == "erp"
    assert party.evidence[0].field == "cVenCode"


def test_supplier_without_code_is_blocked_not_invented() -> None:
    outcome = map_supplier(U8Supplier(cVenName="未建档供应商"), CTX)

    assert outcome.record is None
    assert outcome.blocked is True
    assert outcome.completeness == "unavailable"
    assert [issue.field for issue in outcome.issues] == ["code"]


def test_supplier_name_missing_keeps_none_and_reports() -> None:
    outcome = map_supplier(U8Supplier(cVenCode="V009"), CTX)

    assert outcome.record is not None
    assert outcome.record.name is None  # 缺失 ≠ 空字符串，更不是 0
    assert outcome.reason_codes() == ["missing"]


# ----------------------------------------------------------------------- 物料


def test_item_missing_base_unit_is_flagged_and_not_guessed() -> None:
    outcome = map_item(U8Item(cInvCode="M-3001", cInvName="无基本单位物料"), CTX)

    assert outcome.record is not None
    assert outcome.record.base_unit is None
    assert outcome.reason_codes() == ["base_unit_missing"]
    assert outcome.completeness == "partial"


def test_item_roundtrip_to_u8_payload() -> None:
    item = _items()["M-1001"]
    payload = to_u8_item(item)
    assert payload == {
        "cInvCode": "M-1001",
        "cInvName": "法兰盘毛坯",
        "cInvStd": "DN100 / 20#",
        "cComUnitName": "件",
    }
    party = map_supplier(U8Supplier(cVenCode="V002", cVenName="无锡华通机械有限公司"), CTX).record
    assert party is not None
    assert to_u8_supplier(party)["cVenCode"] == "V002"


# ----------------------------------------------------------------------- 单据


def test_purchase_order_maps_total_and_dates() -> None:
    raw = U8PurchaseOrder.model_validate(load_fixture("purchase_orders.json")[0])
    outcome = map_purchase_order(raw, CTX, items=_items())

    assert outcome.blocked is False
    doc = outcome.record
    assert isinstance(doc, Document)
    assert doc.doc_no == "PO202610310001"
    assert doc.doc_date == "2026-10-31"  # ISO 时间戳归一成日期
    assert doc.kind == "purchase_order"
    assert len(doc.lines) == 2
    assert doc.total_amount is not None
    assert doc.total_amount.value == Decimal(12680)
    assert doc.total_amount.currency == "CNY"
    assert doc.lines[0].unit_price is not None
    assert doc.lines[0].unit_price.tax_included is True


def test_amount_mismatch_blocks_total_but_keeps_line() -> None:
    raw = U8PurchaseOrder.model_validate(load_fixture("purchase_orders.json")[1])
    outcome = map_purchase_order(raw, CTX)

    assert outcome.record is not None
    assert outcome.reason_codes() == ["amount_mismatch", "missing"]
    assert outcome.record.total_amount is None  # 不「先把能加的加起来」
    assert len(outcome.record.lines) == 1
    assert outcome.record.lines[0].amount is not None


def test_purchase_order_missing_supplier_is_blocked() -> None:
    raw = U8PurchaseOrder.model_validate(load_fixture("purchase_orders.json")[2])
    outcome = map_purchase_order(raw, CTX)

    assert outcome.record is None
    assert outcome.blocked is True
    assert "party_code" in {issue.field for issue in outcome.issues}


def test_unit_without_conversion_is_degraded() -> None:
    raw = U8ArrivalVouch.model_validate(load_fixture("arrival_vouches.json")[1])
    outcome = map_arrival_vouch(raw, CTX, items=_items())

    assert outcome.record is not None
    assert "unit_conversion_missing" in outcome.reason_codes()
    assert outcome.record.total_amount is None
    assert outcome.record.lines[0].unit == "箱"  # 原样保留，不偷偷换成"吨"


def test_missing_quantity_stays_none() -> None:
    raw = U8PurchaseOrder(
        cCode="PO-NO-QTY",
        dPODate="2026-10-31",
        cVenCode="V001",
        details=[{"cInvCode": "M-1001", "cComUnitName": "件", "iTaxPrice": 35.5, "iMoney": 4260}],
    )
    outcome = map_purchase_order(raw, CTX)

    assert outcome.record is not None
    line = outcome.record.lines[0]
    assert line.quantity is None
    assert "missing" in outcome.reason_codes()
    assert outcome.record.total_amount is None


def test_date_only_normalizes_u8_formats() -> None:
    assert date_only("2026-10-31T00:00:00") == "2026-10-31"
    assert date_only("2026/11/1 08:00:00") == "2026-11-01"
    assert date_only("") is None
    assert date_only("not-a-date") is None


# ---------------------------------------------------------------- 反向（写回）


def _doc(*, tax_included: bool = True) -> Document:
    return Document(
        kind="purchase_order",
        doc_no="PO202610310001",
        doc_date="2026-11-02",
        party_code="V001",
        lines=[
            DocumentLine(
                line_no=1,
                item_code="M-1001",
                quantity="120",
                unit="件",
                unit_price=Money(
                    amount="35.5000", currency="CNY", tax_included=tax_included
                ),
                tax_rate="13",
                source_refs={"u8": "PO202610310001#1"},
                authority_system="erp",
            )
        ],
        source_refs={"u8": "PO202610310001"},
        authority_system="erp",
    )


def test_reverse_mapping_uses_tax_price_and_carries_business_key() -> None:
    outcome = build_po_draft_body(
        {
            "source_doc_no": "PO202610310001",
            "supplier_code": "V001",
            "doc_date": "2026-11-02",
            "memo": "工单 T-001",
            "lines": [
                {
                    "item_code": "M-1001",
                    "quantity": "120",
                    "unit": "件",
                    "unit_price": "35.5",
                    "tax_rate": "13",
                }
            ],
        },
        CTX,
    )

    assert outcome.record is not None
    body = outcome.record
    assert body["source_doc_no"] == "PO202610310001"
    assert body["cVenCode"] == "V001"
    assert body["dPODate"] == "2026-11-02"
    assert body["details"][0]["iTaxPrice"] == "35.5000"
    assert "iUnitPrice" not in body["details"][0]


def test_reverse_mapping_blocks_when_unit_missing() -> None:
    outcome = build_po_draft_body(
        {
            "source_doc_no": "PO202610310001",
            "supplier_code": "V001",
            "doc_date": "2026-11-02",
            "lines": [{"item_code": "M-1001", "quantity": "120"}],
        },
        CTX,
    )

    assert outcome.record is None
    assert outcome.blocked is True
    assert "unit_conversion_missing" in outcome.reason_codes()


def test_reverse_mapping_uses_plain_price_when_not_tax_inclusive() -> None:
    from apps.connectors.u8.mapping import to_u8_purchase_order_draft

    outcome = to_u8_purchase_order_draft(_doc(tax_included=False), CTX)
    assert outcome.record is not None
    assert outcome.record["details"][0]["iUnitPrice"] == "35.5000"


@pytest.mark.parametrize("field", ["source_doc_no", "doc_date", "supplier_code"])
def test_reverse_mapping_requires_core_fields(field: str) -> None:
    payload = {
        "source_doc_no": "PO202610310001",
        "supplier_code": "V001",
        "doc_date": "2026-11-02",
        "lines": [
            {"item_code": "M-1001", "quantity": "1", "unit": "件", "unit_price": "35.5"}
        ],
    }
    payload.pop(field)
    outcome = build_po_draft_body(payload, CTX)
    assert outcome.blocked is True
    assert field in {issue.field for issue in outcome.issues}
