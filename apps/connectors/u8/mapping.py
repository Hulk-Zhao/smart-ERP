"""U8 ↔ canonical 双向映射（C1）。

降级口径（PRD §7.1 / §7.3 / DQ-02 / DQ-05），一句话：**缺就报缺，不猜**。

- 供应商缺 ``cVenCode`` / 物料缺 ``cInvCode`` / 单据缺单号或日期 → 阻断，不造记录；
- 物料缺基本单位 → ``base_unit_missing``；行单位与基本单位不同且无换算 → ``unit_conversion_missing``，
  该行不参与金额合计；
- 行金额 ≠ 数量 × 单价（±0.01）→ ``amount_mismatch``（DQ-05），该行不参与合计；
- 任一行不可用 → 单据合计为 ``None``，绝不「先把能加的加起来」。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from decimal import Decimal
from typing import Any

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
from apps.connectors.canonical.models import (
    Document,
    DocumentLine,
    Item,
    MissingField,
    Party,
)
from apps.connectors.u8.models import (
    U8ArrivalVouch,
    U8Item,
    U8POLine,
    U8PurchaseOrder,
    U8Supplier,
)
from apps.platform.contracts.canonical import Evidence, Money


def _evidence(ctx: MappingContext, doc_no: str, field: str) -> Evidence:
    return Evidence(system=ctx.connector, doc_no=doc_no, field=field)


# --------------------------------------------------------------------------- 正向


def map_supplier(raw: U8Supplier, ctx: MappingContext) -> MappingOutcome[Party]:
    code = text(raw.cVenCode)
    if code is None:
        return MappingOutcome(
            record=None,
            issues=[missing("code", "missing", source=ctx.connector)],
            blocked=True,
        )
    issues: list[MissingField] = []
    name = text(raw.cVenName)
    if name is None:
        issues.append(missing("name", "missing", source=ctx.connector, doc_no=code))
    aliases = [text(raw.cVenAbbName)] if text(raw.cVenAbbName) else []
    record = Party(
        kind="supplier",
        code=code,
        name=name,
        aliases=[a for a in aliases if a],
        tax_no=text(raw.cTaxCode),
        payment_terms=None,
        source_refs={ctx.connector: code},
        authority_system=ctx.authority_system,
        evidence=[_evidence(ctx, code, "cVenCode")],
        missing_fields=issues,
    )
    return MappingOutcome(record=record, issues=issues)


def map_item(raw: U8Item, ctx: MappingContext) -> MappingOutcome[Item]:
    code = text(raw.cInvCode)
    if code is None:
        return MappingOutcome(
            record=None,
            issues=[missing("code", "missing", source=ctx.connector)],
            blocked=True,
        )
    issues: list[MissingField] = []
    base_unit = text(raw.cComUnitName)
    if base_unit is None:
        issues.append(
            missing(
                "base_unit",
                "base_unit_missing",
                source=ctx.connector,
                doc_no=code,
                detail="物料档案无基本单位，涉及该物料的数量无法归一（DQ-02）",
            )
        )
    record = Item(
        code=code,
        name=text(raw.cInvName),
        spec=text(raw.cInvStd),
        base_unit=base_unit,
        item_type=text(raw.cInvType),
        source_refs={ctx.connector: code},
        authority_system=ctx.authority_system,
        evidence=[_evidence(ctx, code, "cInvCode")],
        missing_fields=issues,
    )
    return MappingOutcome(record=record, issues=issues)


def _build_line(
    index: int,
    raw: U8POLine | Any,
    ctx: MappingContext,
    doc_no: str,
    items: Mapping[str, Item] | None,
) -> tuple[DocumentLine | None, list[MissingField], bool]:
    """返回 (行, 缺失, 该行是否可用于合计)。"""

    issues: list[MissingField] = []
    item_code = text(raw.cInvCode)
    if item_code is None:
        issues.append(
            missing(f"lines[{index}].item_code", "missing", source=ctx.connector, doc_no=doc_no)
        )
        return None, issues, False

    unit = text(raw.cComUnitName)
    quantity = qty(getattr(raw, "iQuantity", None))
    tax_price = getattr(raw, "iTaxPrice", None)
    plain_price = getattr(raw, "iUnitPrice", None)
    if tax_price is not None and text(tax_price) is not None:
        unit_price = money(tax_price, ctx, tax_included=True)
    elif plain_price is not None and text(plain_price) is not None:
        # 只有无税单价时如实标注 tax_included=False，不按含税价反算。
        unit_price = money(plain_price, ctx, tax_included=False)
    else:
        unit_price = None
    amount = money(getattr(raw, "iMoney", None), ctx)

    if unit is None:
        issues.append(
            missing(
                f"lines[{index}].unit",
                "unit_conversion_missing",
                source=ctx.connector,
                doc_no=doc_no,
                detail="行上无单位",
            )
        )
    if quantity is None:
        issues.append(
            missing(f"lines[{index}].quantity", "missing", source=ctx.connector, doc_no=doc_no)
        )
    if amount is None:
        issues.append(
            missing(f"lines[{index}].amount", "missing", source=ctx.connector, doc_no=doc_no)
        )

    if items is not None:
        item = items.get(item_code)
        if item is None:
            issues.append(
                missing(
                    f"lines[{index}].item",
                    "unmapped",
                    source=ctx.connector,
                    doc_no=doc_no,
                    detail=f"物料 {item_code} 未匹配到档案",
                )
            )
        elif item.base_unit is None:
            issues.append(
                missing(
                    f"lines[{index}].unit",
                    "base_unit_missing",
                    source=ctx.connector,
                    doc_no=doc_no,
                    detail=f"物料 {item_code} 无基本单位",
                )
            )
        elif unit is not None and unit != item.base_unit:
            factors = {
                (conv.from_unit, conv.to_unit): conv.factor for conv in item.conversions
            }
            if (unit, item.base_unit) not in factors and (item.base_unit, unit) not in factors:
                issues.append(
                    missing(
                        f"lines[{index}].unit",
                        "unit_conversion_missing",
                        source=ctx.connector,
                        doc_no=doc_no,
                        detail=f"{unit} → {item.base_unit} 无换算率",
                    )
                )

    if amount_mismatch(quantity, unit_price, amount):
        issues.append(
            missing(
                f"lines[{index}].amount",
                "amount_mismatch",
                source=ctx.connector,
                doc_no=doc_no,
                detail="金额 ≠ 数量 × 单价（容差 0.01，DQ-05）",
            )
        )

    line = DocumentLine(
        line_no=index,
        item_code=item_code,
        item_name=text(getattr(raw, "cInvName", None)),
        quantity=quantity,
        unit=unit,
        unit_price=unit_price,
        amount=amount,
        tax_rate=qty(getattr(raw, "iTaxRate", None)),
        source_refs={ctx.connector: f"{doc_no}#{index}"},
        authority_system=ctx.authority_system,
        evidence=[_evidence(ctx, doc_no, f"details[{index}]")],
        missing_fields=issues,
    )
    return line, issues, not issues


def _map_document(
    *,
    kind: str,
    doc_no: str | None,
    doc_date: str | None,
    party_code: str | None,
    party_name: str | None,
    raw_lines: Iterable[Any],
    ctx: MappingContext,
    items: Mapping[str, Item] | None,
) -> MappingOutcome[Document]:
    issues: list[MissingField] = []
    blocked = False
    if doc_no is None:
        issues.append(missing("doc_no", "missing", source=ctx.connector))
        blocked = True
    if doc_date is None:
        issues.append(missing("doc_date", "missing", source=ctx.connector, doc_no=doc_no))
        blocked = True
    if party_code is None:
        issues.append(missing("party_code", "missing", source=ctx.connector, doc_no=doc_no))
        blocked = True
    if blocked:
        return MappingOutcome(record=None, issues=issues, blocked=True)

    lines: list[DocumentLine] = []
    all_usable = True
    for index, raw in enumerate(raw_lines, start=1):
        line, line_issues, usable = _build_line(index, raw, ctx, doc_no, items)
        issues.extend(line_issues)
        all_usable = all_usable and usable
        if line is not None:
            lines.append(line)

    total: Money | None = None
    if lines and all_usable:
        amounts = [line.amount for line in lines if line.amount is not None]
        if len(amounts) == len(lines):
            total_value = sum((m.value for m in amounts), Decimal(0)).quantize(Decimal("0.0001"))
            total = Money(
                amount=str(total_value),
                currency=ctx.currency,
                tax_included=ctx.tax_inclusive_price,
            )
    if total is None:
        issues.append(
            missing(
                "total_amount",
                "missing",
                source=ctx.connector,
                doc_no=doc_no,
                detail="存在不可用行，合计不计算（缺失 ≠ 零）",
            )
        )

    record = Document(
        kind=kind,  # type: ignore[arg-type]
        doc_no=doc_no,
        doc_date=doc_date,
        party_code=party_code,
        party_name=party_name,
        lines=lines,
        total_amount=total,
        status=None,
        source_refs={ctx.connector: doc_no},
        authority_system=ctx.authority_system,
        evidence=[_evidence(ctx, doc_no, "cCode")],
        missing_fields=issues,
    )
    return MappingOutcome(record=record, issues=issues)


def map_purchase_order(
    raw: U8PurchaseOrder,
    ctx: MappingContext,
    *,
    items: Mapping[str, Item] | None = None,
) -> MappingOutcome[Document]:
    return _map_document(
        kind="purchase_order",
        doc_no=text(raw.cCode),
        doc_date=date_only(raw.dPODate),
        party_code=text(raw.cVenCode),
        party_name=text(raw.cVenName),
        raw_lines=raw.details,
        ctx=ctx,
        items=items,
    )


def map_arrival_vouch(
    raw: U8ArrivalVouch,
    ctx: MappingContext,
    *,
    items: Mapping[str, Item] | None = None,
) -> MappingOutcome[Document]:
    return _map_document(
        kind="inbound_order",
        doc_no=text(raw.cCode),
        doc_date=date_only(raw.dDate),
        party_code=text(raw.cVenCode),
        party_name=text(raw.cVenName),
        raw_lines=raw.details,
        ctx=ctx,
        items=items,
    )


# --------------------------------------------------------------------------- 反向


def to_u8_supplier(party: Party) -> dict[str, Any]:
    return {
        "cVenCode": party.code,
        "cVenName": party.name,
        "cVenAbbName": party.aliases[0] if party.aliases else None,
        "cTaxCode": party.tax_no,
    }


def to_u8_item(item: Item) -> dict[str, Any]:
    return {
        "cInvCode": item.code,
        "cInvName": item.name,
        "cInvStd": item.spec,
        "cComUnitName": item.base_unit,
    }


def _draft_payload(
    doc: Document,
    ctx: MappingContext,
    *,
    memo: str | None,
) -> MappingOutcome[dict[str, Any]]:
    issues: list[MissingField] = []
    details: list[dict[str, Any]] = []
    for line in doc.lines:
        line_issues: list[MissingField] = []
        if not line.item_code:
            line_issues.append(
                missing(f"lines[{line.line_no}].item_code", "missing", source=ctx.connector, doc_no=doc.doc_no)
            )
        if line.quantity is None:
            line_issues.append(
                missing(f"lines[{line.line_no}].quantity", "missing", source=ctx.connector, doc_no=doc.doc_no)
            )
        if line.unit is None:
            line_issues.append(
                missing(
                    f"lines[{line.line_no}].unit",
                    "unit_conversion_missing",
                    source=ctx.connector,
                    doc_no=doc.doc_no,
                    detail="写回 U8 必须带单位",
                )
            )
        if line_issues:
            issues.extend(line_issues)
            continue
        price_key = "iTaxPrice" if (line.unit_price and line.unit_price.tax_included) else "iUnitPrice"
        details.append(
            {
                "cInvCode": line.item_code,
                "cComUnitName": line.unit,
                "iQuantity": line.quantity,
                price_key: line.unit_price.amount if line.unit_price else None,
                "iTaxRate": line.tax_rate,
            }
        )

    if not details:
        issues.append(
            missing("details", "missing", source=ctx.connector, doc_no=doc.doc_no, detail="没有可写回的行")
        )
    if issues:
        return MappingOutcome(record=None, issues=issues, blocked=True)

    return MappingOutcome(
        record={
            # 业务键随草稿一起带过去：verify/补偿都靠它定位这张草稿。
            "source_doc_no": doc.doc_no,
            "cVenCode": doc.party_code,
            "dPODate": doc.doc_date,
            "cMemo": memo,
            "details": details,
        }
    )


def to_u8_purchase_order_draft(
    doc: Document, ctx: MappingContext, *, memo: str | None = None
) -> MappingOutcome[dict[str, Any]]:
    return _draft_payload(doc, ctx, memo=memo)


def to_u8_arrival_draft(
    doc: Document, ctx: MappingContext, *, memo: str | None = None
) -> MappingOutcome[dict[str, Any]]:
    return _draft_payload(doc, ctx, memo=memo)


# --------------------------------------------------------------- 工单载荷 → U8 报文


def _doc_from_payload(payload: Mapping[str, Any], ctx: MappingContext, kind: str) -> MappingOutcome[Document]:
    """把工单载荷装配成 canonical ``Document``；任一必需字段缺失即阻断。

    写路径上不允许「缺一点也先写出去」——草稿一旦生成就会被业务同事当真。
    """

    issues: list[MissingField] = []
    doc_no = text(payload.get("source_doc_no"))
    if doc_no is None:
        issues.append(missing("source_doc_no", "missing", source=ctx.connector))
    doc_date = date_only(payload.get("doc_date"))
    if doc_date is None:
        issues.append(missing("doc_date", "missing", source=ctx.connector, doc_no=doc_no))
    party_code = text(payload.get("supplier_code"))
    if party_code is None:
        issues.append(missing("supplier_code", "missing", source=ctx.connector, doc_no=doc_no))

    raw_lines = payload.get("lines")
    if not isinstance(raw_lines, list) or not raw_lines:
        issues.append(
            missing("lines", "missing", source=ctx.connector, doc_no=doc_no, detail="没有明细行")
        )
        raw_lines = []

    lines: list[DocumentLine] = []
    for index, raw in enumerate(raw_lines, start=1):
        if not isinstance(raw, Mapping):
            issues.append(
                missing(f"lines[{index}]", "missing", source=ctx.connector, doc_no=doc_no)
            )
            continue
        item_code = text(raw.get("item_code"))
        quantity = qty(raw.get("quantity"))
        unit = text(raw.get("unit"))
        price = money(raw.get("unit_price"), ctx)
        if item_code is None:
            issues.append(
                missing(f"lines[{index}].item_code", "missing", source=ctx.connector, doc_no=doc_no)
            )
        if quantity is None:
            issues.append(
                missing(f"lines[{index}].quantity", "missing", source=ctx.connector, doc_no=doc_no)
            )
        if unit is None:
            issues.append(
                missing(
                    f"lines[{index}].unit",
                    "unit_conversion_missing",
                    source=ctx.connector,
                    doc_no=doc_no,
                    detail="写回 U8 必须带单位",
                )
            )
        if price is None:
            issues.append(
                missing(f"lines[{index}].unit_price", "missing", source=ctx.connector, doc_no=doc_no)
            )
        if item_code is None or quantity is None or unit is None or price is None:
            continue
        lines.append(
            DocumentLine(
                line_no=index,
                item_code=item_code,
                quantity=quantity,
                unit=unit,
                unit_price=price,
                tax_rate=qty(raw.get("tax_rate")),
                source_refs={ctx.connector: f"{doc_no or '-'}#{index}"},
                authority_system=ctx.authority_system,
                evidence=[_evidence(ctx, doc_no or "-", f"lines[{index}]")],
            )
        )

    if issues:
        return MappingOutcome(record=None, issues=issues, blocked=True)

    return MappingOutcome(
        record=Document(
            kind=kind,  # type: ignore[arg-type]
            doc_no=doc_no or "",
            doc_date=doc_date or "",
            party_code=party_code or "",
            lines=lines,
            source_refs={ctx.connector: doc_no or ""},
            authority_system=ctx.authority_system,
            evidence=[_evidence(ctx, doc_no or "-", "cCode")],
        )
    )


def build_po_draft_body(
    payload: Mapping[str, Any], ctx: MappingContext
) -> MappingOutcome[dict[str, Any]]:
    doc = _doc_from_payload(payload, ctx, "purchase_order")
    if doc.record is None:
        return MappingOutcome(record=None, issues=doc.issues, blocked=True)
    return to_u8_purchase_order_draft(
        doc.record, ctx, memo=text(payload.get("memo"))
    )


def build_arrival_draft_body(
    payload: Mapping[str, Any], ctx: MappingContext
) -> MappingOutcome[dict[str, Any]]:
    doc = _doc_from_payload(payload, ctx, "arrival_vouch")
    if doc.record is None:
        return MappingOutcome(record=None, issues=doc.issues, blocked=True)
    return to_u8_arrival_draft(doc.record, ctx, memo=text(payload.get("memo")))
