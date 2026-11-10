"""清晰单据：分类、字段、明细、金额一致性、坐标回链全部通过。"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from apps.docparse import DocType, ParseStatus


def test_clear_invoice_is_parsed_ok(pipeline, fixtures_dir):
    result = pipeline.parse_file(fixtures_dir / "clear_invoice.txt")

    assert result.status is ParseStatus.OK
    assert result.message is None
    assert result.doc_type is DocType.INVOICE
    assert result.doc_type_confidence >= 0.8
    assert result.pages == 1
    assert result.file_kind == "text"

    assert result.header.doc_no.value == "88231007"
    assert result.header.doc_date.value == date(2026, 3, 18)
    assert result.header.supplier_name.value == "东莞市恒泰五金制品有限公司"
    assert result.header.supplier_tax_no.value == "91441900MA4W8XYZ3K"
    assert result.header.buyer_name.value == "广州华南精密制造有限公司"
    assert result.header.currency.value == "CNY"
    assert result.header.subtotal.value == Decimal("3650.00")
    assert result.header.tax_amount.value == Decimal("474.50")
    assert result.header.total_amount.value == Decimal("4124.50")

    assert len(result.lines) == 3
    first = result.lines[0]
    assert first.line_no == 1
    assert first.page == 1
    assert first.item_name.value == "不锈钢螺钉M6"
    assert first.spec.value == "M6*20"
    assert first.unit.value == "个"
    assert first.quantity.value == Decimal(1000)
    assert first.unit_price.value == Decimal("0.85")
    assert first.amount.value == Decimal("850.00")
    assert first.tax_rate.value == Decimal("0.13")
    assert first.tax_amount.value == Decimal("110.50")

    assert result.missing_fields == []
    assert result.low_confidence_fields == []
    assert result.issues == []


def test_clear_invoice_fields_carry_evidence_coords(pipeline, fixtures_dir):
    result = pipeline.parse_file(fixtures_dir / "clear_invoice.txt")

    for field in (
        result.header.doc_no,
        result.header.doc_date,
        result.header.supplier_name,
        result.header.total_amount,
    ):
        assert field.coords, "已识别字段必须带证据坐标"
        assert field.coords[0].page == 1
        x0, y0, x1, y1 = field.coords[0].bbox
        assert x0 < x1 and y0 < y1
        assert field.raw_text

    for line in result.lines:
        for field in (line.item_name, line.quantity, line.amount):
            assert field.coords[0].page == line.page


def test_clear_invoice_issues_none_but_parser_ids_recorded(pipeline, fixtures_dir):
    result = pipeline.parse_file(fixtures_dir / "clear_invoice.txt")
    assert "plaintext.v1" in result.parser
    assert "rule-fields.v1" in result.parser
