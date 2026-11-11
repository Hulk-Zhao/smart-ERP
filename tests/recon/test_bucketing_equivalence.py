"""分桶匹配差分测试（旧实现 vs 新实现）。

背景：``apps/recon/matching.py`` 把「逐行全量扫描」改成按键分桶，只改变了
**候选集合怎么取**。golden 500 / holdout 是固定数据，覆盖不到桶化最容易塌陷的
边界组合（单位为空、规范化后名称撞名、编码缺失只剩名称桶、数量为空的合并候选、
跨单据号重复行）。这里用随机数据做逐行差分。

约定：
- 旧实现只保留在**本测试文件**内（仅复制被改动的 ``match_lines`` /
  ``_match_merged`` / ``_match_aggregated`` 三个函数）；未改动的纯函数
  （``_score`` / ``unit_guard`` / ``_sum_qty`` 等）仍从生产模块导入，
  避免在 ``apps/`` 里出现第二套生产路径。
- 新实现照常走 ``apps/recon/matching.py``。
- 比对走完整 ``reconcile()``：行状态、匹配类型、匹配到的对方行、差异类型与金额。
  就绪检查在两侧同时被摘掉——它决定「整批是否出结论」，与桶化无关，保留会让
  含空值的随机数据统一返回「无法对账」，差分就变成空比对。
- 任何不一致都会打印可复现的种子，并先把该组数据按行贪心最小化。
"""

from __future__ import annotations

import itertools
import json
import random
from dataclasses import replace
from decimal import Decimal

import pytest

from apps.recon import engine as recon_engine
from apps.recon.matching import (
    AGGREGATED_CONFIDENCE,
    AMBIGUITY_GAP,
    MERGE_MAX_CANDIDATES,
    MERGE_MAX_SIZE,
    MERGED_CONFIDENCE,
    MIN_CONFIDENCE,
    Converter,
    MatchOutcome,
    _close,
    _consume_plausible,
    _score,
    _sum_amount,
    _sum_qty,
    _top_candidates,
    match_lines,
    unit_guard,
)
from apps.recon.models import (
    DocLine,
    MasterData,
    MatchPair,
    MatchType,
    Money,
    ReconRequest,
    Scope,
    SupplierMaster,
    Tolerance,
    UnitConversion,
)

# ---------------------------------------------------------------- 旧实现（参照）

# 以下三个函数是 995c344（分桶前）的原文，仅改了函数名。
# 它们依赖的纯函数未在本次改动中变化，直接从生产模块导入。


def _old_match_merged(
    our_lines: list[DocLine],
    supplier_lines: list[DocLine],
    tolerance: Tolerance,
    used_our: set[int],
    matched_supplier: set[int],
    outcome: MatchOutcome,
) -> None:
    """一对多：一张供应商行覆盖多行我方行（合并账单），必须人工确认。"""
    for sup_index, supplier in enumerate(supplier_lines):
        if sup_index in matched_supplier or not supplier.item_code:
            continue
        if supplier.qty is None:
            continue
        candidates = [
            index
            for index, our in enumerate(our_lines)
            if index not in used_our
            and our.supplier_code == supplier.supplier_code
            and our.item_code == supplier.item_code
            and our.unit == supplier.unit
            and our.qty is not None
        ]
        if len(candidates) < 2 or len(candidates) > MERGE_MAX_CANDIDATES:
            continue
        # 单行已经能对上时不做合并，避免把正常单行匹配拆成聚合
        if any(
            abs(our_lines[index].qty - supplier.qty) <= tolerance.qty
            for index in candidates
        ):
            continue
        supplier_amount = _old_amount_of(supplier)
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


def _old_amount_of(line: DocLine) -> Decimal | None:
    """旧实现里 ``_amount_of`` 未改动，这里只是本地别名，避免与生产模块混淆。"""
    if line.amount is not None:
        return line.amount.amount
    if line.qty is not None and line.unit_price is not None:
        return line.qty * line.unit_price
    return None


