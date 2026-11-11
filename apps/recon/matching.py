"""匹配层（FR-103）：单据号直配 → 聚合匹配 → 业务键模糊配 → 候选生成。

规则要点：
- 直配：单据号一致即 1.00；
- 聚合匹配：多行合计等于单行时按聚合配对（拆单=多供应商行对一行我方行，
  合并=一张供应商行覆盖多行我方行），置信度低于直配且单独标注 ``aggregated``，
  便于人工复核时区分；合并方向一律不自动放行，必须转人工确认拆分口径；
- 模糊配：物料 + 数量（±容差）+ 期间 + 单价 打分，低于 0.70 不自动通过；
  单位不一致时先按主数据换算到同一口径再比，换算不可得才按中性降权，
  并在候选原因里区分「不可换算」与「相似度低」；
- 单位守门（P0）：单位不同必须显式换算且换算后「数量与金额同时闭合」才允许
  自动匹配；否则即使数量/金额字面相等也只给候选转人工（100 BOX ≠ 100 PCS）；
- 多候选置信度差 < 0.10 → 「匹配歧义」，只给候选不判定（F3）；
- 无任何候选 → 未匹配，进差异清单（缺单类）。
"""

from __future__ import annotations

import itertools
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal

from .masterdata import normalize_unit
from .models import (
    AMBIGUITY_GAP,
    MIN_CONFIDENCE,
    Candidate,
    DocLine,
    MatchPair,
    MatchType,
    Tolerance,
    quantize_confidence,
)

# 打分权重（合计 1.00），确定性规则，不依赖模型
W_ITEM = Decimal("0.40")
W_QTY = Decimal("0.35")
W_PRICE = Decimal("0.15")
W_PERIOD = Decimal("0.10")

# 单位换算回调：(物料编码, 供应商单位, 我方单位) -> 系数；不可换算返回 None
Converter = Callable[[str | None, str | None, str | None], Decimal | None]

# 无法换算时数量/单价的降权分：不判 0（会误报缺单），也不给满分（会误配）
UNCONVERTIBLE_SCORE = Decimal("0.50")
# 单位不可换算时总分上限：必须低于 MIN_CONFIDENCE，只给候选转人工。
# 否则「数量/单价根本没得比」会被物料+期间拉到 0.75 直接自动匹配。
UNCONVERTIBLE_CAP = Decimal("0.65")

# 单位不同且换算后未同时闭合时，总分同样封顶：字面「数量相等」不能当同一笔业务
UNIT_MISMATCH_CAP = UNCONVERTIBLE_CAP

# 聚合匹配置信度：合计闭合度已校验，但依赖多行归组，低于直配/单行模糊配
AGGREGATED_CONFIDENCE = Decimal("0.90")
MERGED_CONFIDENCE = Decimal("0.85")

# 合并方向的组合枚举上限，避免组合爆炸
MERGE_MAX_CANDIDATES = 4
MERGE_MAX_SIZE = 3


@dataclass
class MatchOutcome:
    pairs: list[MatchPair] = field(default_factory=list)
    # 拆单：多行供应商行合计匹配同一我方行
    aggregated: list[MatchPair] = field(default_factory=list)
    # 每组聚合匹配：(我方行 ref, (供应商行 ref, ...))
    aggregate_groups: list[tuple[str, tuple[str, ...]]] = field(default_factory=list)
    # 合并：一张供应商行覆盖多行我方行，必须人工确认
    merged: list[tuple[DocLine, tuple[DocLine, ...], Decimal]] = field(
        default_factory=list
    )
    ambiguous: list[tuple[DocLine, list[Candidate]]] = field(default_factory=list)
    candidate_only: list[tuple[DocLine, list[Candidate]]] = field(default_factory=list)
    unmatched_supplier: list[DocLine] = field(default_factory=list)
    unmatched_our: list[DocLine] = field(default_factory=list)


def _normalize_name(value: str | None) -> str:
    if not value:
        return ""
    return "".join(value.split()).upper()


