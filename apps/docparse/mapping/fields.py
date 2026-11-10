"""字段映射（管线第 4 步）：规则版表头/明细行映射。

规则来源：PRD FR-402 要求抽取的关键字段（单号、日期、供应商、物料、数量、单价、
金额、税额）。真实项目可替换为版面模型 / VLM 兜底，只要实现 ``FieldMapper`` 协议。

设计原则：
- 只按文档中显式出现的标签和表格列取值，取不到就留空；
- 空单元格 → ``None``，不写 0；
- 明细金额仅在「数量 + 单价都有、金额为空」时按乘法推导，并标注 ``source=derived``；
- 无法解析的原值写成 ``VALUE_UNPARSABLE`` 警告，不猜测。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Protocol

from ..detection import is_page_marker
from ..extractors.base import DocumentText, TextLine, line_bbox
from ..models import (
    HEADER_FIELD_LABELS,
    LINE_FIELD_LABELS,
    DateField,
    DecimalField,
    DocHeader,
    DocLine,
    DocType,
    FieldSource,
    FieldValue,
    IssueSeverity,
    StrField,
    ValidationIssue,
)

CENT = Decimal("0.01")


# ---------------------------------------------------------------------------
# 值解析
# ---------------------------------------------------------------------------

_NUM_CLEAN = re.compile(r"[,\s¥￥$元]")
_NUM_VALID = re.compile(r"^-?\d+(?:\.\d+)?$")
_DATE_DASHED = re.compile(r"^(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})$")
_DATE_CN = re.compile(r"^(\d{4})年(\d{1,2})月(\d{1,2})日$")

_SEPARATORS = ":：|,，\t 　"


def parse_decimal(raw: str) -> Decimal | None:
    """解析金额/数量。只接受纯数字（可带千分位、货币符号），其余返回 None。"""

    text = _NUM_CLEAN.sub("", raw.strip())
    if not _NUM_VALID.match(text):
        return None
    try:
        return Decimal(text)
    except InvalidOperation:
        return None


def parse_rate(raw: str) -> Decimal | None:
    """税率归一化为小数：``13%`` → ``0.13``，``13`` 视为 13%。"""

    text = raw.strip()
    percent = text.endswith("%")
    value = parse_decimal(text[:-1] if percent else text)
    if value is None:
        return None
    if percent or value > 1:
        return (value / 100).quantize(Decimal("0.0001"))
    return value


def parse_date(raw: str) -> date | None:
    text = raw.strip().replace(" ", "")
    for pattern in (_DATE_DASHED, _DATE_CN):
        matched = pattern.match(text)
        if not matched:
            continue
        year, month, day = (int(part) for part in matched.groups())
        try:
            return date(year, month, day)
        except ValueError:
            return None
    return None


# ---------------------------------------------------------------------------
# 表头字段规则
# ---------------------------------------------------------------------------

HEADER_LABEL_PATTERNS: dict[str, tuple[str, ...]] = {
    "doc_no": ("发票号码", "发票号", "送货单号", "对账单号", "领料单号", "单据编号", "单号"),
    "doc_date": ("开票日期", "送货日期", "对账日期", "领料日期", "单据日期", "日期"),
    "supplier_name": ("销售方名称", "供应商名称", "供货单位", "销方名称", "供应商", "销售方"),
    "supplier_tax_no": (
        "销售方纳税人识别号",
        "销方纳税人识别号",
        "供应商税号",
        "纳税人识别号",
    ),
    "buyer_name": ("购买方名称", "购方名称", "收货单位", "采购方名称"),
    "currency": ("币种", "货币"),
    "subtotal": ("小计金额", "金额合计", "合计金额", "小计"),
    "tax_amount": ("税额合计", "合计税额"),
    "total_amount": ("价税合计", "应付金额", "金额总计", "总计", "合计"),
    "tax_rate": ("税率",),
}

# 长标签优先，避免「税额合计」被「合计」抢走
_LABEL_ORDER: tuple[tuple[str, str], ...] = tuple(
    sorted(
        ((label, field) for field, labels in HEADER_LABEL_PATTERNS.items() for label in labels),
        key=lambda item: len(item[0]),
        reverse=True,
    )
)

_DATE_FIELDS = {"doc_date"}
_DECIMAL_FIELDS = {"subtotal", "tax_amount", "total_amount", "tax_rate"}


def _find_label(text: str) -> tuple[str, str, int] | None:
    """返回 (标签, 字段名, 标签起始位置)。标签后必须是分隔符或数字。"""

    for label, field in _LABEL_ORDER:
        position = text.find(label)
        if position < 0:
            continue
        end = position + len(label)
        if end < len(text):
            following = text[end]
            if following not in _SEPARATORS and not following.isdigit():
                continue
        return label, field, position
    return None


def _split_value(text: str, start: int) -> tuple[str, int]:
    """取标签后的值，返回 (值, 值起始字符位置)。"""

    index = start
    while index < len(text) and text[index] in _SEPARATORS:
        index += 1
    value = text[index:].strip()
    return value, index


def _default_header_field(name: str) -> FieldValue[Any]:
    if name in _DATE_FIELDS:
        return DateField(value=None, confidence=0.0)
    if name in _DECIMAL_FIELDS:
        return DecimalField(value=None, confidence=0.0)
    return StrField(value=None, confidence=0.0)


def _build_header_field(
    name: str,
    raw: str,
    line: TextLine,
    value_start: int,
    confidence: float,
) -> FieldValue[Any] | None:
    coords = [line_bbox(line, value_start, value_start + len(raw))]
    raw_text = line.text.strip()

    if name in _DATE_FIELDS:
        parsed: Any = parse_date(raw)
        model = DateField
    elif name in _DECIMAL_FIELDS:
        parsed = parse_rate(raw) if name == "tax_rate" else parse_decimal(raw)
        model = DecimalField
    else:
        parsed = raw.strip()
        model = StrField

    if parsed is None or parsed == "":
        return None
    return model(value=parsed, confidence=confidence, coords=coords, raw_text=raw_text)


# ---------------------------------------------------------------------------
# 明细表格规则
# ---------------------------------------------------------------------------

_COLUMN_ALIASES: tuple[tuple[str, str], ...] = (
    ("物料编码", "item_code"),
    ("物料号", "item_code"),
    ("编码", "item_code"),
    ("物料名称", "item_name"),
    ("品名", "item_name"),
    ("名称", "item_name"),
    ("物料", "item_name"),
    ("规格型号", "spec"),
    ("规格", "spec"),
    ("型号", "spec"),
    ("单位", "unit"),
    ("数量", "quantity"),
    ("单价", "unit_price"),
    ("金额", "amount"),
    ("小计", "amount"),
    ("税率", "tax_rate"),
    ("税额", "tax_amount"),
    ("序号", "seq"),
    ("项次", "seq"),
)

_LINE_NUMERIC_FIELDS = ("quantity", "unit_price", "amount", "tax_rate", "tax_amount")
_TABLE_STOP_KEYWORDS = (
    "合计",
    "小计",
    "总计",
    "价税",
    "税额",
    "签收",
    "备注",
    "大写",
    "期末",
    "期初",
    "应付",
)


@dataclass(frozen=True)
class Token:
    value: str
    start: int
    end: int


def _append_trimmed(tokens: list[Token], text: str, start: int, end: int) -> None:
    segment = text[start:end]
    stripped = segment.strip()
    if not stripped:
        return
    lead = len(segment) - len(segment.lstrip())
    tokens.append(Token(value=stripped, start=start + lead, end=start + lead + len(stripped)))


def split_tokens(text: str) -> list[Token]:
    """按 ``|`` → ``,`` → 空白 的优先级切分，并保留字符偏移用于坐标回链。"""

    for separator in ("|", ","):
        if separator in text:
            tokens: list[Token] = []
            start = 0
            for index, char in enumerate(text):
                if char == separator:
                    _append_trimmed(tokens, text, start, index)
                    start = index + 1
            _append_trimmed(tokens, text, start, len(text))
            return tokens

    return [
        Token(value=match.group(), start=match.start(), end=match.end())
        for match in re.finditer(r"\S+", text)
    ]


def _canonical_column(token: Token) -> str | None:
    text = token.value.strip().strip("：:")
    for alias, field in _COLUMN_ALIASES:
        if alias in text:
            return field
    return None


def _table_header_map(line: TextLine) -> dict[str, int] | None:
    columns: dict[str, int] = {}
    for index, token in enumerate(split_tokens(line.text)):
        field = _canonical_column(token)
        if field and field not in columns:
            columns[field] = index
    if len(columns) < 3:
        return None
    has_measure = "quantity" in columns or "amount" in columns or "unit_price" in columns
    has_item = "item_name" in columns or "item_code" in columns
    return columns if has_measure and has_item else None


def _table_stop(text: str) -> bool:
    first = split_tokens(text)
    head = first[0].value if first else text.strip()
    return head.startswith(_TABLE_STOP_KEYWORDS)


def _row_has_numbers(tokens: list[Token], columns: dict[str, int]) -> bool:
    for field in _LINE_NUMERIC_FIELDS:
        index = columns.get(field)
        if index is None or index >= len(tokens):
            continue
        parser = parse_rate if field == "tax_rate" else parse_decimal
        if parser(tokens[index].value) is not None:
            return True
    return False


def _empty_line_field(name: str) -> FieldValue[Any]:
    if name in ("quantity", "unit_price", "amount", "tax_rate", "tax_amount"):
        return DecimalField(value=None, confidence=0.0)
    return StrField(value=None, confidence=0.0)


def _build_line(
    line: TextLine,
    tokens: list[Token],
    columns: dict[str, int],
    fallback_no: int,
) -> tuple[DocLine, list[ValidationIssue]]:
    issues: list[ValidationIssue] = []
    values: dict[str, FieldValue[Any]] = {}
    line_no = fallback_no

    seq_index = columns.get("seq")
    if seq_index is not None and seq_index < len(tokens):
        seq_token = tokens[seq_index].value.strip()
        if seq_token.isdigit():
            line_no = int(seq_token)

    for name in LINE_FIELD_LABELS:
        index = columns.get(name)
        if index is None or index >= len(tokens):
            values[name] = _empty_line_field(name)
            continue
        token = tokens[index]
        raw = token.value.strip()
        if not raw:
            values[name] = _empty_line_field(name)
            continue

        coords = [line_bbox(line, token.start, token.end)]
        if name in _LINE_NUMERIC_FIELDS:
            parsed = parse_rate(raw) if name == "tax_rate" else parse_decimal(raw)
            if parsed is None:
                values[name] = _empty_line_field(name)
                issues.append(
                    ValidationIssue(
                        code="VALUE_UNPARSABLE",
                        severity=IssueSeverity.WARNING,
                        message=f"明细第 {fallback_no} 行的{LINE_FIELD_LABELS[name]}“{raw}”无法解析",
                        field_path=f"lines[{fallback_no}].{name}",
                    )
                )
                continue
            values[name] = DecimalField(
                value=parsed,
                confidence=0.92,
                coords=coords,
                raw_text=line.text.strip(),
            )
        else:
            values[name] = StrField(
                value=raw,
                confidence=0.92,
                coords=coords,
                raw_text=line.text.strip(),
            )

    # 金额可由「数量 × 单价」推导，但必须标注来源，便于复核
    quantity = values["quantity"]
    unit_price = values["unit_price"]
    amount = values["amount"]
    if amount.is_missing and quantity.value is not None and unit_price.value is not None:
        derived = (quantity.value * unit_price.value).quantize(CENT, rounding=ROUND_HALF_UP)
        values["amount"] = DecimalField(
            value=derived,
            confidence=0.88,
            coords=[*quantity.coords, *unit_price.coords],
            source=FieldSource.DERIVED,
        )
        issues.append(
            ValidationIssue(
                code="AMOUNT_DERIVED",
                severity=IssueSeverity.WARNING,
                message=f"明细第 {fallback_no} 行金额缺失，已按数量×单价推导，需复核",
                field_path=f"lines[{fallback_no}].amount",
                expected=derived,
            )
        )

    return DocLine(page=line.page, line_no=line_no, **values), issues


# ---------------------------------------------------------------------------
# 管线中间结果
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MappedDocument:
    header: DocHeader
    lines: tuple[DocLine, ...]
    issues: tuple[ValidationIssue, ...]


class FieldMapper(Protocol):
    name: str

    def map(self, text: DocumentText, doc_type: DocType) -> MappedDocument:
        ...


class RuleBasedFieldMapper:
    """按标签字典 + 表格列头映射字段；不做任何补全猜测。"""

    name = "rule-fields.v1"

    def map(self, text: DocumentText, doc_type: DocType) -> MappedDocument:
        lines = list(text.iter_lines())
        table_lines, sealed = self._map_tables(lines)
        header, header_issues = self._map_header(lines, sealed)

        issues: list[ValidationIssue] = []
        for line in table_lines:
            issues.extend(line[1])
        issues.extend(header_issues)

        return MappedDocument(
            header=header,
            lines=tuple(line[0] for line in table_lines),
            issues=tuple(issues),
        )

    def _map_header(
        self, lines: list[TextLine], sealed: set[tuple[int, int]]
    ) -> tuple[DocHeader, list[ValidationIssue]]:
        produced: dict[str, FieldValue[Any]] = {}
        issues: list[ValidationIssue] = []

        for line in lines:
            if (line.page, line.index) in sealed:
                continue
            text = line.text.strip()
            if not text or is_page_marker(text):
                continue
            matched = _find_label(text)
            if matched is None:
                continue
            _label, field, position = matched
            if field in produced:
                continue
            raw_value, value_start = _split_value(text, position + len(_label))
            if not raw_value:
                continue
            value = _build_header_field(
                field,
                raw_value,
                line,
                value_start,
                confidence=0.98 if position == 0 else 0.90,
            )
            if value is None:
                issues.append(
                    ValidationIssue(
                        code="VALUE_UNPARSABLE",
                        severity=IssueSeverity.WARNING,
                        message=f"{HEADER_FIELD_LABELS[field]}“{raw_value}”无法解析",
                        field_path=f"header.{field}",
                    )
                )
                continue
            produced[field] = value

        values = {
            name: produced[name] if name in produced else _default_header_field(name)
            for name in HEADER_FIELD_LABELS
        }
        return DocHeader(**values), issues

    def _map_tables(
        self, lines: list[TextLine]
    ) -> tuple[list[tuple[DocLine, list[ValidationIssue]]], set[tuple[int, int]]]:
        mapped: list[tuple[DocLine, list[ValidationIssue]]] = []
        sealed: set[tuple[int, int]] = set()
        fallback_no = 0
        index = 0

        while index < len(lines):
            columns = _table_header_map(lines[index])
            if columns is None:
                index += 1
                continue

            sealed.add((lines[index].page, lines[index].index))
            cursor = index + 1
            while cursor < len(lines):
                row = lines[cursor]
                row_text = row.text.strip()
                if not row_text or is_page_marker(row_text):
                    break
                if _table_header_map(row) is not None or _table_stop(row_text):
                    break
                tokens = split_tokens(row.text)
                if not _row_has_numbers(tokens, columns):
                    break
                fallback_no += 1
                doc_line, row_issues = _build_line(row, tokens, columns, fallback_no)
                mapped.append((doc_line, row_issues))
                sealed.add((row.page, row.index))
                cursor += 1

            index = cursor

        return mapped, sealed
