"""CSV 行 → canonical ``Document`` 的映射（只读导入）。

三条硬规则（与 U8 映射一致，来自 PRD §7.1/§7.3）：

1. **缺就报缺**：单号/日期/供应商编码缺失整单阻断，行字段缺失记 ``missing_fields``；
2. **缺失 ≠ 零**：空值保持 ``None``，存在不可用行时合计不计算（不把缺行当 0）；
3. **不猜**：单位缺失不按 1:1、编码非 UTF-8 不按猜测编码解析。
"""

from __future__ import annotations

from collections.abc import Iterable
from decimal import Decimal

from apps.connectors.canonical.mapping import (
    MappingContext,
    MappingOutcome,
    amount_mismatch,
    date_only,
    missing,
    money,
    qty,
    text,
)
from apps.connectors.canonical.models import Document, DocumentLine
from apps.connectors.file.csvsource import ParsedRow
from apps.connectors.file.spec import ENTITY_KINDS
from apps.platform.contracts.canonical import Evidence, Money

_TRUE_VALUES = frozenset({"true", "1", "y", "yes", "是", "含税"})
_FALSE_VALUES = frozenset({"false", "0", "n", "no", "否", "不含税"})


def group_rows(rows: Iterable[ParsedRow]) -> dict[tuple[str, str], list[ParsedRow]]:
    """按 ``(doc_no, supplier_code)`` 分组；空值也成组，交由映射层判阻断。"""

    grouped: dict[tuple[str, str], list[ParsedRow]] = {}
    for row in rows:
        key = (
            text(row.values.get("doc_no")) or "",
            text(row.values.get("supplier_code")) or "",
        )
        grouped.setdefault(key, []).append(row)
    return grouped


def map_rows(
    rows: Iterable[ParsedRow], *, entity: str, ctx: MappingContext
) -> list[MappingOutcome[Document]]:
    kind = ENTITY_KINDS.get(entity)
    if kind is None:
        raise ValueError(f"文件通道不支持的实体: {entity!r}")
    return [
        map_document(key, tenant_rows, kind=kind, ctx=ctx)
        for key, tenant_rows in group_rows(rows).items()
    ]


def map_document(
    key: tuple[str, str],
    rows: list[ParsedRow],
    *,
    kind: str,
    ctx: MappingContext,
) -> MappingOutcome[Document]:
    doc_no, party_code = key
    issues = []
    blocked = False

    if not doc_no:
        issues.append(
            missing("doc_no", "missing", source=ctx.connector, detail="源文件该行单据号为空")
        )
        blocked = True

    doc_date = next(
        (value for value in (date_only(row.values.get("doc_date")) for row in rows) if value),
        None,
    )
    if doc_date is None:
        issues.append(
            missing(
                "doc_date",
                "missing",
                source=ctx.connector,
                doc_no=doc_no or None,
                detail="源文件无可用日期（YYYY-MM-DD）",
            )
        )
        blocked = True

    if not party_code:
        issues.append(
            missing(
                "party_code",
                "missing",
                source=ctx.connector,
                doc_no=doc_no or None,
                detail="源文件供应商编码为空",
            )
        )
        blocked = True

    if blocked:
        # 阻断时连半条记录都不构造，避免下游把残缺单据当完整数据。
        return MappingOutcome(record=None, issues=issues, blocked=True)

    lines: list[DocumentLine] = []
    all_usable = True
    for index, row in enumerate(rows, start=1):
        line, line_issues, usable = _build_line(index, row, ctx, doc_no)
        issues.extend(line_issues)
        all_usable = all_usable and usable
        if line is not None:
            lines.append(line)

    total_amount = _total(lines) if lines and all_usable else None
    if total_amount is None:
        issues.append(
            missing(
                "total_amount",
                "missing",
                source=ctx.connector,
                doc_no=doc_no,
                detail="存在不可用行，合计不计算（缺失 ≠ 0）",
            )
        )
    else:
        total_amount = Money(
            amount=str(total_amount.quantize(Decimal("0.0001"))),
            currency=ctx.currency,
            tax_included=ctx.tax_inclusive_price,
        )

    file_name = rows[0].file
    record = Document(
        kind=kind,
        doc_no=doc_no,
        doc_date=doc_date,
        party_code=party_code,
        party_name=_first_text(rows, "supplier_name"),
        lines=lines,
        total_amount=total_amount,
        status=_first_text(rows, "status"),
        source_refs={ctx.connector: f"{file_name}#{doc_no}"},
        authority_system=ctx.authority_system,
        evidence=[
            Evidence(system=ctx.connector, doc_no=doc_no, field=f"{file_name}:doc_no"),
        ],
        missing_fields=issues,
    )
    return MappingOutcome(record=record, issues=issues)


