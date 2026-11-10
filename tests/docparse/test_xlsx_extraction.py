"""XLSX 真抽取：合并单元格、缺列、多工作表拼接、日期序列号。"""

from __future__ import annotations

from datetime import date

import pytest

from apps.docparse.errors import EmptyDocumentError
from apps.docparse.extractors import SpreadsheetExtractor
from apps.docparse.models import DocType, ParseStatus


def test_merged_cells_and_two_sheets_are_parsed(pipeline, fixtures_dir):
    result = pipeline.parse_file(fixtures_dir / "messy_invoice.xlsx")

    assert result.file_kind == "spreadsheet"
    assert result.doc_type is DocType.INVOICE
    assert result.pages == 2
    assert result.status is ParseStatus.OK
    assert result.issues == []

    assert result.header.doc_no.value == "88231007"
    assert result.header.doc_date.value == date(2026, 3, 5)
    assert result.header.supplier_name.value == "东莞市恒泰五金制品有限公司"
    assert result.header.total_amount.value == 1638.5

    assert len(result.lines) == 3
    assert [line.item_name.value for line in result.lines] == [
        "不锈钢螺钉M6",
        "平垫片",
        "六角螺母",
    ]
    # 第三行来自第二个工作表（续页），页码必须跟着走，供前端回链到正确页
    assert result.lines[2].page == 2
    assert result.lines[2].quantity.value == 500


def test_numeric_cells_and_date_style_are_converted(fixtures_dir):
    text = SpreadsheetExtractor().extract(
        (fixtures_dir / "messy_invoice.xlsx").read_bytes(),
        filename="messy_invoice.xlsx",
    )

    header_page = text.pages[0]
    # 单元格按引用保留列位：A2 标签、B2 值，中间没有值塌陷
    assert header_page[1].cells == ("发票号码", "88231007")
    # B3 是日期格式的序列号，必须还原成 ISO 日期而不是 46000 之类的整数
    assert header_page[2].cells == ("开票日期", "2026-03-05")


def test_vertical_merge_keeps_following_columns_aligned(fixtures_dir):
    text = SpreadsheetExtractor().extract(
        (fixtures_dir / "messy_invoice.xlsx").read_bytes(),
        filename="messy_invoice.xlsx",
    )

    rows = text.pages[0]
    header_cells = rows[5].cells
    assert header_cells is not None
    assert header_cells[:5] == ("序号", "物料名称", "规格", "单位", "数量")

    # I7:I8 纵向合并：第 8 行的 I 列被覆盖，但 A–H 各列不能左移
    second_row = rows[7].cells
    assert second_row is not None
    assert second_row[0] == "2"
    assert second_row[1] == "平垫片"
    assert second_row[3] == "个"
    assert second_row[4] == "2500"


def test_missing_header_sheet_degrades_to_unrecognized(pipeline, fixtures_dir):
    result = pipeline.parse_file(fixtures_dir / "missing_header.xlsx")

    # 没有表头就无法判断哪一列是「是什么、多少个」，不能凭位置猜
    assert result.status is ParseStatus.UNRECOGNIZED
    assert len(result.lines) == 0
    assert "未识别到明细行" in (result.message or "")


def test_broken_zip_raises_empty_document(pipeline):
    with pytest.raises(EmptyDocumentError):
        pipeline.parse_bytes(b"PK\x03\x04broken", filename="broken.xlsx")
