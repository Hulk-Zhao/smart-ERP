"""对账引擎入口：就绪检查 → 重复拦截 → 分层匹配 → 七类归因 → 结论。

对外最小接口（等 S0 的 C7 Agent I/O Schema 冻结后再对齐）：

    reconcile(ReconRequest) -> ReconResult

降级原则（PRD §7.3）：关键主数据/范围缺失时直接返回「无法对账」+ 缺失清单，
不做任何匹配与归因，不输出带猜测的结论。
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import replace
from decimal import Decimal

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
    DiffType,
    DocLine,
    Evidence,
    LineStatus,
    MatchPair,
    MatchResultLine,
    MatchType,
    ReconMetrics,
    ReconRequest,
    ReconResult,
    Tolerance,
    UnattributedItem,
    quantize_confidence,
)

# 差异金额占总计费金额超过该比例 → 整批疑似数据异常，阻断自动处置
ANOMALY_BATCH_RATE = Decimal("0.20")
# 舍入容差累计占总计费金额超过该比例 → 疑似系统性凑整，转人工
ROUNDING_BATCH_RATE = Decimal("0.005")


def _detect_duplicates(
    supplier_lines: Iterable[DocLine],
) -> list[tuple[DocLine, DocLine, str]]:
    """重复计费识别，两个维度，返回 (重复行, 首次行, kind)。

    ① 同单据号同物料二次列示（``kind="conflict"``）：单据号是业务唯一键，
       即使数量/金额被改动也仍是同一笔业务，必须按重复计费处理并指出冲突列，
       否则会被误判成「缺单」。
    ② 跨单据号内容完全相同（``kind="exact"``）：同供应商 + 期间 + 物料 + 单位
       + 数量 + 单价 + 金额 全等，后者视为整行重复。
    """
    lines = list(supplier_lines)
    duplicates: list[tuple[DocLine, DocLine, str]] = []
    flagged: set[str] = set()

    grouped: dict[tuple, list[DocLine]] = {}
    for line in lines:
        if not line.doc_no or not line.item_code:
            continue
        grouped.setdefault(
            (line.supplier_code, line.period, line.doc_no, line.item_code), []
        ).append(line)
    for key in sorted(grouped, key=lambda item: (item[0], item[1] or "", item[2], item[3])):
        group = sorted(grouped[key], key=lambda line: line.line_no)
        if len(group) < 2:
            continue
        first = group[0]
        for extra in group[1:]:
            extra_amount = effective_amount(extra)
            first_amount = effective_amount(first)
            same = (
                extra.qty == first.qty
                and extra.unit == first.unit
                and extra.unit_price == first.unit_price
                and extra_amount is not None
                and first_amount is not None
                and extra_amount.amount == first_amount.amount
                and extra.tax_rate == first.tax_rate
            )
            duplicates.append((extra, first, "exact" if same else "conflict"))
            flagged.add(extra.ref)

    seen: dict[tuple, DocLine] = {}
    for line in lines:
        if line.ref in flagged or line.qty is None:
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
            duplicates.append((line, first, "exact"))
            flagged.add(line.ref)
    return duplicates



def _supplier_in_our_unit(
    pair: MatchPair, request: ReconRequest
) -> tuple[Decimal | None, Decimal | None]:
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


def _unparseable_plan(
    request: ReconRequest,
) -> tuple[set[str], set[str], list[MatchResultLine]]:
    """把不可解析行从匹配中摘出来，并连带摘掉其同单号同行号的对手行。

    返回 (需跳过的我方 ref, 需跳过的供应商 ref, 行级降级结果)。
    对手行一起摘掉，避免「一方不可解析、另一方被判缺单」的假差异。
    """
    if not request.parse_issues:
        return set(), set(), []

    our_by_key = {
        (line.supplier_code, line.doc_no, line.line_no): line
        for line in request.our_lines
    }
    supplier_by_key = {
        (line.supplier_code, line.doc_no, line.line_no): line
        for line in request.supplier_lines
    }

    grouped: dict[str, list] = {}
    for issue in request.parse_issues:
        grouped.setdefault(issue.ref, []).append(issue)

    skip_our: set[str] = set()
    skip_supplier: set[str] = set()
    lines: list[MatchResultLine] = []
    for ref, issues in grouped.items():
        issue = issues[0]
        key = (issue.supplier_code, issue.doc_no, issue.line_no)
        our_ref: str | None = None
        supplier_ref: str | None = None
        if issue.side == "our":
            skip_our.add(ref)
            our_ref = ref
            counterpart = supplier_by_key.get(key)
            if counterpart is not None:
                supplier_ref = counterpart.ref
                skip_supplier.add(counterpart.ref)
        else:
            skip_supplier.add(ref)
            supplier_ref = ref
            counterpart = our_by_key.get(key)
            if counterpart is not None:
                our_ref = counterpart.ref
                skip_our.add(counterpart.ref)
        fields = "、".join(f"{item.field}={item.raw!r}" for item in issues)
        lines.append(
            MatchResultLine(
                item=None,
                qty_mine=None,
                qty_supplier=None,
                amount=None,
                match_type=MatchType.UNMATCHED,
                confidence=Decimal("0.00"),
                status=LineStatus.UNPARSEABLE,
                our_ref=our_ref,
                supplier_ref=supplier_ref,
                note=(
                    f"unparseable：行 {ref} 字段 {fields} 无法解析，"
                    "该行不参与对账（不静默当 0、不猜），需人工修数后重跑"
                ),
            )
        )
    return skip_our, skip_supplier, lines


def reconcile(request: ReconRequest) -> ReconResult:
    skip_our, skip_supplier, unparseable_lines = _unparseable_plan(request)
    our_matchable = [line for line in request.our_lines if line.ref not in skip_our]
    supplier_matchable = [
        line for line in request.supplier_lines if line.ref not in skip_supplier
    ]

    missing = check_readiness(
        replace(
            request,
            our_lines=tuple(our_matchable),
            supplier_lines=tuple(supplier_matchable),
        )
    )
    if missing:
        return _no_conclusion(request, missing)

    our_lines = our_matchable
    all_supplier_lines = list(request.supplier_lines)

    duplicates = _detect_duplicates(supplier_matchable)
    duplicate_refs = {dup.ref for dup, _first, _kind in duplicates}
    clean_supplier = [
        line for line in supplier_matchable if line.ref not in duplicate_refs
    ]

    outcome: MatchOutcome = match_lines(
        our_lines,
        clean_supplier,
        request.tolerance,
        converters=lambda item, from_unit, to_unit: conversion_factor(
            request.master_data, item, from_unit, to_unit
        ),
    )

    lines: list[MatchResultLine] = list(unparseable_lines)
    diffs: list[DiffItem] = []
    unattributed_items: list[UnattributedItem] = []
    evidence: list[Evidence] = []
    manual_line_count = 0

    # ① 重复计费（先扣，避免同一笔被当成两次正常差异）
    for duplicate, first, kind in duplicates:
        diff = build_duplicate_diff(duplicate, first, kind=kind)
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
                note=(
                    f"与 {first.ref} 整行重复"
                    if kind == "exact"
                    else f"与 {first.ref} 同单号同物料但内容冲突（重复计费，非缺单）"
                ),
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

    # ②' 聚合匹配（拆单）：多行供应商行合计对应我方一行。
    #     合计已闭合，可自动放行，但标成 aggregated 便于人工复核时区分直配；
    #     不做单行归因——拆单的分行金额与整单口径本就不同，逐行比会误判数量不符。
    for our_ref, supplier_refs in outcome.aggregate_groups:
        group = [pair for pair in outcome.aggregated if pair.our.ref == our_ref]
        for pair in group:
            others = tuple(ref for ref in supplier_refs if ref != pair.supplier.ref)
            evidence.append(pair.our.evidence("amount"))
            evidence.append(pair.supplier.evidence("amount"))
            lines.append(
                MatchResultLine(
                    item=pair.supplier.item_code or pair.our.item_code,
                    qty_mine=pair.our.qty,
                    qty_supplier=pair.supplier.qty,
                    amount=effective_amount(pair.supplier),
                    match_type=MatchType.AGGREGATED,
                    confidence=pair.confidence,
                    status=LineStatus.MATCHED,
                    our_ref=our_ref,
                    supplier_ref=pair.supplier.ref,
                    aggregate_refs=others,
                    note=(
                        f"聚合匹配：与另外 {len(others)} 行供应商行合计对应我方 "
                        f"{our_ref}（拆单），非直配，人工复核请按合计口径核对"
                    ),
                )
            )

    # ②'' 聚合匹配（合并）：一张供应商行覆盖我方多行，拆分口径不唯一，必须人工确认
    for supplier, our_group, confidence in outcome.merged:
        our_refs = tuple(line.ref for line in our_group)
        evidence.append(supplier.evidence("amount"))
        for line in our_group:
            evidence.append(line.evidence("amount"))
        manual_line_count += 1
        lines.append(
            MatchResultLine(
                item=supplier.item_code,
                qty_mine=sum((line.qty or Decimal(0) for line in our_group), Decimal(0)),
                qty_supplier=supplier.qty,
                amount=effective_amount(supplier),
                match_type=MatchType.AGGREGATED,
                confidence=confidence,
                status=LineStatus.NEEDS_REVIEW,
                our_ref=our_refs[0] if our_refs else None,
                supplier_ref=supplier.ref,
                aggregate_refs=our_refs,
                note=(
                    f"聚合匹配：一张供应商行覆盖我方 {len(our_refs)} 行合并开票，"
                    "拆分口径不唯一，需人工确认后放行"
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

    metrics = _build_metrics(
        all_supplier_lines,
        outcome,
        diffs,
        len(duplicates),
        manual_line_count + len(unparseable_lines),
        request.tolerance,
    )

    has_manual = bool(
        outcome.ambiguous
        or outcome.candidate_only
        or unattributed_items
        or unparseable_lines
        or any(diff.needs_manual for diff in diffs)
        or manual_line_count
    )
    if has_manual or metrics.suspected_anomaly or metrics.rounding_escalated:
        batch_status = BatchStatus.NEEDS_REVIEW
        if unparseable_lines:
            reason = (
                f"{len(unparseable_lines)} 行金额/数量无法解析（unparseable），"
                "已按行降级并从匹配中摘出，整批转人工；其余可解析行照常对账"
            )
        elif metrics.rounding_escalated:
            reason = (
                "舍入容差累计超限（单批累计 "
                f"{metrics.rounding_amount} 元 > 上限 "
                f"{request.tolerance.rounding_batch_limit} 元或占比超 "
                f"{ROUNDING_BATCH_RATE}），疑似系统性凑整，需人工复核"
            )
        else:
            reason = (
                "存在匹配歧义/低置信度/无法归因项，或差异率超阈值，需人工复核后再出结论"
            )
    else:
        batch_status = BatchStatus.RECONCILED
        if metrics.rounding_lines:
            reason = (
                "全部差异已归入七类口径，无歧义项；"
                f"{metrics.rounding_lines} 行金额舍入差 "
                f"{metrics.rounding_amount} 元在容差内已放行（明细可审计）"
            )
        else:
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
    tolerance: Tolerance | None = None,
) -> ReconMetrics:
    total = len(supplier_lines)
    matched = len(outcome.pairs) + len(outcome.aggregated) + len(outcome.merged)
    matched_rate = (
        quantize_confidence(Decimal(matched) / Decimal(total)) if total else Decimal(0)
    )

    supplier_total = Decimal(0)
    for line in supplier_lines:
        amount = effective_amount(line)
        if amount is not None:
            supplier_total += abs(amount.amount)

    # 舍入容差是「容差调整」，不是七类归因差异：单独统计，不进差异金额/差异率，
    # 否则既会虚高差异率误触异常闸门，也会让差异明细占比失真。
    rounding_diffs = [diff for diff in diffs if diff.type is DiffType.ROUNDING_TOLERANCE]
    business_diffs = [diff for diff in diffs if diff.type is not DiffType.ROUNDING_TOLERANCE]

    diff_amount = sum((abs(diff.amount.amount) for diff in business_diffs), Decimal(0))
    diff_rate = (
        quantize_confidence(diff_amount / supplier_total)
        if supplier_total > 0
        else Decimal(0)
    )

    rounding_amount = sum((abs(diff.amount.amount) for diff in rounding_diffs), Decimal(0))
    limit = tolerance.rounding_batch_limit if tolerance is not None else Decimal("1.00")
    rounding_escalated = bool(rounding_diffs) and (
        rounding_amount > limit
        or (supplier_total > 0 and rounding_amount / supplier_total > ROUNDING_BATCH_RATE)
    )

    diff_line_refs = {diff.ref for diff in business_diffs}
    suspected = diff_rate > ANOMALY_BATCH_RATE
    auto_allowed = not suspected and manual_line_count == 0 and not rounding_escalated

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
        aggregated=len(outcome.aggregated) + len(outcome.merged),
        rounding_lines=len(rounding_diffs),
        rounding_amount=rounding_amount,
        rounding_escalated=rounding_escalated,
    )


def empty_result(reason: str = "无输入行") -> ReconResult:
    """空批次：明确返回「无结论」，不给空结论冒充已对账。"""
    return ReconResult(
        batch_status=BatchStatus.NO_CONCLUSION,
        conclusive=False,
        status_reason=reason,
        metrics=_empty_metrics(),
    )


__all__ = ["empty_result", "reconcile"]
