"""对账引擎入口：就绪检查 → 重复拦截 → 三层匹配 → 七类归因 → 结论。

对外最小接口（等 S0 的 C7 Agent I/O Schema 冻结后再对齐）：

    reconcile(ReconRequest) -> ReconResult

降级原则（PRD §7.3）：关键主数据/范围缺失时直接返回「无法对账」+ 缺失清单，
不做任何匹配与归因，不输出带猜测的结论。
"""

from __future__ import annotations

from decimal import Decimal
from typing import Iterable, Optional

from .attribution import (
    attribute_pair,
    build_duplicate_diff,
    build_missing_doc_diff,
    effective_amount,
    effective_price,
)
from .drafts import build_discrepancy_drafts
from .masterdata import check_readiness, conversion_factor
from .matching import MatchOutcome, match_lines
from .models import (
    BatchStatus,
    Candidate,
    DiffItem,
    DocLine,
    Evidence,
    LineStatus,
    MatchPair,
    MatchResultLine,
    MatchType,
    Money,
    ReconMetrics,
    ReconRequest,
    ReconResult,
    UnattributedItem,
    quantize_confidence,
)

# 差异金额占总计费金额超过该比例 → 整批疑似数据异常，阻断自动处置
ANOMALY_BATCH_RATE = Decimal("0.20")


def _detect_duplicates(
    supplier_lines: Iterable[DocLine],
) -> list[tuple[DocLine, DocLine]]:
    """重复计费识别：同供应商 + 期间 + 物料 + 数量 + 单价/金额 出现多次，后者视为重复。"""
    seen: dict[tuple, DocLine] = {}
    duplicates: list[tuple[DocLine, DocLine]] = []
    for line in supplier_lines:
        if line.qty is None:
            continue
        price = effective_price(line)
        amount = effective_amount(line)
        key = (
            line.supplier_code,
            line.period,
            line.item_code,
            line.unit,
            line.qty,
            price,
            None if amount is None else amount.amount,
        )
        first = seen.get(key)
        if first is None:
            seen[key] = line
        else:
            duplicates.append((line, first))
    return duplicates


def _supplier_in_our_unit(
    pair: MatchPair, request: ReconRequest
) -> tuple[Optional[Decimal], Optional[Decimal]]:
    """把供应商数量/单价换算到我方单位口径；换算缺失返回 (None, None)。"""
    our, supplier = pair.our, pair.supplier
    if supplier.qty is None:
        return None, None
    if supplier.unit == our.unit:
        return supplier.qty, effective_price(supplier)
    factor = conversion_factor(
        request.master_data, our.item_code, supplier.unit, our.unit
    )
    if factor is None or factor == 0:
        return None, None
    price = effective_price(supplier)
    return supplier.qty * factor, (None if price is None else price / factor)


def _no_conclusion(request: ReconRequest, missing) -> ReconResult:
    return ReconResult(
        batch_status=BatchStatus.NO_CONCLUSION,
        conclusive=False,
        status_reason=(
            "关键主数据或范围缺失，按「宁可不出结论，不可出错结论」原则不执行对账；"
            f"缺失 {len(missing)} 项，见 missing_items"
        ),
        missing_items=tuple(missing),
    )


def _empty_metrics() -> ReconMetrics:
    zero = Decimal(0)
    return ReconMetrics(
        supplier_lines=0,
        matched=0,
        matched_rate=zero,
        ambiguous=0,
        candidate_only=0,
        unmatched=0,
        diff_lines=0,
        diff_rate=zero,
        diff_amount=zero,
        suspected_anomaly=False,
        auto_disposal_allowed=False,
    )


def _candidate_line(
    supplier: DocLine,
    candidates: list[Candidate],
    status: LineStatus,
    note: str,
) -> MatchResultLine:
    best = candidates[0] if candidates else None
    return MatchResultLine(
        item=supplier.item_code,
        qty_mine=None,
        qty_supplier=supplier.qty,
        amount=effective_amount(supplier),
        match_type=MatchType.CANDIDATE,
        confidence=best.confidence if best else Decimal("0.00"),
        status=status,
        supplier_ref=supplier.ref,
        candidates=tuple(candidates),
        note=note,
    )


