"""七类归因（FR-104）：单价不一致 / 数量不符 / 报废未扣 / 单位换算 / 税率口径 / 缺单 / 重复计费。

判定顺序按「最能解释差异的那个原因」优先，规则确定性、可解释：
重复计费 > 缺单 > 报废未扣 > 数量不符 > 单价不一致 > 税率口径。
无法归入七类的金额差不出结论，进 ``unattributed``（待人工归因）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from .models import (
    MIN_CONFIDENCE,
    MONEY_EXP,
    DiffItem,
    DiffOwner,
    DiffType,
    DocLine,
    Evidence,
    MatchPair,
    Money,
    Tolerance,
    UnattributedItem,
    quantize_confidence,
)

# 差异金额占本行金额超过该比例 → 疑似数据异常，阻断自动处置（FR-104 异常）
ANOMALY_LINE_RATIO = Decimal("0.20")
# 数量差超过我方数量 10 倍 → 疑似数据异常
ANOMALY_QTY_MULTIPLE = Decimal(10)


@dataclass
class PairAttribution:
    diffs: list[DiffItem] = field(default_factory=list)
    unattributed: UnattributedItem | None = None
    suspected_anomaly: bool = False

    @property
    def needs_manual(self) -> bool:
        if self.unattributed is not None:
            return True
        return any(diff.needs_manual for diff in self.diffs)


def effective_price(line: DocLine) -> Decimal | None:
    """单价缺失时可由 金额 / 数量 反算（算术推导，不是猜测）。"""
    if line.unit_price is not None:
        return line.unit_price
    if line.amount is not None and line.qty:
        return (line.amount.amount / line.qty).quantize(MONEY_EXP)
    return None


def effective_amount(line: DocLine) -> Money | None:
    if line.amount is not None:
        return line.amount
    price = effective_price(line)
    if price is None or line.qty is None:
        return None
    currency = line.amount.currency if line.amount else "CNY"
    tax_included = line.amount.tax_included if line.amount else True
    return Money(price * line.qty, currency, tax_included)


def _evidence(our: DocLine, supplier: DocLine, our_field: str, sup_field: str) -> tuple[Evidence, ...]:
    return (our.evidence(our_field), supplier.evidence(sup_field))


def _suspected(amount_diff: Decimal, amount_mine: Decimal, qty_diff: Decimal | None, qty_mine: Decimal | None) -> bool:
    amount_exploded = (
        amount_mine != 0
        and abs(amount_diff) / abs(amount_mine) > ANOMALY_LINE_RATIO
    )
    qty_exploded = (
        qty_diff is not None
        and qty_mine is not None
        and qty_mine != 0
        and abs(qty_diff) / abs(qty_mine) > ANOMALY_QTY_MULTIPLE
    )
    return amount_exploded or qty_exploded


def attribute_pair(
    pair: MatchPair,
    tolerance: Tolerance,
    supplier_qty: Decimal | None,
    supplier_price: Decimal | None = None,
) -> PairAttribution:
    """对一对已匹配行做差异计算与归因。

    ``supplier_qty`` / ``supplier_price`` 是供应商数量与单价按单位换算后的值
    （与我方同单位口径），避免「箱价 vs 件价」被误判为单价不一致。
    """
    result = PairAttribution()
    our, supplier = pair.our, pair.supplier

    qty_mine = our.qty
    qty_supplier = supplier_qty
    price_mine = effective_price(our)
    price_supplier = (
        supplier_price if supplier_price is not None else effective_price(supplier)
    )
    amount_mine = effective_amount(our)
    amount_supplier = effective_amount(supplier)

    if (
        qty_mine is None
        or qty_supplier is None
        or price_mine is None
        or price_supplier is None
        or amount_mine is None
        or amount_supplier is None
    ):
        # 就绪检查应已拦截；兜底仍不出结论
        result.unattributed = UnattributedItem(
            ref=supplier.ref,
            amount_diff=Money(Decimal(0)),
            reason="关键数值缺失，无法计算差异（缺失≠零）",
            evidence=_evidence(our, supplier, "qty", "qty"),
        )
        return result

    qty_diff = qty_supplier - qty_mine
    price_diff = price_supplier - price_mine
    amount_diff = amount_supplier - amount_mine
    amount_mine_amount = amount_mine.amount

    qty_bad = abs(qty_diff) > tolerance.qty
    price_bad = abs(price_diff) > tolerance.price

    result.suspected_anomaly = _suspected(
        amount_diff.amount, amount_mine_amount, qty_diff, qty_mine
    )

    explained = Decimal(0)

    scrap = our.scrap_qty
    scrap_explains = (
        scrap is not None
        and qty_bad
        and abs(qty_diff - scrap) <= max(tolerance.qty, Decimal("0.0001"))
    )

    if scrap_explains:
        scrap_amount = qty_diff * price_mine
        explained += scrap_amount
        result.diffs.append(
            DiffItem(
                type=DiffType.SCRAP,
                amount=Money(scrap_amount, amount_diff.currency, amount_diff.tax_included),
                owner=DiffOwner.SUPPLIER,
                suggestion=(
                    f"供应商按产出数量计费，未扣报废 {scrap}，"
                    "建议按合同损耗率重算或要求下期红冲"
                ),
                confidence=quantize_confidence(Decimal("0.92")),
                evidence=_evidence(our, supplier, "scrap_qty", "qty"),
                ref=supplier.ref,
                supplier_code=supplier.supplier_code,
            )
        )
        qty_bad = False

    if qty_bad:
        qty_amount = qty_diff * price_mine
        explained += qty_amount
        confidence = Decimal("0.88") if not price_bad else Decimal("0.74")
        result.diffs.append(
            DiffItem(
                type=DiffType.QTY,
                amount=Money(qty_amount, amount_diff.currency, amount_diff.tax_included),
                owner=DiffOwner.SUPPLIER if qty_diff > 0 else DiffOwner.OURS,
                suggestion=(
                    "数量差额需与入库单/报工单核对，确认后补单或在下期冲抵"
                ),
                confidence=quantize_confidence(confidence),
                evidence=_evidence(our, supplier, "qty", "qty"),
                ref=supplier.ref,
                supplier_code=supplier.supplier_code,
            )
        )

    if price_bad:
        price_amount = price_diff * qty_mine
        explained += price_amount
        confidence = Decimal("0.90") if not qty_bad else Decimal("0.78")
        result.diffs.append(
            DiffItem(
                type=DiffType.UNIT_PRICE,
                amount=Money(
                    price_amount, amount_diff.currency, amount_diff.tax_included
                ),
                owner=DiffOwner.SUPPLIER if price_diff > 0 else DiffOwner.OURS,
                suggestion=(
                    f"单价 {price_mine} 与对账单 {price_supplier} 不一致，"
                    "核对价格协议/调价生效日期"
                ),
                confidence=quantize_confidence(confidence),
                evidence=_evidence(our, supplier, "unit_price", "unit_price"),
                ref=supplier.ref,
                supplier_code=supplier.supplier_code,
            )
        )
        if qty_bad:
            # 数量与单价同时变动时的交叉项，避免被误判为「无法归因」
            explained += qty_diff * price_diff

    residual = amount_diff.amount - explained
    if abs(residual) <= tolerance.amount:
        return result

    tax_rate_mine, tax_rate_supplier = our.tax_rate, supplier.tax_rate
    rate_differs = (
        tax_rate_mine is not None
        and tax_rate_supplier is not None
        and tax_rate_mine != tax_rate_supplier
    )
    basis_differs = amount_mine.tax_included != amount_supplier.tax_included

    if rate_differs or basis_differs:
        result.diffs.append(
            DiffItem(
                type=DiffType.TAX,
                amount=Money(residual, amount_diff.currency, amount_diff.tax_included),
                owner=DiffOwner.SUPPLIER if residual > 0 else DiffOwner.OURS,
                suggestion="核对含税/不含税口径与税率，按合同税率重算",
                confidence=quantize_confidence(
                    Decimal("0.88") if rate_differs else Decimal("0.75")
                ),
                evidence=_evidence(our, supplier, "tax_rate", "tax_rate"),
                ref=supplier.ref,
                supplier_code=supplier.supplier_code,
            )
        )
        return result

    # 金额舍入容差：数量与单价完全一致，残留仅来自金额取整。
    # 这是「容差调整」而非七类业务归因：在明细里留痕可审计，但不生成差异单、
    # 不阻断批次；超过容差或整批累计超限时仍走下面的 unattributed / 转人工。
    if (
        not qty_bad
        and not price_bad
        and not result.diffs
        and tolerance.rounding > 0
        and abs(residual) <= tolerance.rounding
    ):
        result.diffs.append(
            DiffItem(
                type=DiffType.ROUNDING_TOLERANCE,
                amount=Money(residual, amount_diff.currency, amount_diff.tax_included),
                owner=DiffOwner.SYSTEM,
                suggestion=(
                    f"数量/单价一致，金额差 {residual} 元在舍入容差"
                    f" {tolerance.rounding} 元内（供应商按角取整），按容差放行"
                ),
                confidence=quantize_confidence(Decimal("0.95")),
                evidence=_evidence(our, supplier, "amount", "amount"),
                ref=supplier.ref,
                supplier_code=supplier.supplier_code,
            )
        )
        return result

    if result.diffs:
        result.unattributed = UnattributedItem(
            ref=supplier.ref,
            amount_diff=Money(
                residual, amount_diff.currency, amount_diff.tax_included
            ),
            reason="数量/单价归因后仍有金额差无法解释，需人工核对（不猜）",
            evidence=_evidence(our, supplier, "amount", "amount"),
        )
    else:
        result.unattributed = UnattributedItem(
            ref=supplier.ref,
            amount_diff=amount_diff,
            reason="数量与单价一致但金额不平，七类规则无法解释（DQ-05 金额不平）",
            evidence=_evidence(our, supplier, "amount", "amount"),
        )

    return result


def build_missing_doc_diff(
    line: DocLine,
    *,
    supplier_side: bool,
) -> DiffItem:
    """缺单类差异：一侧有行、另一侧完全没有对应单据。"""
    amount = effective_amount(line) or Money(Decimal(0))
    if supplier_side:
        owner = DiffOwner.SUPPLIER
        suggestion = "供应商计费但我方无对应单据，需补单、拒付或要求提供证据"
    else:
        owner = DiffOwner.SUPPLIER
        suggestion = "我方有单据但对账单未列示，需确认是否已在上期结算或要求补列"
    return DiffItem(
        type=DiffType.MISSING_DOC,
        amount=amount if supplier_side else -amount,
        owner=owner,
        suggestion=suggestion,
        confidence=quantize_confidence(Decimal("0.90")),
        evidence=(line.evidence("doc_no"),),
        ref=line.ref,
        supplier_code=line.supplier_code,
    )


def _conflict_columns(line: DocLine, first: DocLine) -> list[str]:
    """同单号重复列示时，逐列指出与首次列示的冲突，便于供应商核对。"""
    conflicts: list[str] = []
    if line.qty != first.qty:
        conflicts.append(f"数量 {first.qty}→{line.qty}")
    if line.unit != first.unit:
        conflicts.append(f"单位 {first.unit}→{line.unit}")
    price_line, price_first = effective_price(line), effective_price(first)
    if price_line != price_first:
        conflicts.append(f"单价 {price_first}→{price_line}")
    amount_line = effective_amount(line) or Money(Decimal(0))
    amount_first = effective_amount(first) or Money(Decimal(0))
    if amount_line.amount != amount_first.amount:
        conflicts.append(f"金额 {amount_first.amount}→{amount_line.amount}")
    if line.tax_rate != first.tax_rate:
        conflicts.append(f"税率 {first.tax_rate}→{line.tax_rate}")
    return conflicts


def build_duplicate_diff(line: DocLine, first: DocLine, *, kind: str = "exact") -> DiffItem:
    """重复计费差异。

    ``kind="exact"``：整行重复（同单号同内容，或不同单号但内容完全相同）。
    ``kind="conflict"``：同单号同物料二次列示但内容被改动——仍属重复计费，
    但必须把冲突列写进建议，避免被当成「缺单」放过。
    """
    amount = effective_amount(line) or Money(Decimal(0))
    if kind == "conflict":
        conflicts = "、".join(_conflict_columns(line, first)) or "内容不一致"
        suggestion = (
            f"与 {first.ref} 同单号同物料重复列示，冲突列：{conflicts}；"
            "按重复计费处理，需供应商红冲或书面说明后再结"
        )
        confidence = Decimal("0.92")
    else:
        suggestion = f"与 {first.ref} 重复计费，要求删除重复行或下期红冲"
        confidence = Decimal("0.96")
    return DiffItem(
        type=DiffType.DUPLICATE,
        amount=amount,
        owner=DiffOwner.SUPPLIER,
        suggestion=suggestion,
        confidence=quantize_confidence(confidence),
        evidence=(
            line.evidence("doc_no"),
            first.evidence("doc_no"),
            line.evidence("amount"),
        ),
        ref=line.ref,
        supplier_code=line.supplier_code,
    )


def build_uom_diff(line: DocLine, detail: str) -> DiffItem:
    """单位换算缺失/不一致：不计算金额，只标记待人工（绝不按猜测换算）。"""
    return DiffItem(
        type=DiffType.UOM,
        amount=Money(Decimal(0)),
        owner=DiffOwner.SYSTEM,
        suggestion=f"单位口径不一致（{detail}），需补齐换算关系后重算，不参与金额",
        confidence=quantize_confidence(Decimal("0.40")),
        evidence=(line.evidence("unit"),),
        ref=line.ref,
        supplier_code=line.supplier_code,
    )


def is_conclusive(confidence: Decimal) -> bool:
    return confidence >= MIN_CONFIDENCE
