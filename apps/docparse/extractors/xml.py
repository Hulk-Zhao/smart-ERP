"""XML 抽取器（标准库 ``xml.etree``）：常见发票 / 对账单结构。

不假设某一家供应商的 schema，只做两件事：

1. **标签语义归一**：``invoiceNo`` / ``InvoiceNumber`` / ``invoice_no`` 统一映射到
   中文标签「发票号码」，命名空间前缀、大小写、下划线与连字符都不影响；
2. **重复行元素还原为表格**：同一父节点下连续出现 ≥2 个同名元素、每个都带 ≥2 个
   叶子子节点、且至少一个值能解析为数字 → 视为明细行。第一行生成表头，后续生成
   数据行，列序按标签名对齐（缺列留空，不左移）。

认不出的标签按原样输出（``标签: 值``），交给字段映射层按标签字典取；取不到就缺失，
不做位置猜测。
"""

from __future__ import annotations

from xml.etree import ElementTree as ET

from ..detection import FileKind
from .base import DocumentText, TextLine

# 表头类标签 → 中文标签（必须命中 mapping/fields.py 的 HEADER_LABEL_PATTERNS）
_HEADER_TAGS: dict[str, str] = {
    "invoiceno": "发票号码",
    "invoicenumber": "发票号码",
    "invoiceid": "发票号码",
    "fphm": "发票号码",
    "billno": "单据编号",
    "billnumber": "单据编号",
    "docno": "单据编号",
    "documentno": "单据编号",
    "statementno": "对账单号",
    "deliveryno": "送货单号",
    "invoicedate": "开票日期",
    "issuedate": "开票日期",
    "billdate": "单据日期",
    "docdate": "单据日期",
    "seller": "销售方名称",
    "sellername": "销售方名称",
    "supplier": "供应商名称",
    "suppliername": "供应商名称",
    "vendor": "供应商名称",
    "vendorname": "供应商名称",
    "buyer": "购买方名称",
    "buyername": "购买方名称",
    "purchaser": "购买方名称",
    "purchasername": "购买方名称",
    "sellertaxno": "销售方纳税人识别号",
    "suppliertaxno": "供应商税号",
    "taxno": "纳税人识别号",
    "taxid": "纳税人识别号",
    "currency": "币种",
    "currencycode": "币种",
    "subtotal": "小计",
    "taxamount": "税额合计",
    "totaltax": "税额合计",
    "totalamount": "价税合计",
    "amountwithtax": "价税合计",
    "grandtotal": "价税合计",
    "total": "合计",
    "taxrate": "税率",
}

# 明细行子标签 → 中文列名（必须命中 mapping/fields.py 的 _COLUMN_ALIASES）
_LINE_TAGS: dict[str, str] = {
    "seq": "序号",
    "no": "序号",
    "index": "序号",
    "itemcode": "物料编码",
    "productcode": "物料编码",
    "goodscode": "物料编码",
    "sku": "物料编码",
    "code": "物料编码",
    "itemname": "物料名称",
    "productname": "物料名称",
    "goodsname": "物料名称",
    "name": "物料名称",
    "description": "物料名称",
    "spec": "规格",
    "specification": "规格",
    "model": "规格",
    "unit": "单位",
    "uom": "单位",
    "quantity": "数量",
    "qty": "数量",
    "count": "数量",
    "unitprice": "单价",
    "price": "单价",
    "amount": "金额",
    "lineamount": "金额",
    "taxrate": "税率",
    "taxamount": "税额",
}

# 这些标签的值单独成行（不带「标签:」前缀），供分类器识别标题
_TITLE_TAGS = frozenset({"title", "doctype", "documenttype", "type"})
_ROW_MIN_LEAVES = 2


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _key(tag: str) -> str:
    return _local(tag).lower().replace("_", "").replace("-", "")


def _clean(value: str | None) -> str:
    return (value or "").strip()


def _element_children(element: ET.Element) -> list[ET.Element]:
    return [child for child in element if isinstance(child.tag, str)]