def reconcile(request: ReconRequest) -> ReconResult:
    missing = check_readiness(request)
    if missing:
        return _no_conclusion(request, missing)

    our_lines = list(request.our_lines)
    all_supplier_lines = list(request.supplier_lines)

    duplicates = _detect_duplicates(all_supplier_lines)
    duplicate_refs = {dup.ref for dup, _ in duplicates}
    clean_supplier = [line for line in all_supplier_lines if line.ref not in duplicate_refs]

    outcome: MatchOutcome = match_lines(our_lines, clean_supplier, request.tolerance)

    lines: list[MatchResultLine] = []
    diffs: list[DiffItem] = []
    unattributed_items: list[UnattributedItem] = []
    evidence: list[Evidence] = []
    manual_line_count = 0

    # ① 重复计费（先扣，避免同一笔被当成两次正常差异）
    for duplicate, first in duplicates:
        diff = build_duplicate_diff(duplicate, first)
        diffs.append(diff)
        evidence.extend(diff.evidence)
        lines.append(
            MatchResultLine(
                item=duplicate.item_code,
                qty_mine=None,
                qty_supplier=duplicate.qty,
                amount=effective_amount(duplicate),
                match_type=MatchType.UNMATCHED,
                confidence=diff.confidence,
                status=LineStatus.DUPLICATE,
                supplier_ref=duplicate.ref,
                diffs=(diff,),
                note=f"与 {first.ref} 重复",
            )
        )

    # ② 已匹配行：数量单位归一 → 差异计算与归因
    for pair in outcome.pairs:
        supplier_qty, supplier_price = _supplier_in_our_unit(pair, request)
        attribution = attribute_pair(pair, request.tolerance, supplier_qty, supplier_price)
        status = (
            LineStatus.NEEDS_REVIEW
            if attribution.needs_manual
            else LineStatus.MATCHED
        )
        if status is LineStatus.NEEDS_REVIEW:
            manual_line_count += 1
        diffs.extend(attribution.diffs)
        if attribution.unattributed is not None:
            unattributed_items.append(attribution.unattributed)
            evidence.extend(attribution.unattributed.evidence)
        for diff in attribution.diffs:
            evidence.extend(diff.evidence)
        evidence.append(pair.our.evidence("amount"))
        evidence.append(pair.supplier.evidence("amount"))

        lines.append(
            MatchResultLine(
                item=pair.supplier.item_code or pair.our.item_code,
                qty_mine=pair.our.qty,
                qty_supplier=supplier_qty,
                amount=effective_amount(pair.supplier) or effective_amount(pair.our),
                match_type=pair.match_type,
                confidence=pair.confidence,
                status=status,
                our_ref=pair.our.ref,
                supplier_ref=pair.supplier.ref,
                diffs=tuple(attribution.diffs),
                note=(
                    "归因置信度低于 0.70，待人工"
                    if attribution.needs_manual
                    else ""
                ),
            )
        )

    # ③ 匹配歧义：只给候选，不判定（F3）
    for supplier, candidates in outcome.ambiguous:
        lines.append(
            _candidate_line(
                supplier,
                candidates,
                LineStatus.AMBIGUOUS,
                "多候选且置信度差 < 0.10，需人工裁决（F3）",
            )
        )
        manual_line_count += 1

    # ④ 候选置信度不足 0.70：不得自动通过
    for supplier, candidates in outcome.candidate_only:
        lines.append(
            _candidate_line(
                supplier,
                candidates,
                LineStatus.CANDIDATE_ONLY,
                "候选置信度低于 0.70，待人工确认",
            )
        )
        manual_line_count += 1

    # ⑤ 缺单：供应商有、我方无 / 我方有、供应商无
    for supplier in outcome.unmatched_supplier:
        diff = build_missing_doc_diff(supplier, supplier_side=True)
        diffs.append(diff)
        evidence.extend(diff.evidence)
        lines.append(
            MatchResultLine(
                item=supplier.item_code,
                qty_mine=None,
                qty_supplier=supplier.qty,
                amount=effective_amount(supplier),
                match_type=MatchType.UNMATCHED,
                confidence=diff.confidence,
                status=LineStatus.UNMATCHED_SUPPLIER,
                supplier_ref=supplier.ref,
                diffs=(diff,),
                note="供应商计费，我方无对应单据",
            )
        )

    for our in outcome.unmatched_our:
        diff = build_missing_doc_diff(our, supplier_side=False)
        diffs.append(diff)
        evidence.extend(diff.evidence)
        lines.append(
            MatchResultLine(
                item=our.item_code,
                qty_mine=our.qty,
                qty_supplier=None,
                amount=effective_amount(our),
                match_type=MatchType.UNMATCHED,
                confidence=diff.confidence,
                status=LineStatus.UNMATCHED_OURS,
                our_ref=our.ref,
                diffs=(diff,),
                note="我方单据未在对账单列示",
            )
        )

    metrics = _build_metrics(all_supplier_lines, outcome, diffs, len(duplicates), manual_line_count)

    has_manual = bool(
        outcome.ambiguous
        or outcome.candidate_only
        or unattributed_items
        or any(diff.needs_manual for diff in diffs)
        or manual_line_count
    )
    if has_manual or metrics.suspected_anomaly:
        batch_status = BatchStatus.NEEDS_REVIEW
        reason = (
            "存在匹配歧义/低置信度/无法归因项，或差异率超阈值，需人工复核后再出结论"
        )
    else:
        batch_status = BatchStatus.RECONCILED
        reason = "全部差异已归入七类口径，无歧义项"

    if not evidence and lines:
        first_line = lines[0]
        if first_line.our_ref:
            evidence.append(Evidence(system="ERP", doc_no=first_line.our_ref, field="amount"))

    return ReconResult(
        batch_status=batch_status,
        conclusive=batch_status is BatchStatus.RECONCILED,
        status_reason=reason,
        missing_items=(),
        lines=tuple(lines),
        diffs=tuple(diffs),
        unattributed=tuple(unattributed_items),
        discrepancy_drafts=build_discrepancy_drafts(request, diffs),
        metrics=metrics,
        evidence=tuple(_dedupe_evidence(evidence)),
    )


