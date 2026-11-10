"""管线降级与可插拔性：桩抽取器、拒收、CSV、低置信、人工指定类型。"""

from __future__ import annotations

from decimal import Decimal

import pytest

from apps.docparse import (
    DocParsePipeline,
    DocType,
    EmptyDocumentError,
    ParseStatus,
    UnsupportedFileTypeError,
)
from apps.docparse.detection import FileKind
from apps.docparse.extractors import PlainTextExtractor


def test_image_stub_degrades_to_unrecognized(tmp_path):
    image = tmp_path / "scan.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 32)

    result = DocParsePipeline().parse_file(image)

    assert result.status is ParseStatus.UNRECOGNIZED
    assert "OCR" in result.message


def test_unsupported_file_type_is_rejected(tmp_path):
    exe = tmp_path / "payload.exe"
    exe.write_bytes(b"MZ\x90\x00")

    with pytest.raises(UnsupportedFileTypeError):
        DocParsePipeline().parse_file(exe)


def test_empty_file_is_rejected(tmp_path):
    empty = tmp_path / "empty.txt"
    empty.write_bytes(b"")

    with pytest.raises(EmptyDocumentError):
        DocParsePipeline().parse_file(empty)


def test_custom_extractor_can_replace_stub(tmp_path):
    """接入真实 PDF 抽取器时只需实现协议并注册，后续阶段不变。"""

    class FakePdfExtractor:
        name = "fake-pdf.v1"

        def extract(self, source: bytes, *, filename: str = ""):
            # 模拟已接入的 PDF 抽取器抽出文本层：复用文本抽取器解析同一份内容
            return PlainTextExtractor().extract(
                (tmp_path / "invoice.txt").read_bytes(), filename="invoice.txt"
            )

    (tmp_path / "invoice.txt").write_text(
        "增值税专用发票\n"
        "发票号码: 9001\n"
        "开票日期: 2026-04-01\n"
        "销售方名称: 测试供应商\n"
        "价税合计: 113.00\n"
        "序号 | 物料名称 | 单位 | 数量 | 单价 | 金额\n"
        "1 | 测试物料 | 个 | 10 | 10.00 | 100.00\n",
        encoding="utf-8",
    )
    pdf = tmp_path / "scan.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")

    pipeline = DocParsePipeline(extractors={FileKind.PDF: FakePdfExtractor()})
    result = pipeline.parse_file(pdf)

    assert result.status is ParseStatus.OK
    assert result.doc_type is DocType.INVOICE
    assert result.header.doc_no.value == "9001"
    assert result.parser.startswith("fake-pdf.v1")


def test_csv_statement_can_be_parsed(tmp_path):
    csv_path = tmp_path / "statement.csv"
    csv_path.write_text(
        "供应商对账单\n"
        "对账单号,DZ-2026-03-001\n"
        "对账日期,2026-03-05\n"
        "供应商名称,测试供应商\n"
        "应付金额,100.00\n"
        "\n"
        "序号,物料名称,单位,数量,单价,金额\n"
        "1,测试物料,个,10,10.00,100.00\n",
        encoding="utf-8",
    )

    result = DocParsePipeline().parse_file(csv_path)

    assert result.file_kind == "csv"
    assert result.doc_type is DocType.STATEMENT
    assert result.status is ParseStatus.OK
    assert result.lines[0].amount.value == Decimal("100.00")


def test_low_confidence_amount_is_flagged(tmp_path):
    doc = tmp_path / "weak_invoice.txt"
    doc.write_text(
        "增值税普通发票\n"
        "发票号码: 7001\n"
        "开票日期: 2026-05-01\n"
        "销售方名称: 测试供应商\n"
        "价税合计: 100.00\n"
        "序号 | 物料名称 | 单位 | 数量 | 单价 | 金额\n"
        "1 | 测试物料 | 个 | 10 | 2.00 | 25.00\n",
        encoding="utf-8",
    )

    result = DocParsePipeline().parse_file(doc)

    assert result.status is ParseStatus.NEEDS_REVIEW
    assert "lines[1].amount" in result.low_confidence_fields
    assert result.lines[0].amount.confidence < 0.85
    assert any(issue.code == "LINE_AMOUNT_MISMATCH" for issue in result.issues)
    assert "置信度" in result.message


def test_unknown_doc_type_requires_manual_choice(tmp_path):
    doc = tmp_path / "note.txt"
    doc.write_text("会议记录\n参会人: 张三\n今天讨论了排产计划\n", encoding="utf-8")

    result = DocParsePipeline().parse_file(doc)

    assert result.status is ParseStatus.UNRECOGNIZED
    assert result.doc_type is DocType.UNKNOWN
    assert "人工选择类型" in result.message


def test_doc_type_hint_bypasses_classifier(fixtures_dir):
    result = DocParsePipeline().parse_file(
        fixtures_dir / "clear_invoice.txt", doc_type_hint=DocType.INVOICE
    )

    assert result.doc_type is DocType.INVOICE
    assert result.doc_type_confidence == 1.0