def _similarity(left: Decimal, right: Decimal) -> Decimal:
    scale = max(abs(left), abs(right))
    if scale == 0:
        return Decimal(1)
    ratio = abs(left - right) / scale
    return max(Decimal(0), Decimal(1) - ratio)


def _amount_of(line: DocLine) -> Decimal | None:
    if line.amount is not None:
        return line.amount.amount
    if line.qty is not None and line.unit_price is not None:
        return line.qty * line.unit_price
    return None


def _comparable(
    our: DocLine, supplier: DocLine, converters: Converter | None
) -> tuple[Decimal | None, Decimal | None, str]:
    """把供应商数量/单价折算到我方单位口径。

    返回 (数量, 单价, 换算说明)；换算说明为空表示两侧单位一致或无可比单位。
    换算不可得时返回 (None, None, "unconvertible")，由调用方按中性分处理。
    """
    if not our.unit or not supplier.unit or our.unit == supplier.unit:
        return supplier.qty, supplier.unit_price, ""
    factor = converters(our.item_code, supplier.unit, our.unit) if converters else None
    if factor is None or factor == 0:
        return None, None, "unconvertible"
    qty = None if supplier.qty is None else supplier.qty * factor
    price = None if supplier.unit_price is None else supplier.unit_price / factor
    return qty, price, f"单位按 {supplier.unit}→{our.unit}×{factor} 换算后比较"


def unit_guard(
    our: DocLine,
    supplier: DocLine,
    converters: Converter | None,
    tolerance: Tolerance,
) -> tuple[bool, str]:
    """单位守门：不同单位必须换算，且换算后「数量与金额同时闭合」才可自动匹配。

    返回 (是否允许自动, 原因)；原因为空表示两侧单位一致。
    """
    if not our.unit or not supplier.unit:
        return False, "单位缺失，禁止自动匹配"
    if normalize_unit(our.unit) == normalize_unit(supplier.unit):
        return True, ""
    factor = converters(our.item_code, supplier.unit, our.unit) if converters else None
    if factor is None or factor == 0:
        return False, (
            f"单位不一致（{supplier.unit} vs {our.unit}，缺换算关系），不可换算，"
            "禁止自动匹配 [UNCONVERTIBLE]"
        )
    converted_qty = None if supplier.qty is None else supplier.qty * factor
    if not _close(our.qty, converted_qty, tolerance.qty):
        return False, (
            f"单位不一致（{supplier.unit}→{our.unit}×{factor} 换算后数量不闭合："
            f"{converted_qty} vs {our.qty}），禁止自动匹配"
        )
    if not _close(_amount_of(our), _amount_of(supplier), tolerance.amount):
        return False, (
            f"单位不一致（{supplier.unit}→{our.unit}×{factor} 换算后金额不闭合），禁止自动匹配"
        )
    return True, f"单位按 {supplier.unit}→{our.unit}×{factor} 换算后数量与金额闭合"


