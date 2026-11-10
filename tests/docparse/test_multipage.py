"""多页 / 多行明细：页码保留、行顺序稳定、金额合计可校验。"""

from __future__ import annotations

from decimal import Decimal

from apps.docparse import DocType, ParseStatus


def test_multipage_statement_merges_rows_and_keeps_page(pipeline, fixtures_dir):
    result = pipeline.parse_file(fixtures_dir / "multipage_statement.txt")

    assert result.doc_type is DocType.STATEMENT
    assert result.pages == 2
    assert len(result.lines) == 6
    assert [line.line_no for line in result.lines] == [1, 2, 3, 4, 5, 6]
    assert [line.page for line in result.lines] == [1, 1, 1, 1, 2, 2]
    assert [line.item_name.value for line in result.lines] == [
        "不锈钢螺钉M6",
        "铝合金支架",
        "尼龙垫片",
        "弹簧垫圈",
        "六角螺栓M8",
        "冷轧钢板",
    ]

    assert result.header.subtotal.value == Decimal("9205.00")
    assert sum(line.amount.value for line in result.lines) == Decimal("9205.00")
    assert result.issues == []
    assert result.status is ParseStatus.OK


def test_multipage_evidence_points_to_original_page(pipeline, fixtures_dir):
    result = pipeline.parse_file(fixtures_dir / "multipage_statement.txt")

    last = result.lines[-1]
    assert last.page == 2
    assert last.amount.coords[0].page == 2
    assert last.amount.coords[0].bbox[1] > 0
