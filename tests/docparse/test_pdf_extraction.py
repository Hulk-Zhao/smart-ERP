"""真实 PDF 文本层抽取（pypdfium2，可选 ``[pdf]`` extra）。

缺 extra 时整模块跳过；未安装依赖的降级路径见 ``test_pdf_optional_dependency.py``。
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from apps.docparse import DocParsePipeline, DocType, ParseStatus

pytest.importorskip("pypdfium2", reason="未安装 [pdf] extra，跳过真实 PDF 用例")


def test_invoice_fields_and_char_bbox(fixtures_dir):
    """中文发票：字段值正确，且坐标来自 get_charbox 的真实字符框。"""

    result = DocParsePipeline().parse_file(fixtures_dir / "invoice_zh.pdf")

    assert result.file_kind == "pdf"
    assert result.doc_type is DocType.INVOICE
    assert result.status is ParseStatus.OK
    assert result.parser.startswith("pdf.pypdfium2.v1")

    assert result.header.doc_no.value == "88231007"
    assert result.header.doc_date.value.isoformat() == "2026-04-01"
    assert result.header.supplier_name.value == "蓝票科技有限公司"
    assert result.header.total_amount.value == Decimal("113.00")
    assert result.lines[0].item_name.value == "测试物料"
    assert result.lines[0].quantity.value == Decimal(10)
    assert result.lines[0].amount.value == Decimal("100.00")

    coord = result.header.doc_no.coords[0]
    assert coord.page == 1
    x0, y0, x1, y1 = coord.bbox
    # 样本里号码首位 x≈122.5；离线估算会从 40 + 偏移开始，不会落在这一带
    assert 118 <= x0 <= 128
    assert 78 <= y0 <= 90  # 已由 PDF 左下原点换算成左上原点
    assert x1 > x0 and y1 > y0


def test_scanned_pdf_reports_ocr_needed(fixtures_dir):
    """无文本层（扫描件）必须明确报缺 OCR，不能静默返回空文本。"""

    result = DocParsePipeline().parse_file(fixtures_dir / "scanned_no_text.pdf")

    assert result.status is ParseStatus.UNRECOGNIZED
    assert result.doc_type is DocType.UNKNOWN
    assert "文本层" in result.message
    assert "OCR" in result.message
    assert "PaddleOCR" in result.message