def _score(
    our: DocLine,
    supplier: DocLine,
    converters: Converter | None,
    tolerance: Tolerance | None = None,
) -> tuple[Decimal | None, str]:
    """业务键相似度 + 低分原因；物料不可比返回 (None, 原因)。"""
    if our.supplier_code != supplier.supplier_code:
        return None, "供应商不一致，不参与匹配"

    if our.item_code and our.item_code == supplier.item_code:
        item_score = Decimal("1.00")
    elif _normalize_name(our.item_name) and _normalize_name(
        our.item_name
    ) == _normalize_name(supplier.item_name):
        item_score = Decimal("0.80")
    else:
        return None, "物料编码与名称都对不上，不跨物料匹配"

    if our.qty is None or supplier.qty is None:
        return None, "数量缺失，无法比较"

    tolerance = tolerance or Tolerance()
    unit_ok, unit_reason = unit_guard(our, supplier, converters, tolerance)
    if not unit_ok:
        # 单位不同且不可换算/换算后不闭合：数量与单价都不可比，按中性降权并封顶，
        # 只给候选转人工；原因码写明「单位不一致」。
        qty_score = UNCONVERTIBLE_SCORE
        price_score = UNCONVERTIBLE_SCORE
        unit_note = unit_reason
        capped = True
    else:
        qty_value, price_value, unit_note = _comparable(our, supplier, converters)
        capped = False
        qty_score = _similarity(our.qty, qty_value)
        if our.unit_price is not None and price_value is not None:
            price_score = _similarity(our.unit_price, price_value)
        else:
            # 一侧缺价：不给满分也不给 0，按中性处理（缺失≠零）
            price_score = Decimal("0.50")

    if our.period and supplier.period:
        period_score = Decimal(1) if our.period == supplier.period else Decimal(0)
    else:
        period_score = Decimal("0.50")

    score = (
        W_ITEM * item_score
        + W_QTY * qty_score
        + W_PRICE * price_score
        + W_PERIOD * period_score
    )
    if capped:
        score = min(score, UNIT_MISMATCH_CAP)
    return quantize_confidence(score), unit_note


def score_pair(
    our: DocLine,
    supplier: DocLine,
    converters: Converter | None = None,
    tolerance: Tolerance | None = None,
) -> Decimal | None:
    """业务键相似度；物料不可比（编码与名称都对不上）返回 None，不做跨物料匹配。"""
    return _score(our, supplier, converters, tolerance)[0]


def _top_candidates(
    scored: list[tuple[Decimal, int, str]], our_lines: list[DocLine]
) -> list[Candidate]:
    return [
        Candidate(
            line=our_lines[index],
            confidence=score,
            reason=(reason or "业务键相似度低"),
        )
        for score, index, reason in scored[:3]
    ]


# 候选行「占位」阈值：达到该相似度说明是同一笔业务的其他可能来源，
# 不应再被误报成「缺单」；低于该值仍按未匹配处理。
CANDIDATE_CONSUME_SCORE = Decimal("0.50")


def _consume_plausible(
    scored: list[tuple[Decimal, int, str]], used_our: set[int]
) -> None:
    for score, index, _reason in scored:
        if score >= CANDIDATE_CONSUME_SCORE:
            used_our.add(index)


def _sum_qty(lines: list[DocLine]) -> Decimal | None:
    total = Decimal(0)
    for line in lines:
        if line.qty is None:
            return None
        total += line.qty
    return total


def _sum_amount(lines: list[DocLine]) -> Decimal | None:
    total = Decimal(0)
    for line in lines:
        amount = _amount_of(line)
        if amount is None:
            return None
        total += amount
    return total


def _close(left: Decimal | None, right: Decimal | None, tolerance: Decimal) -> bool:
    if left is None or right is None:
        return False
    return abs(left - right) <= tolerance


def _match_merged(
    our_lines: list[DocLine],
    supplier_lines: list[DocLine],
    tolerance: Tolerance,
    used_our: set[int],
    matched_supplier: set[int],
    outcome: MatchOutcome,
    our_by_sku_unit: dict[tuple[str, str, str | None], list[int]],
) -> None:
    """一对多：一张供应商行覆盖多行我方行（合并账单），必须人工确认。"""
    for sup_index, supplier in enumerate(supplier_lines):
        if sup_index in matched_supplier or not supplier.item_code:
            continue
        if supplier.qty is None:
            continue
        candidates = [
            index
            for index in our_by_sku_unit.get(
                (supplier.supplier_code, supplier.item_code, supplier.unit), ()
            )
            if index not in used_our and our_lines[index].qty is not None
        ]
        if len(candidates) < 2 or len(candidates) > MERGE_MAX_CANDIDATES:
            continue
        # 单行已经能对上时不做合并，避免把正常单行匹配拆成聚合
        if any(
            abs(our_lines[index].qty - supplier.qty) <= tolerance.qty
            for index in candidates
        ):
            continue
        supplier_amount = _amount_of(supplier)
        picked: tuple[int, ...] | None = None
        for size in range(2, min(MERGE_MAX_SIZE, len(candidates)) + 1):
            for combo in itertools.combinations(candidates, size):
                group = [our_lines[index] for index in combo]
                if not _close(_sum_qty(group), supplier.qty, tolerance.qty):
                    continue
                if not _close(_sum_amount(group), supplier_amount, tolerance.amount):
                    continue
                picked = combo
                break
            if picked is not None:
                break
        if picked is None:
            continue
        for index in picked:
            used_our.add(index)
        matched_supplier.add(sup_index)
        outcome.merged.append(
            (
                supplier,
                tuple(our_lines[index] for index in picked),
                MERGED_CONFIDENCE,
            )
        )


