"""供应商对账单 CSV → 对账行级数据集（库内能力，无路由）。

边界与真话：

- 文件通道只能拿到**供应商侧**数据，所以产物固定是 ``supplier_lines``；
  ``our_lines`` 恒为空，需由 ERP/MES/WMS 另提供，不在这里补 0 或伪造。
- 解析 fail-closed：缺列、金额/数量解析失败、数量为空、单位不认识、重复行，
  一律收集到 ``StatementCsvError.issues`` 并拒收整个文件，带文件真实行号，
  不静默跳过、不补 0。
- 本模块不读目录、不连数据库、不挂路由；接线（``ERP_FILE_INBOX`` 读端点、
  dataset 登记、API 取数）见 ``apps/docparse/README.md`` 的建议清单。

输出行字段与 ``apps.api.schemas.ReconLineIn`` 一一对应，可直接放进
``ReconRunIn.dataset.supplier_lines``，也可以交给 ``apps.recon.loader.build_request``。
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from .errors import DocParseError
from .extractors.csv import CsvExtractor
from .mapping.fields import parse_decimal_detailed

#: 与文件通道连接器（apps.connectors.file.spec.SYSTEM_NAME）保持一致
SYSTEM_NAME = "file"

#: 必须有值的列；缺任一列整文件拒收
REQUIRED_COLUMNS: tuple[str, ...] = ("doc_no", "supplier_code")

#: 至少命中一个的列组（物料标识、数量、单位、金额口径）
REQUIRED_GROUPS: tuple[tuple[str, ...], ...] = (
    ("item_code", "item_name"),
    ("qty",),
    ("unit",),
    ("amount", "unit_price"),
)

#: 单位别名 → 规范单位；只合并语义相同的写法，不把「件/个」「箱/包」混为一谈。
#: 键按 NFKC + casefold 归一后比较。
_UNIT_ALIASES: dict[str, str] = {
    "个": "PCS",
    "只": "PCS",
    "支": "PCS",
    "pcs": "PCS",
    "pc": "PCS",
    "套": "SET",
    "set": "SET",
    "千克": "KG",
    "公斤": "KG",
    "kg": "KG",
    "kgs": "KG",
    "克": "G",
    "g": "G",
    "吨": "T",
    "t": "T",
    "米": "M",
    "m": "M",
    "毫米": "MM",
    "mm": "MM",
    "平方米": "M2",
    "m2": "M2",
    "立方米": "M3",
    "m3": "M3",
    "升": "L",
    "l": "L",
    "毫升": "ML",
    "ml": "ML",
    "箱": "BOX",
    "box": "BOX",
    "卷": "ROLL",
    "roll": "ROLL",
    "双": "PAIR",
    "对": "PAIR",
    "pair": "PAIR",
    "包": "PACK",
    "pack": "PACK",
    "张": "SHEET",
    "sheet": "SHEET",
}

#: 允许的单位规范值（文档与错误提示用）
KNOWN_UNITS: tuple[str, ...] = tuple(sorted(set(_UNIT_ALIASES.values())))


@dataclass(frozen=True, slots=True)
class StatementRowIssue:
    """一条可回指文件行号的映射错误。"""

    line_no: int
    field: str
    value: str
    code: str
    message: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "line_no": self.line_no,
            "field": self.field,
            "value": self.value,
            "code": self.code,
            "message": self.message,
        }


class StatementCsvError(DocParseError):
    """对账单 CSV 映射失败；``issues`` 含全部行级错误，不是遇到第一条就停。"""

    def __init__(
        self,
        filename: str,
        issues: tuple[StatementRowIssue, ...],
        *,
        missing_columns: tuple[str, ...] = (),
        reason: str | None = None,
    ) -> None:
        self.filename = filename
        self.issues = issues
        self.missing_columns = missing_columns
        parts: list[str] = []
        if missing_columns:
            parts.append(f"缺少必需列: {', '.join(missing_columns)}")
        parts.extend(
            f"第 {issue.line_no} 行 {issue.field}={issue.value!r}：{issue.message}"
            f"（{issue.code}）"
            for issue in issues
        )
        detail = "；".join(parts) if parts else (reason or "文件为空或没有数据行")
        super().__init__(f"拒绝对账单 {filename!r}：{detail}")


@dataclass(frozen=True, slots=True)
class SupplierStatement:
    """映射结果：只有供应商侧行，``our_lines`` 固定为空。"""

    source_file: str
    supplier_lines: tuple[dict[str, Any], ...]
    our_lines: tuple[dict[str, Any], ...] = ()
    ignored_columns: tuple[str, ...] = ()

    def as_dataset(self) -> dict[str, list[dict[str, Any]]]:
        """转成 ``ReconDatasetIn`` 可接受的 dataset（our_lines 明确为空）。"""

        return {
            "our_lines": [dict(line) for line in self.our_lines],
            "supplier_lines": [dict(line) for line in self.supplier_lines],
        }


def normalize_unit(raw: str) -> str | None:
    """单位规范化（NFKC + 大小写 + 别名）；不认识返回 ``None``。"""

    key = unicodedata.normalize("NFKC", raw).strip().casefold()
    if not key:
        return None
    return _UNIT_ALIASES.get(key)


def _parse_number(
    raw: str, *, line_no: int, field: str, issues: list[StatementRowIssue]
) -> Decimal | None:
    parsed = parse_decimal_detailed(raw)
    if parsed.value is None:
        issues.append(
            StatementRowIssue(
                line_no=line_no,
                field=field,
                value=raw,
                code="E_BAD_NUMBER",
                message="无法解析为数字（不补 0）",
            )
        )
    return parsed.value


def _parse_bool(
    raw: str, *, line_no: int, issues: list[StatementRowIssue]
) -> bool:
    text = unicodedata.normalize("NFKC", raw).strip().casefold()
    if text in {"true", "1", "yes", "y", "是"}:
        return True
    if text in {"false", "0", "no", "n", "否"}:
        return False
    issues.append(
        StatementRowIssue(
            line_no=line_no,
            field="tax_included",
            value=raw,
            code="E_BAD_BOOL",
            message="只能是 true/false/1/0/是/否",
        )
    )
    return True


def _normalize_header(cells: tuple[str, ...]) -> list[str]:
    return [
        unicodedata.normalize("NFKC", cell).replace("\ufeff", "").strip().lower()
        for cell in cells
    ]


def _collect_missing_columns(header: list[str]) -> tuple[str, ...]:
    missing = [column for column in REQUIRED_COLUMNS if column not in header]
    for group in REQUIRED_GROUPS:
        if not any(column in header for column in group):
            missing.append(" 或 ".join(group))
    return tuple(missing)


def map_supplier_statement_csv(
    source: bytes,
    *,
    filename: str,
    default_period: str | None = None,
) -> SupplierStatement:
    """把供应商对账单 CSV 映射为 ``ReconLineIn`` 字段结构的行。

    出错抛 ``StatementCsvError``（含全部行号与原因），不做部分输出。
    """

    try:
        document = CsvExtractor().extract(source, filename=filename)
    except ValueError as exc:
        raise StatementCsvError(filename, (), reason=f"读取失败：{exc}") from exc

    lines = document.pages[0]
    if not lines:
        raise StatementCsvError(filename, (), reason="文件为空或没有表头")

    header = _normalize_header(lines[0].cells or ())
    if not any(header):
        raise StatementCsvError(filename, ())
    duplicated = sorted({column for column in header if header.count(column) > 1})
    if duplicated:
        issues = tuple(
            StatementRowIssue(
                line_no=1,
                field=column,
                value=column,
                code="E_HEADER_DUPLICATE",
                message="表头列名重复",
            )
            for column in duplicated
        )
        raise StatementCsvError(filename, issues)
    missing = _collect_missing_columns(header)
    if missing:
        raise StatementCsvError(filename, (), missing_columns=missing)

    ignored = tuple(
        column for column in header if column and column not in _MAPPED_COLUMNS
    )
    issues: list[StatementRowIssue] = []
    rows: list[dict[str, Any]] = []
    seen_line: dict[tuple[str, int], int] = {}
    seen_row: dict[tuple[Any, ...], int] = {}

    for offset, line in enumerate(lines[1:], start=2):
        cells = line.cells or ()
        if not any(cell.strip() for cell in cells):
            continue  # 空行不是数据行，不计入缺失
        if len(cells) != len(header):
            issues.append(
                StatementRowIssue(
                    line_no=offset,
                    field="<row>",
                    value=line.text,
                    code="E_COLUMN_COUNT",
                    message=f"列数不符：表头 {len(header)} 列，本行 {len(cells)} 列",
                )
            )
            continue
        row = dict(zip(header, (cell.strip() for cell in cells), strict=True))
        mapped = _map_row(
            row, line_no=offset, filename=filename, default_period=default_period,
            issues=issues,
        )
        if mapped is None:
            continue
        key = (mapped["doc_no"], mapped["line_no"])
        if key in seen_line:
            issues.append(
                StatementRowIssue(
                    line_no=offset,
                    field="line_no",
                    value=str(mapped["line_no"]),
                    code="E_DUPLICATE_LINE",
                    message=f"与第 {seen_line[key]} 行的 (doc_no, line_no) 重复",
                )
            )
            continue
        seen_line[key] = offset
        fingerprint = (
            mapped["doc_no"],
            mapped.get("item_code"),
            mapped.get("item_name"),
            mapped["qty"],
            mapped["unit"],
            mapped.get("unit_price"),
            mapped.get("amount"),
        )
        if fingerprint in seen_row:
            issues.append(
                StatementRowIssue(
                    line_no=offset,
                    field="<row>",
                    value=str(fingerprint),
                    code="E_DUPLICATE_ROW",
                    message=f"与第 {seen_row[fingerprint]} 行内容完全重复",
                )
            )
            continue
        seen_row[fingerprint] = offset
        rows.append(mapped)

    if not rows and not issues:
        raise StatementCsvError(filename, (), reason="只有表头或全是空行，没有数据行")
    if issues:
        raise StatementCsvError(filename, tuple(issues))

    return SupplierStatement(
        source_file=filename,
        supplier_lines=tuple(rows),
        ignored_columns=tuple(dict.fromkeys(ignored)),
    )


def _map_row(
    row: dict[str, str],
    *,
    line_no: int,
    filename: str,
    default_period: str | None,
    issues: list[StatementRowIssue],
) -> dict[str, Any] | None:
    raw_line_no = row.get("line_no", "")
    if raw_line_no:
        try:
            mapped_line_no = int(raw_line_no)
        except ValueError:
            issues.append(
                StatementRowIssue(
                    line_no=line_no,
                    field="line_no",
                    value=raw_line_no,
                    code="E_BAD_LINE_NO",
                    message="行号必须是正整数",
                )
            )
            return None
    else:
        mapped_line_no = line_no - 1

    doc_no = row.get("doc_no", "")
    supplier_code = row.get("supplier_code", "")
    if not doc_no:
        issues.append(
            StatementRowIssue(line_no, "doc_no", "", "E_REQUIRED_EMPTY", "单据号不能为空")
        )
    if not supplier_code:
        issues.append(
            StatementRowIssue(
                line_no, "supplier_code", "", "E_REQUIRED_EMPTY", "供应商编码不能为空"
            )
        )

    item_code = row.get("item_code") or None
    item_name = row.get("item_name") or None
    if not item_code and not item_name:
        issues.append(
            StatementRowIssue(
                line_no,
                "item_code/item_name",
                "",
                "E_ITEM_MISSING",
                "物料编码与名称至少填一个",
            )
        )

    qty_raw = row.get("qty", "")
    if not qty_raw:
        issues.append(
            StatementRowIssue(line_no, "qty", "", "E_QTY_EMPTY", "数量为空（缺失≠零）")
        )
        qty = None
    else:
        qty = _parse_number(qty_raw, line_no=line_no, field="qty", issues=issues)

    unit_raw = row.get("unit", "")
    unit = normalize_unit(unit_raw)
    if not unit_raw:
        issues.append(
            StatementRowIssue(line_no, "unit", "", "E_UNIT_EMPTY", "单位为空")
        )
    elif unit is None:
        issues.append(
            StatementRowIssue(
                line_no,
                "unit",
                unit_raw,
                "E_UNKNOWN_UNIT",
                f"单位不认识，可用: {', '.join(KNOWN_UNITS)}",
            )
        )

    amount_raw = row.get("amount", "")
    price_raw = row.get("unit_price", "")
    if not amount_raw and not price_raw:
        issues.append(
            StatementRowIssue(
                line_no,
                "amount/unit_price",
                "",
                "E_AMOUNT_MISSING",
                "金额与单价至少填一个",
            )
        )
    amount = (
        _parse_number(amount_raw, line_no=line_no, field="amount", issues=issues)
        if amount_raw
        else None
    )
    unit_price = (
        _parse_number(price_raw, line_no=line_no, field="unit_price", issues=issues)
        if price_raw
        else None
    )
    tax_rate = (
        _parse_number(row["tax_rate"], line_no=line_no, field="tax_rate", issues=issues)
        if row.get("tax_rate")
        else None
    )
    scrap_qty = (
        _parse_number(
            row["scrap_qty"], line_no=line_no, field="scrap_qty", issues=issues
        )
        if row.get("scrap_qty")
        else None
    )
    tax_included = (
        _parse_bool(row["tax_included"], line_no=line_no, issues=issues)
        if row.get("tax_included")
        else True
    )

    return {
        "system": SYSTEM_NAME,
        "doc_no": doc_no,
        "line_no": mapped_line_no,
        "supplier_code": supplier_code,
        "item_code": item_code,
        "item_name": item_name,
        "qty": qty,
        "unit": unit,
        "unit_price": unit_price,
        "amount": amount,
        "currency": row.get("currency") or "CNY",
        "tax_included": tax_included,
        "tax_rate": tax_rate,
        "scrap_qty": scrap_qty,
        "period": row.get("period") or default_period,
        "coord": {"file": filename, "line": line_no},
    }


#: 已被映射消费的列（其余列进 ignored_columns）
_MAPPED_COLUMNS: frozenset[str] = frozenset(
    {
        "doc_no",
        "supplier_code",
        "line_no",
        "item_code",
        "item_name",
        "qty",
        "unit",
        "unit_price",
        "amount",
        "currency",
        "tax_included",
        "tax_rate",
        "scrap_qty",
        "period",
    }
)

__all__ = [
    "KNOWN_UNITS",
    "REQUIRED_COLUMNS",
    "REQUIRED_GROUPS",
    "SYSTEM_NAME",
    "StatementCsvError",
    "StatementRowIssue",
    "SupplierStatement",
    "map_supplier_statement_csv",
    "normalize_unit",
]
