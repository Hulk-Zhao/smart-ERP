"""CSV 真抽取：引号、BOM、分隔符、编码探测。"""

from __future__ import annotations

from apps.docparse.extractors import CsvExtractor, detect_delimiter
from apps.docparse.models import DocType, ParseStatus


def test_quoted_semicolon_csv_with_bom(pipeline, fixtures_dir):
    result = pipeline.parse_file(fixtures_dir / "quoted_statement.csv")

    assert result.file_kind == "csv"
    assert result.doc_type is DocType.STATEMENT
    assert result.status is ParseStatus.OK
    # BOM 必须被吃掉，否则第一个字段会带 \ufeff 前缀导致表头匹配失败
    assert result.header.doc_no.value == "DZ-2026-0601"
    # 引号内的分号属于值本身，不能被当成分隔符
    assert result.header.supplier_name.value == "恒泰五金;东莞厂"
    # 引号内的逗号同理
    assert result.lines[0].item_name.value == "不锈钢螺钉, M6"
    assert result.lines[0].quantity.value == 100


def test_quoted_newline_keeps_one_logical_row():
    payload = (
        "供应商名称;测试供应商\n"
        "序号;物料名称;数量;单价;金额\n"
        '1;"不锈钢\n螺钉";100;0.85;85.00\n'
    ).encode()

    text = CsvExtractor().extract(payload, filename="multi.csv")
    lines = list(text.iter_lines())

    assert len(lines) == 3
    assert lines[-1].cells is not None
    assert lines[-1].cells[1] == "不锈钢\n螺钉"
    assert lines[-1].cells[2] == "100"


def test_gb18030_encoded_csv_is_decoded():
    payload = "供应商名称: 东莞市恒泰五金制品有限公司\n序号;物料名称;数量\n".encode("gb18030")

    text = CsvExtractor().extract(payload, filename="gbk.csv")
    lines = list(text.iter_lines())

    assert lines[0].text == "供应商名称: 东莞市恒泰五金制品有限公司"


def test_utf16_bom_csv_is_decoded():
    payload = "序号;物料名称;数量\n1;垫片;20\n".encode("utf-16")

    text = CsvExtractor().extract(payload, filename="utf16.csv")
    lines = list(text.iter_lines())

    assert lines[0].cells == ("序号", "物料名称", "数量")
    assert lines[1].cells == ("1", "垫片", "20")


def test_detect_delimiter_prefers_consistent_column_count():
    assert detect_delimiter("a,b,c\n1,2,3\n") == ","
    assert detect_delimiter("a;b;c\n1;2;3\n") == ";"
    assert detect_delimiter("a\tb\tc\n1\t2\t3\n") == "\t"
    assert detect_delimiter("a|b|c\n1|2|3\n") == "|"


def test_detect_delimiter_ignores_separators_inside_quotes():
    text = '供应商名称;"恒泰五金;东莞厂"\n物料名称;数量\n"垫片, 大";20\n'

    assert detect_delimiter(text) == ";"


def test_single_column_file_uses_default_delimiter():
    assert detect_delimiter("供应商对账单\n") == ","
