"""缺字段：输出「无法识别」+ 缺失字段清单，不编造供应商，不把空值写成 0。"""

from __future__ import annotations

from decimal import Decimal

from apps.docparse import DocType, ParseStatus


def test_missing_supplier_is_unrecognized(pipeline, fixtures_dir):
    result = pipeline.parse_file(fixtures_dir / "missing_fields_delivery_note.txt")

    assert result.doc_type is DocType.DELIVERY_NOTE
    assert result.status is ParseStatus.UNRECOGNIZED
    assert result.message and "无法识别" in result.message

    assert result.header.supplier_name.value is None
    assert result.header.supplier_name.confidence == 0.0
    assert result.header.supplier_name.coords == []

    paths = {(field.path, field.label, field.critical) for field in result.missing_fields}
    assert ("header.supplier_name", "供应商名称", True) in paths

    # 其它已识别字段仍保留，便于补录表单预填
    assert result.header.doc_no.value == "SH-2026-0317-08"
    assert result.header.buyer_name.value == "广州华南精密制造有限公司"
    assert len(result.lines) == 2


def test_missing_amount_stays_null_not_zero(pipeline, fixtures_dir):
    result = pipeline.parse_file(fixtures_dir / "missing_fields_delivery_note.txt")

    first = result.lines[0]
    assert first.item_name.value == "冷轧钢板"
    assert first.quantity.value == Decimal("60")
    assert first.unit_price.value is None
    assert first.amount.value is None
    assert first.amount.confidence == 0.0
    assert first.amount.coords == []


def test_unrecognized_never_invents_values(pipeline, fixtures_dir):
    result = pipeline.parse_file(fixtures_dir / "missing_fields_delivery_note.txt")

    # 头字段为空就是 None，不会被补成 ""/0
    for name in ("supplier_name",):
        assert getattr(result.header, name).value is None
    # 也没有凭空生成明细
    assert all(line.item_name.value for line in result.lines)
