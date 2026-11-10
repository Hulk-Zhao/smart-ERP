"""XML 抽取：命名空间、重复明细行还原表格、属性字段、不安全文档拒绝。"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from apps.docparse.errors import EmptyDocumentError
from apps.docparse.extractors import XmlExtractor
from apps.docparse.models import DocType, ParseStatus


def test_namespaced_invoice_xml_is_parsed(pipeline, fixtures_dir):
    result = pipeline.parse_file(fixtures_dir / "invoice.xml")

    assert result.file_kind == "xml"
    assert result.doc_type is DocType.INVOICE
    assert result.status is ParseStatus.OK
    assert result.issues == []

    assert result.header.doc_no.value == "88231007"
    assert result.header.doc_date.value == date(2026, 3, 5)
    assert result.header.supplier_name.value == "东莞市恒泰五金制品有限公司"
    assert result.header.total_amount.value == Decimal("1299.50")

    assert len(result.lines) == 2
    assert result.lines[0].item_name.value == "不锈钢螺钉M6"
    assert result.lines[0].quantity.value == Decimal(1000)
    assert result.lines[1].amount.value == Decimal("300.00")


def test_repeated_elements_become_table_with_attribute_fields():
    payload = b"""
    <Invoice>
      <InvoiceNo>X-2026-01</InvoiceNo>
      <Lines>
        <Line sku="WL-1" qty="2" amount="4.00"/>
        <Line sku="WL-2" qty="3" amount="6.00"/>
      </Lines>
      <TotalAmount>10.00</TotalAmount>
    </Invoice>
    """

    text = XmlExtractor().extract(payload, filename="attrs.xml")
    lines = list(text.iter_lines())

    # 表头由标签/属性名归一而来，数据行列序与表头一致
    assert lines[1].cells == ("物料编码", "数量", "金额")
    assert lines[2].cells == ("WL-1", "2", "4.00")
    assert lines[3].cells == ("WL-2", "3", "6.00")


def test_missing_column_in_row_stays_empty_instead_of_shifting():
    payload = """
    <Statement>
      <Items>
        <Item><Name>垫片</Name><Qty>10</Qty><Amount>5.00</Amount></Item>
        <Item><Name>螺钉</Name><Amount>8.00</Amount></Item>
      </Items>
    </Statement>
    """.encode()

    text = XmlExtractor().extract(payload, filename="gap.xml")
    lines = list(text.iter_lines())

    assert lines[1].cells == ("垫片", "10", "5.00")
    # 第二行缺 Qty：数量列必须留空，Amount 不能左移到数量列
    assert lines[2].cells == ("螺钉", "", "8.00")


def test_dtd_is_rejected_as_unsafe(pipeline):
    payload = b'<?xml version="1.0"?><!DOCTYPE foo [<!ENTITY x "y">]><Invoice/>'

    with pytest.raises(EmptyDocumentError):
        pipeline.parse_bytes(payload, filename="unsafe.xml")


def test_malformed_xml_raises_empty_document(pipeline):
    with pytest.raises(EmptyDocumentError):
        pipeline.parse_bytes(b"<Invoice><Items>", filename="broken.xml")


def test_pipeline_supports_explicit_doc_type_for_xml(pipeline):
    payload = """
    <Root>
      <InvoiceNo>X-9</InvoiceNo>
      <InvoiceDate>2026-07-01</InvoiceDate>
      <SellerName>测试供应商</SellerName>
      <Items>
        <Item><Name>A</Name><Qty>1</Qty><Amount>2.00</Amount></Item>
        <Item><Name>B</Name><Qty>2</Qty><Amount>4.00</Amount></Item>
      </Items>
      <TotalAmount>6.00</TotalAmount>
    </Root>
    """.encode()

    result = pipeline.parse_bytes(
        payload, filename="no_title.xml", doc_type_hint=DocType.INVOICE
    )

    assert result.status is ParseStatus.OK
    assert result.header.doc_no.value == "X-9"
    assert len(result.lines) == 2