def _match_aggregated(
    our_lines: list[DocLine],
    supplier_lines: list[DocLine],
    tolerance: Tolerance,
    used_our: set[int],
    matched_supplier: set[int],
    outcome: MatchOutcome,
    our_by_sku_unit: dict[tuple[str, str, str | None], list[int]],
) -> None:
    """多对一：同物料多行供应商行合计匹配同一我方行（拆单）。"""
    groups: dict[tuple[str, str, str | None, str], list[int]] = {}
    for sup_index, supplier in enumerate(supplier_lines):
        if sup_index in matched_supplier or not supplier.item_code:
            continue
        if supplier.qty is None or not supplier.unit:
            continue
        key = (
            supplier.supplier_code,
            supplier.item_code,
            supplier.period,
            supplier.unit,
        )
        groups.setdefault(key, []).append(sup_index)

    for key in sorted(
        groups, key=lambda item: (item[0], item[1], item[2] or "", item[3])
    ):
        indexes = groups[key]
        if len(indexes) < 2:
            continue
        group = [supplier_lines[index] for index in indexes]
        total_qty = _sum_qty(group)
        total_amount = _sum_amount(group)
        if total_qty is None:
            continue
        for our_index in our_by_sku_unit.get((key[0], key[1], key[3]), ()):
            if our_index in used_our:
                continue
            our = our_lines[our_index]
            if our.qty is None:
                continue
            if not _close(total_qty, our.qty, tolerance.qty):
                continue
            if not _close(total_amount, _amount_of(our), tolerance.amount):
                continue
            # 组内单行已能独立对上时不做聚合，避免抢占正常单行匹配
            if any(abs(line.qty - our.qty) <= tolerance.qty for line in group):
                continue
            used_our.add(our_index)
            for index in indexes:
                matched_supplier.add(index)
                outcome.aggregated.append(
                    MatchPair(
                        our=our,
                        supplier=supplier_lines[index],
                        match_type=MatchType.AGGREGATED,
                        confidence=AGGREGATED_CONFIDENCE,
                    )
                )
            outcome.aggregate_groups.append(
                (our.ref, tuple(supplier_lines[index].ref for index in indexes))
            )
            break