def _leaf_items(element: ET.Element) -> list[tuple[str, str]]:
    """返回元素的叶子字段 ``[(标签, 值)]``：属性在前，无子元素的子节点在后。"""

    items: list[tuple[str, str]] = []
    for attribute, value in element.attrib.items():
        if _local(attribute).startswith("xmlns"):
            continue
        items.append((_local(attribute), _clean(value)))
    for child in _element_children(element):
        if _element_children(child):
            continue
        items.append((_local(child.tag), _clean(child.text)))
    return items


def _is_number(text: str) -> bool:
    try:
        float(text.replace(",", ""))
    except ValueError:
        return False
    return True


def _row_like(elements: list[ET.Element]) -> bool:
    """重复元素是否为「明细行」：每行 ≥2 个叶子值，且整组至少有一个数值。"""

    if len(elements) < 2:
        return False
    for element in elements:
        if len(_leaf_items(element)) < _ROW_MIN_LEAVES:
            return False
    return any(
        _is_number(value)
        for element in elements
        for _tag, value in _leaf_items(element)
    )


def _table_rows(elements: list[ET.Element]) -> list[list[str]]:
    """把重复元素展开成「表头 + 数据行」，列序以全部行出现过的标签为准。"""

    columns: list[str] = []
    for element in elements:
        for tag, _value in _leaf_items(element):
            if tag not in columns:
                columns.append(tag)
    if len(columns) < _ROW_MIN_LEAVES:
        return []

    header = [_LINE_TAGS.get(_key(column), _local(column)) for column in columns]
    rows: list[list[str]] = [header]
    for element in elements:
        values = {tag: value for tag, value in _leaf_items(element)}
        rows.append([values.get(column, "") for column in columns])
    return rows


class XmlExtractor:
    """按「标签归一 + 重复行还原表格」抽取，单页输出。"""

    name = "xml.etree.v1"

    def extract(self, source: bytes, *, filename: str = "") -> DocumentText:
        # XML 来自不可信上传：直接拒绝带 DTD / 实体声明的文档，避免实体膨胀攻击
        if b"<!doctype" in source.lower() or b"<!entity" in source.lower():
            raise ValueError(f"XML 含 DTD/实体声明，按不安全文档拒绝：{filename!r}")
        try:
            root = ET.fromstring(source)
        except ET.ParseError as error:
            raise ValueError(f"XML 文件无法解析：{filename!r}") from error

        rows: list[list[str]] = []
        for attribute, value in root.attrib.items():
            if _local(attribute).startswith("xmlns"):
                continue
            rows.append([f"{self._label(attribute)}: {_clean(value)}"])
        self._walk(root, rows)

        lines = [
            TextLine(
                page=1,
                index=index,
                text=" | ".join(row) if len(row) > 1 else row[0],
                # 明细/表头行保留单元格列位，缺列时不会被后一列顶上来
                cells=tuple(row) if len(row) > 1 else None,
            )
            for index, row in enumerate(rows)
            if row
        ]
        return DocumentText(pages=(tuple(lines),), extractor=self.name, kind=FileKind.XML)

    def _walk(self, parent: ET.Element, rows: list[list[str]]) -> None:
        children = _element_children(parent)
        position = 0
        while position < len(children):
            tag = _local(children[position].tag)
            run: list[ET.Element] = []
            # ElementTree 保持文档顺序，同名字段在 XML 里通常连续出现
            while position < len(children) and _local(children[position].tag) == tag:
                run.append(children[position])
                position += 1

            if _row_like(run):
                rows.extend(_table_rows(run))
                continue

            for child in run:
                self._emit(child, rows)

    def _emit(self, element: ET.Element, rows: list[list[str]]) -> None:
        if _element_children(element):
            self._walk(element, rows)
            return

        value = _clean(element.text)
        if not value:
            for attribute, attribute_value in element.attrib.items():
                if _local(attribute).startswith("xmlns"):
                    continue
                rows.append([f"{self._label(attribute)}: {_clean(attribute_value)}"])
            return

        tag = _key(element.tag)
        if tag in _TITLE_TAGS:
            rows.append([value])
            return
        rows.append([f"{self._label(element.tag)}: {value}"])

    @staticmethod
    def _label(tag: str) -> str:
        key = _key(tag)
        return _HEADER_TAGS.get(key, _LINE_TAGS.get(key, _local(tag)))