def _build_line(
    index: int, row: ParsedRow, ctx: MappingContext, doc_no: str
) -> tuple[DocumentLine | None, list, bool]:
    """单行映射。物料编码缺失时返回 ``None``（该行不进模型，但登记缺失）。"""

    values = row.values
    issues = []
    where = f"{row.file}:{row.line_no}"
    item_code = text(values.get("item_code"))
    if item_code is None:
        return (
            None,
            [
                missing(
                    f"lines[{index}].item_code",
                    "missing",
                    source=ctx.connector,
                    doc_no=doc_no,
                    detail=f"{where} 无物料编码",
                )
            ],
            False,
        )

    raw_qty = text(values.get("qty"))
    quantity = qty(raw_qty)
    if quantity is None:
        issues.append(
            missing(
                f"lines[{index}].quantity",
                "missing",
                source=ctx.connector,
                doc_no=doc_no,
                detail=f"{where} 数量为空" if raw_qty is None else f"{where} 数量无法解析: {raw_qty!r}",
            )
        )

    unit = text(values.get("unit"))
    if unit is None:
        issues.append(
            missing(
                f"lines[{index}].unit",
                "unit_conversion_missing",
                source=ctx.connector,
                doc_no=doc_no,
                detail=f"{where} 行上无单位，无法换算",
            )
        )

    tax_included = _tax_included(values.get("tax_included"), ctx)
    unit_price = money(values.get("unit_price"), ctx, tax_included=tax_included)
    if unit_price is None:
        issues.append(
            missing(
                f"lines[{index}].unit_price",
                "missing",
                source=ctx.connector,
                doc_no=doc_no,
                detail=f"{where} 单价为空或无法解析",
            )
        )

    amount = money(values.get("amount"), ctx, tax_included=tax_included)
    if amount is None:
        issues.append(
            missing(
                f"lines[{index}].amount",
                "missing",
                source=ctx.connector,
                doc_no=doc_no,
                detail=f"{where} 金额为空或无法解析",
            )
        )

    if amount_mismatch(quantity, unit_price, amount):
        issues.append(
            missing(
                f"lines[{index}].amount",
                "amount_mismatch",
                source=ctx.connector,
                doc_no=doc_no,
                detail=f"{where} 金额 ≠ 数量 × 单价（容差 0.01）",
            )
        )

    line = DocumentLine(
        line_no=_line_no(values.get("line_no"), index),
        item_code=item_code,
        item_name=text(values.get("item_name")),
        quantity=quantity,
        unit=unit,
        unit_price=unit_price,
        amount=amount,
        tax_rate=qty(values.get("tax_rate")),
        source_refs={ctx.connector: f"{row.file}#{doc_no}:{row.line_no}"},
        authority_system=ctx.authority_system,
        evidence=[
            Evidence(
                system=ctx.connector,
                doc_no=doc_no,
                field=f"{row.file}:{row.line_no}.item_code",
            )
        ],
        missing_fields=issues,
    )
    return line, issues, not issues


def _line_no(raw: str | None, fallback: int) -> int:
    value = text(raw)
    if value is None or not value.isdigit():
        return fallback
    parsed = int(value)
    return parsed if parsed >= 1 else fallback


def _tax_included(raw: str | None, ctx: MappingContext) -> bool | None:
    value = text(raw)
    if value is None:
        return None
    lowered = value.lower()
    if lowered in _TRUE_VALUES:
        return True
    if lowered in _FALSE_VALUES:
        return False
    # 认不出的写法不猜，回落到连接器配置（币种/含税口径来自配置而不是单据）。
    return None


def _first_text(rows: list[ParsedRow], column: str) -> str | None:
    for row in rows:
        value = text(row.values.get(column))
        if value is not None:
            return value
    return None


def _total(lines: list[DocumentLine]) -> Decimal | None:
    total = Decimal(0)
    for line in lines:
        if line.amount is None:
            return None
        total += line.amount.value
    return total
