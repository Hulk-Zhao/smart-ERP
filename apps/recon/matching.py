"""三层匹配（FR-103）：单据号直配 → 业务键模糊配 → 候选生成。

规则要点：
- 直配：单据号一致即 1.00；
- 模糊配：物料 + 数量（±容差）+ 期间 + 单价 打分，低于 0.70 不自动通过；
- 多候选置信度差 < 0.10 → 「匹配歧义」，只给候选不判定（F3）；
- 无任何候选 → 未匹配，进差异清单（缺单类）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

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


@dataclass
class MatchOutcome:
    pairs: list[MatchPair] = field(default_factory=list)
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


def score_pair(our: DocLine, supplier: DocLine) -> Decimal | None:
    """业务键相似度；物料不可比（编码与名称都对不上）返回 None，不做跨物料匹配。"""
    if our.supplier_code != supplier.supplier_code:
        return None

    if our.item_code and our.item_code == supplier.item_code:
        item_score = Decimal("1.00")
    elif _normalize_name(our.item_name) and _normalize_name(
        our.item_name
    ) == _normalize_name(supplier.item_name):
        item_score = Decimal("0.80")
    else:
        return None

    if our.qty is None or supplier.qty is None:
        return None
    qty_score = _similarity(our.qty, supplier.qty)

    if our.unit_price is not None and supplier.unit_price is not None:
        price_score = _similarity(our.unit_price, supplier.unit_price)
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
    return quantize_confidence(score)


def _top_candidates(scored: list[tuple[Decimal, int]], our_lines: list[DocLine]) -> list[Candidate]:
    return [
        Candidate(line=our_lines[index], confidence=score)
        for score, index in scored[:3]
    ]


# 候选行「占位」阈值：达到该相似度说明是同一笔业务的其他可能来源，
# 不应再被误报成「缺单」；低于该值仍按未匹配处理。
CANDIDATE_CONSUME_SCORE = Decimal("0.50")


def _consume_plausible(
    scored: list[tuple[Decimal, int]], used_our: set[int]
) -> None:
    for score, index in scored:
        if score >= CANDIDATE_CONSUME_SCORE:
            used_our.add(index)


def match_lines(
    our_lines: list[DocLine],
    supplier_lines: list[DocLine],
    tolerance: Tolerance,
) -> MatchOutcome:
    """对供应商对账行做三层匹配；同一我方行只被消费一次。"""
    outcome = MatchOutcome()
    used_our: set[int] = set()
    matched_supplier: set[int] = set()

    # ① 单据号直配（同供应商 + 同单据号；行号一致优先）
    our_by_doc: dict[tuple[str, str], list[int]] = {}
    for index, line in enumerate(our_lines):
        our_by_doc.setdefault((line.supplier_code, line.doc_no), []).append(index)

    for sup_index, supplier in enumerate(supplier_lines):
        free = [
            index
            for index in our_by_doc.get((supplier.supplier_code, supplier.doc_no), [])
            if index not in used_our
        ]
        if not free:
            continue
        same_line = [index for index in free if our_lines[index].line_no == supplier.line_no]
        picked = same_line[0] if same_line else free[0]
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

    # ② / ③ 模糊配 + 候选生成
    for sup_index, supplier in enumerate(supplier_lines):
        if sup_index in matched_supplier:
            continue
        scored: list[tuple[Decimal, int]] = []
        for index, our in enumerate(our_lines):
            if index in used_our:
                continue
            score = score_pair(our, supplier)
            if score is None:
                continue
            scored.append((score, index))
        if not scored:
            outcome.unmatched_supplier.append(supplier)
            continue

        scored.sort(key=lambda item: (-item[0], item[1]))
        best_score, best_index = scored[0]

        if best_score < MIN_CONFIDENCE:
            # 置信度不足：只给候选，绝不自动通过
            outcome.candidate_only.append(
                (supplier, _top_candidates(scored, our_lines))
            )
            _consume_plausible(scored, used_our)
            continue

        if (
            len(scored) > 1
            and (best_score - scored[1][0]) < AMBIGUITY_GAP
        ):
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