def _old_match_aggregated(
    our_lines: list[DocLine],
    supplier_lines: list[DocLine],
    tolerance: Tolerance,
    used_our: set[int],
    matched_supplier: set[int],
    outcome: MatchOutcome,
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
        for our_index, our in enumerate(our_lines):
            if our_index in used_our:
                continue
            if our.supplier_code != key[0] or our.item_code != key[1]:
                continue
            if our.qty is None or our.unit != key[3]:
                continue
            if not _close(total_qty, our.qty, tolerance.qty):
                continue
            if not _close(total_amount, _old_amount_of(our), tolerance.amount):
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


def reference_match_lines(
    our_lines: list[DocLine],
    supplier_lines: list[DocLine],
    tolerance: Tolerance,
    converters: Converter | None = None,
) -> MatchOutcome:
    """分桶前的旧实现：模糊配逐个全量扫描，合并/聚合也全量扫描。"""
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
        same_line = [
            index for index in free if our_lines[index].line_no == supplier.line_no
        ]
        ordered = same_line + [index for index in free if index not in same_line]
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

    # ② 聚合匹配（一行对多行 / 多行对一行）
    _old_match_merged(
        our_lines, supplier_lines, tolerance, used_our, matched_supplier, outcome
    )
    _old_match_aggregated(
        our_lines, supplier_lines, tolerance, used_our, matched_supplier, outcome
    )

    # ③ 模糊配 + 候选生成（旧实现：逐个全量扫描）
    for sup_index, supplier in enumerate(supplier_lines):
        if sup_index in matched_supplier:
            continue
        scored: list[tuple[Decimal, int, str]] = []
        for index, our in enumerate(our_lines):
            if index in used_our:
                continue
            score, reason = _score(our, supplier, converters, tolerance)
            if score is None:
                continue
            scored.append((score, index, reason))
        if not scored:
            outcome.unmatched_supplier.append(supplier)
            continue

        scored.sort(key=lambda item: (-item[0], item[1]))
        best_score, best_index, _best_reason = scored[0]

        if best_score < MIN_CONFIDENCE:
            outcome.candidate_only.append(
                (supplier, _top_candidates(scored, our_lines))
            )
            _consume_plausible(scored, used_our)
            continue

        if len(scored) > 1 and (best_score - scored[1][0]) < AMBIGUITY_GAP:
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


# ---------------------------------------------------------------- 变异实现（自检）

# 与生产实现完全相同，只把「合并/聚合」的桶键从 (供应商, 物料, 单位) 改成
# (供应商, 物料)——即漏掉 unit。用来证明这组差分测试真的能判出回归。


def _mutant_match_merged(
    our_lines: list[DocLine],
    supplier_lines: list[DocLine],
    tolerance: Tolerance,
    used_our: set[int],
    matched_supplier: set[int],
    outcome: MatchOutcome,
    our_by_sku_without_unit: dict[tuple[str, str], list[int]],
) -> None:
    for sup_index, supplier in enumerate(supplier_lines):
        if sup_index in matched_supplier or not supplier.item_code:
            continue
        if supplier.qty is None:
            continue
        candidates = [
            index
            for index in our_by_sku_without_unit.get(
                (supplier.supplier_code, supplier.item_code), ()
            )
            if index not in used_our and our_lines[index].qty is not None
        ]
        if len(candidates) < 2 or len(candidates) > MERGE_MAX_CANDIDATES:
            continue
        if any(
            abs(our_lines[index].qty - supplier.qty) <= tolerance.qty
            for index in candidates
        ):
            continue
        supplier_amount = _old_amount_of(supplier)
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


def _mutant_match_aggregated(
    our_lines: list[DocLine],
    supplier_lines: list[DocLine],
    tolerance: Tolerance,
    used_our: set[int],
    matched_supplier: set[int],
    outcome: MatchOutcome,
    our_by_sku_without_unit: dict[tuple[str, str], list[int]],
) -> None:
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
        for our_index in our_by_sku_without_unit.get((key[0], key[1]), ()):
            if our_index in used_our:
                continue
            our = our_lines[our_index]
            if our.qty is None:
                continue
            if not _close(total_qty, our.qty, tolerance.qty):
                continue
            if not _close(total_amount, _old_amount_of(our), tolerance.amount):
                continue
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


def mutant_match_lines(
    our_lines: list[DocLine],
    supplier_lines: list[DocLine],
    tolerance: Tolerance,
    converters: Converter | None = None,
) -> MatchOutcome:
    """漏掉 unit 的桶键：只用于证明差分测试能判出候选集合回归。"""
    outcome = MatchOutcome()
    used_our: set[int] = set()
    matched_supplier: set[int] = set()

    our_by_doc: dict[tuple[str, str], list[int]] = {}
    our_by_sku: dict[tuple[str, str], list[int]] = {}
    our_by_name: dict[tuple[str, str], list[int]] = {}
    our_by_sku_without_unit: dict[tuple[str, str], list[int]] = {}
    for index, line in enumerate(our_lines):
        our_by_doc.setdefault((line.supplier_code, line.doc_no), []).append(index)
        if line.item_code:
            our_by_sku.setdefault((line.supplier_code, line.item_code), []).append(
                index
            )
            our_by_sku_without_unit.setdefault(
                (line.supplier_code, line.item_code), []
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

    _mutant_match_merged(
        our_lines,
        supplier_lines,
        tolerance,
        used_our,
        matched_supplier,
        outcome,
        our_by_sku_without_unit,
    )
    _mutant_match_aggregated(
        our_lines,
        supplier_lines,
        tolerance,
        used_our,
        matched_supplier,
        outcome,
        our_by_sku_without_unit,
    )

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
            outcome.candidate_only.append(
                (supplier, _top_candidates(scored, our_lines))
            )
            _consume_plausible(scored, used_our)
            continue

        if len(scored) > 1 and (best_score - scored[1][0]) < AMBIGUITY_GAP:
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


def _normalize_name(value: str | None) -> str:
    """与生产实现同名的规范化，测试内保留一份避免依赖私有函数。"""
    if not value:
        return ""
    return "".join(value.split()).upper()


# ---------------------------------------------------------------- 随机数据与比对


SUPPLIERS = ("SUP-A", "SUP-B")
ITEM_CODES = (None, "IT-A", "IT-B", "IT-C")
ITEM_NAMES = (None, "螺栓 M8", " 螺栓M8 ", "NUT", "螺母")
UNITS = (None, "PCS", "pcs", "BOX", " ")
DOC_NOS = ("PO-1", "PO-2", "INV-1")
PERIODS = ("2026-10", None)
QTYS = (None, "10", "20", "40", "60", "100")
PRICES = (None, "5", "12.5")
SIZES = (10, 20, 30, 40, 60, 80)


def _random_line(rng: random.Random, index: int, system: str) -> DocLine:
    item_code = rng.choice(ITEM_CODES)
    qty = rng.choice(QTYS)
    unit_price = rng.choice(PRICES)
    amount = None
    if qty is not None and unit_price is not None and rng.random() < 0.8:
        amount = str(Decimal(qty) * Decimal(unit_price))
    return DocLine(
        system=system,
        doc_no=rng.choice(DOC_NOS),
        line_no=rng.choice((1, 2, 3)),
        supplier_code=rng.choice(SUPPLIERS),
        item_code=item_code,
        item_name=rng.choice(ITEM_NAMES),
        qty=None if qty is None else Decimal(qty),
        unit=rng.choice(UNITS),
        unit_price=None if unit_price is None else Decimal(unit_price),
        amount=None if amount is None else Money.of(amount),
        period=rng.choice(PERIODS),
    )


def _random_group(seed: int) -> tuple[list[DocLine], list[DocLine]]:
    """同一种子恒等生成一组我方行 + 供应商行，覆盖各类空值/重复组合。"""
    rng = random.Random(seed)
    our_lines = [_random_line(rng, index, "ERP") for index in range(rng.choice(SIZES))]
    supplier_lines = [
        _random_line(rng, index, "STATEMENT") for index in range(rng.choice(SIZES))
    ]
    # 跨单据号重复行：整行内容相同、单据号不同
    if our_lines and rng.random() < 0.4:
        source = rng.choice(our_lines)
        our_lines.append(replace(source, doc_no=rng.choice(DOC_NOS)))
    if supplier_lines and rng.random() < 0.4:
        source = rng.choice(supplier_lines)
        supplier_lines.append(replace(source, doc_no=rng.choice(DOC_NOS)))

    # 构造型场景：纯随机的数量很难凑出合计闭合，聚合/合并路径会一直是 0 覆盖。
    # 用独立供应商编码隔离，避免与随机行互相干扰。
    scenario = rng.choice(("merge", "split", "unit", None))
    if scenario == "merge":
        # 合并：一张供应商行覆盖我方两行；我方另有一行数量为空（不得进合并候选）
        our_lines += [
            _scenario_line(
                "ERP", "PO-M-1", "SUP-M", "IT-M", "40", "PCS", qty_none=False
            ),
            _scenario_line(
                "ERP", "PO-M-2", "SUP-M", "IT-M", "60", "PCS", qty_none=False
            ),
            _scenario_line(
                "ERP", "PO-M-3", "SUP-M", "IT-M", None, "PCS", qty_none=True
            ),
        ]
        supplier_lines.append(
            _scenario_line("STATEMENT", "INV-M", "SUP-M", "IT-M", "100", "PCS")
        )
    elif scenario == "split":
        # 拆单：两行供应商行合计等于我方一行
        our_lines.append(_scenario_line("ERP", "PO-S", "SUP-S", "IT-S", "100", "PCS"))
        supplier_lines += [
            _scenario_line("STATEMENT", "INV-S-1", "SUP-S", "IT-S", "40", "PCS"),
            _scenario_line("STATEMENT", "INV-S-2", "SUP-S", "IT-S", "60", "PCS"),
        ]
    elif scenario == "unit":
        # 单位桶边界：同物料同数量合计，但两行单位不同，不得进合并候选
        our_lines += [
            _scenario_line("ERP", "PO-U-1", "SUP-U", "IT-U", "40", "PCS"),
            _scenario_line("ERP", "PO-U-2", "SUP-U", "IT-U", "60", "BOX"),
        ]
        supplier_lines.append(
            _scenario_line("STATEMENT", "INV-U", "SUP-U", "IT-U", "100", "PCS")
        )
    return our_lines, supplier_lines


def _scenario_line(
    system: str,
    doc_no: str,
    supplier_code: str,
    item_code: str,
    qty: str | None,
    unit: str,
    *,
    qty_none: bool = False,
) -> DocLine:
    quantity = None if qty_none else Decimal(qty)
    return DocLine(
        system=system,
        doc_no=doc_no,
        line_no=1,
        supplier_code=supplier_code,
        item_code=item_code,
        qty=quantity,
        unit=unit,
        unit_price=Decimal(5),
        amount=Money.of(Decimal(5) * quantity) if quantity is not None else None,
        period="2026-10",
    )


def _master_data() -> MasterData:
    return MasterData(
        suppliers={
            code: SupplierMaster(code=code, name=f"{code} 示例") for code in SUPPLIERS
        },
        unit_conversions=(
            UnitConversion(
                item="IT-A", from_unit="BOX", to_unit="PCS", factor=Decimal(10)
            ),
            UnitConversion(
                item="IT-B", from_unit="BOX", to_unit="PCS", factor=Decimal(6)
            ),
        ),
    )


def _request(our_lines: list[DocLine], supplier_lines: list[DocLine]) -> ReconRequest:
    return ReconRequest(
        scope=Scope(
            org_id="ORG-TEST", period="2026-10", system_ids=("ERP", "STATEMENT")
        ),
        suppliers=SUPPLIERS,
        our_lines=tuple(our_lines),
        supplier_lines=tuple(supplier_lines),
        master_data=_master_data(),
        tolerance=Tolerance(),
    )


def _line_signature(line) -> tuple:
    return (
        line.status.value,
        line.match_type.value,
        f"{line.confidence:.2f}",
        str(line.our_ref),
        str(line.supplier_ref),
        "|".join(line.aggregate_refs),
        " ; ".join(
            f"{diff.type.value}:{diff.amount.amount}:{diff.owner.value}"
            for diff in line.diffs
        ),
    )


def _result_signature(result) -> tuple:
    return (
        result.batch_status.value,
        tuple(sorted(_line_signature(line) for line in result.lines)),
        tuple(
            sorted(
                f"{diff.type.value}:{diff.amount.amount}:{diff.owner.value}"
                for diff in result.diffs
            )
        ),
        None
        if result.metrics is None
        else json.dumps(result.metrics.to_dict(), sort_keys=True),
    )


def _run(request: ReconRequest, matcher) -> tuple:
    """在指定匹配实现下跑完整引擎；两侧同时摘掉整批就绪检查。"""
    original_match = recon_engine.match_lines
    original_ready = recon_engine.check_readiness
    recon_engine.match_lines = matcher
    recon_engine.check_readiness = lambda _request: ()
    try:
        return _result_signature(recon_engine.reconcile(request))
    finally:
        recon_engine.match_lines = original_match
        recon_engine.check_readiness = original_ready


def _signature(our_lines, supplier_lines, matcher=match_lines) -> tuple:
    return _run(_request(list(our_lines), list(supplier_lines)), matcher)


def _differs(our_lines, supplier_lines, matcher=reference_match_lines) -> bool:
    return _signature(our_lines, supplier_lines, matcher) != _signature(
        our_lines, supplier_lines, match_lines
    )


def _describe(line: DocLine) -> dict:
    return {
        "system": line.system,
        "doc_no": line.doc_no,
        "line_no": line.line_no,
        "supplier_code": line.supplier_code,
        "item_code": line.item_code,
        "item_name": line.item_name,
        "qty": None if line.qty is None else str(line.qty),
        "unit": line.unit,
        "unit_price": None if line.unit_price is None else str(line.unit_price),
        "amount": None if line.amount is None else str(line.amount.amount),
        "period": line.period,
    }


def _minimize(our_lines, supplier_lines):
    """按行贪心最小化：每去掉一行仍不一致就保留删除，直到不能再删。"""
    ours = list(our_lines)
    sups = list(supplier_lines)
    improved = True
    while improved:
        improved = False
        for index in range(len(ours)):
            trial = ours[:index] + ours[index + 1 :]
            if _differs(trial, sups):
                ours = trial
                improved = True
                break
        if improved:
            continue
        for index in range(len(sups)):
            trial = sups[:index] + sups[index + 1 :]
            if _differs(ours, trial):
                sups = trial
                improved = True
                break
    return ours, sups


def _failure_message(seed: int, our_lines, supplier_lines, old_sig, new_sig) -> str:
    payload = {
        "seed": seed,
        "our_lines": [_describe(line) for line in our_lines],
        "supplier_lines": [_describe(line) for line in supplier_lines],
    }
    return (
        f"分桶匹配与旧实现结果不一致（seed={seed}），已最小化到 "
        f"{len(our_lines)}+{len(supplier_lines)} 行：\n"
        f"{json.dumps(payload, ensure_ascii=False, indent=2)}\n"
        f"旧实现：{old_sig}\n"
        f"新实现：{new_sig}"
    )


@pytest.mark.parametrize("seed", range(200))
def test_bucketed_matching_equals_reference(seed: int) -> None:
    """200 组随机数据逐行比对：状态 / 匹配类型 / 对方行 / 差异类型与金额。"""
    our_lines, supplier_lines = _random_group(seed)
    old_sig = _signature(our_lines, supplier_lines, reference_match_lines)
    new_sig = _signature(our_lines, supplier_lines, match_lines)
    if old_sig == new_sig:
        return

    minimal_our, minimal_supplier = _minimize(our_lines, supplier_lines)
    minimal_old = _signature(minimal_our, minimal_supplier, reference_match_lines)
    minimal_new = _signature(minimal_our, minimal_supplier, match_lines)
    pytest.fail(
        _failure_message(seed, minimal_our, minimal_supplier, minimal_old, minimal_new)
    )


def test_differential_harness_detects_broken_bucket_key() -> None:
    """反例自检：桶键漏掉 unit 时差分必须变红，证明这组测试能判出回归。"""
    our_lines = [
        DocLine(
            system="ERP",
            doc_no="PO-1",
            line_no=1,
            supplier_code="SUP-A",
            item_code="IT-A",
            qty=Decimal(40),
            unit="PCS",
            unit_price=Decimal(5),
            amount=Money.of("200"),
            period="2026-10",
        ),
        DocLine(
            system="ERP",
            doc_no="PO-2",
            line_no=1,
            supplier_code="SUP-A",
            item_code="IT-A",
            qty=Decimal(60),
            unit="BOX",
            unit_price=Decimal(5),
            amount=Money.of("300"),
            period="2026-10",
        ),
    ]
    supplier_lines = [
        DocLine(
            system="STATEMENT",
            doc_no="INV-1",
            line_no=1,
            supplier_code="SUP-A",
            item_code="IT-A",
            qty=Decimal(100),
            unit="PCS",
            unit_price=Decimal(5),
            amount=Money.of("500"),
            period="2026-10",
        )
    ]

    # 旧实现与新实现在该数据上一致（差异只来自变异实现）
    assert _signature(our_lines, supplier_lines, reference_match_lines) == _signature(
        our_lines, supplier_lines, match_lines
    )
    # 桶键漏掉 unit 后，合并候选把 PCS 与 BOX 两行凑成 100，差分必须报出来
    assert _signature(our_lines, supplier_lines, mutant_match_lines) != _signature(
        our_lines, supplier_lines, match_lines
    )
    # 同一变异在随机语料上也能被判出
    detected = [
        seed
        for seed in range(50)
        if _differs(*_random_group(seed), matcher=mutant_match_lines)
    ]
    assert detected, "变异实现在 50 组随机数据上都未被判出，差分测试无效"