def match_lines(
    our_lines: list[DocLine],
    supplier_lines: list[DocLine],
    tolerance: Tolerance,
    converters: Converter | None = None,
) -> MatchOutcome:
    """对供应商对账行做分层匹配；同一我方行只被消费一次。"""
    outcome = MatchOutcome()
    used_our: set[int] = set()
    matched_supplier: set[int] = set()

    # ① 单据号直配（同供应商 + 同单据号；行号一致优先）
    # 索引一次建好：直配按单据号、合并/聚合按「供应商+物料+单位」、模糊配按
    # 「供应商+物料编码」与「供应商+规范化物料名称」两个键。桶内候选与原全量
    # 扫描的候选集合完全一致，只是把 O(n²) 降到 O(n·桶内行)。
    our_by_doc: dict[tuple[str, str], list[int]] = {}
    our_by_sku: dict[tuple[str, str], list[int]] = {}
    our_by_name: dict[tuple[str, str], list[int]] = {}
    our_by_sku_unit: dict[tuple[str, str, str | None], list[int]] = {}
    for index, line in enumerate(our_lines):
        our_by_doc.setdefault((line.supplier_code, line.doc_no), []).append(index)
        if line.item_code:
            our_by_sku.setdefault((line.supplier_code, line.item_code), []).append(
                index
            )
            our_by_sku_unit.setdefault(
                (line.supplier_code, line.item_code, line.unit), []
            ).append(index)
        name = _normalize_name(line.item_name)
        if name:
            our_by_name.setdefault((line.supplier_code, name), []).append(index)

    for sup_index, supplier in enumerate(supplier_lines):
        free = [
            index
            for index in our_by_doc.get((supplier.supplier_code, supplier.doc_no), [])
            if index not in used_our
        ]
        if not free:
            continue
        same_line = [
            index for index in free if our_lines[index].line_no == supplier.line_no
        ]
        ordered = same_line + [index for index in free if index not in same_line]
        # 单位守门：单据号相同也不能绕过单位校验（100 BOX 与 100 PCS 不是同一笔数量）。
        # 不满足时不直配，留给模糊配生成候选并写明「单位不一致」。
        picked = next(
            (
                index
                for index in ordered
                if unit_guard(our_lines[index], supplier, converters, tolerance)[0]
            ),
            None,
        )
        if picked is None:
            continue
        used_our.add(picked)
        matched_supplier.add(sup_index)
        outcome.pairs.append(
            MatchPair(
                our=our_lines[picked],
                supplier=supplier,
                match_type=MatchType.EXACT,
                confidence=Decimal("1.00"),
            )
        )

    # ② 聚合匹配（一行对多行 / 多行对一行），先于模糊配避免拆单首行被单行吃掉
    _match_merged(
        our_lines,
        supplier_lines,
        tolerance,
        used_our,
        matched_supplier,
        outcome,
        our_by_sku_unit,
    )
    _match_aggregated(
        our_lines,
        supplier_lines,
        tolerance,
        used_our,
        matched_supplier,
        outcome,
        our_by_sku_unit,
    )

    # ③ 模糊配 + 候选生成
    for sup_index, supplier in enumerate(supplier_lines):
        if sup_index in matched_supplier:
            continue
        candidate_indexes: set[int] = set()
        if supplier.item_code:
            candidate_indexes.update(
                our_by_sku.get((supplier.supplier_code, supplier.item_code), ())
            )
        supplier_name = _normalize_name(supplier.item_name)
        if supplier_name:
            candidate_indexes.update(
                our_by_name.get((supplier.supplier_code, supplier_name), ())
            )
        scored: list[tuple[Decimal, int, str]] = []
        for index in sorted(candidate_indexes):
            if index in used_our:
                continue
            score, reason = _score(our_lines[index], supplier, converters, tolerance)
            if score is None:
                continue
            scored.append((score, index, reason))
        if not scored:
            outcome.unmatched_supplier.append(supplier)
            continue

        scored.sort(key=lambda item: (-item[0], item[1]))
        best_score, best_index, _best_reason = scored[0]

        if best_score < MIN_CONFIDENCE:
            # 置信度不足：只给候选，绝不自动通过
            outcome.candidate_only.append(
                (supplier, _top_candidates(scored, our_lines))
            )
            _consume_plausible(scored, used_our)
            continue

        if len(scored) > 1 and (best_score - scored[1][0]) < AMBIGUITY_GAP:
            # 多候选且置信度接近：不判定，交人工裁决（F3）
            outcome.ambiguous.append((supplier, _top_candidates(scored, our_lines)))
            _consume_plausible(scored, used_our)
            continue

        used_our.add(best_index)
        outcome.pairs.append(
            MatchPair(
                our=our_lines[best_index],
                supplier=supplier,
                match_type=MatchType.FUZZY,
                confidence=best_score,
            )
        )

    outcome.unmatched_our = [
        line for index, line in enumerate(our_lines) if index not in used_our
    ]
    return outcome