def _dedupe_evidence(items: Iterable[Evidence]) -> list[Evidence]:
    seen: set[tuple[str, str, str]] = set()
    result: list[Evidence] = []
    for item in items:
        key = (item.system, item.doc_no, item.field)
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result


def _build_metrics(
    supplier_lines: list[DocLine],
    outcome: MatchOutcome,
    diffs: list[DiffItem],
    duplicate_count: int,
    manual_line_count: int,
) -> ReconMetrics:
    total = len(supplier_lines)
    matched = len(outcome.pairs)
    matched_rate = (
        quantize_confidence(Decimal(matched) / Decimal(total)) if total else Decimal(0)
    )

    supplier_total = Decimal(0)
    for line in supplier_lines:
        amount = effective_amount(line)
        if amount is not None:
            supplier_total += abs(amount.amount)

    diff_amount = sum((abs(diff.amount.amount) for diff in diffs), Decimal(0))
    diff_rate = (
        quantize_confidence(diff_amount / supplier_total)
        if supplier_total > 0
        else Decimal(0)
    )

    diff_line_refs = {diff.ref for diff in diffs}
    suspected = diff_rate > ANOMALY_BATCH_RATE
    auto_allowed = not suspected and manual_line_count == 0

    return ReconMetrics(
        supplier_lines=total,
        matched=matched,
        matched_rate=matched_rate,
        ambiguous=len(outcome.ambiguous),
        candidate_only=len(outcome.candidate_only),
        unmatched=len(outcome.unmatched_supplier) + len(outcome.unmatched_our),
        diff_lines=len(diff_line_refs) + duplicate_count,
        diff_rate=diff_rate,
        diff_amount=diff_amount,
        suspected_anomaly=suspected,
        auto_disposal_allowed=auto_allowed,
    )


def empty_result(reason: str = "无输入行") -> ReconResult:
    """空批次：明确返回「无结论」，不给空结论冒充已对账。"""
    return ReconResult(
        batch_status=BatchStatus.NO_CONCLUSION,
        conclusive=False,
        status_reason=reason,
        metrics=_empty_metrics(),
    )


__all__ = ["reconcile", "empty_result"]
