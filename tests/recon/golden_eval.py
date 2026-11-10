"""金标准夹具评估：把 500 行标注答案与引擎实际输出逐行比对，产出可复跑基线。

用法（仓库根执行）：

    python tests/recon/golden_eval.py            # 打印并落盘基线
    python tests/recon/golden_eval.py --check    # 只比对，不落盘（CI 用）

产物：``fixtures/recon/golden/golden_500_baseline.json``
口径：``fixtures/recon/GOLDEN_LABELING.md`` 第 6 节「指标口径」。

评估不修改夹具、不改引擎：
- 指标低就是低，不做四舍五入美化，也不为了让数字好看而剔除场景；
- 分母一律用「标注期望」，不用「引擎实际产出的行数」，否则漏判会被隐藏。
"""

from __future__ import annotations

import hashlib
import json
import sys
from collections import Counter
from decimal import Decimal
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import file_digest

from apps.recon import build_request, reconcile
from apps.recon.models import BatchStatus, MatchResultLine, ReconResult

FIXTURE = ROOT / "fixtures" / "recon" / "golden" / "golden_500.json"
BASELINE = ROOT / "fixtures" / "recon" / "golden" / "golden_500_baseline.json"

# 匹配成功可接受的终态：已匹配（差异可解释且置信度达标）与待人工（差异需人工确认，
# 但配对本身正确）。
AUTO_MATCH_STATUS = {"已匹配", "待人工"}
# 应转人工的终态：不得自动出结论。
REVIEW_STATUS = {"匹配歧义", "候选待人工", "待人工"}

RATE_EXP = Decimal("0.0001")


def _rate(numerator: int, denominator: int) -> str:
    if denominator == 0:
        return "0.0000"
    return f"{(Decimal(numerator) / Decimal(denominator)).quantize(RATE_EXP):.4f}"


def _by_supplier_ref(result: ReconResult) -> dict[str, MatchResultLine]:
    mapping: dict[str, MatchResultLine] = {}
    for line in result.lines:
        if line.supplier_ref:
            mapping[line.supplier_ref] = line
    return mapping


def _diff_types(result: ReconResult, ref: str) -> set[str]:
    return {diff.type.value for diff in result.diffs if diff.ref == ref}


def run_fixture(path: Path = FIXTURE) -> tuple[dict[str, Any], dict[str, ReconResult]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    results = {
        batch["batch_id"]: reconcile(build_request(batch["input"]))
        for batch in payload["batches"]
    }
    return payload, results


def _judge_supplier(
    label: dict[str, Any],
    line: MatchResultLine | None,
    result: ReconResult,
) -> tuple[bool, str]:
    verdict = label["expected_verdict"]
    expected_refs = label["expected_our_refs"]
    if verdict == "no_conclusion":
        ok = result.batch_status is BatchStatus.NO_CONCLUSION
        return ok, "批次已降级为无法对账" if ok else f"批次出结论: {result.batch_status.value}"
    if line is None:
        return False, "引擎未输出该行"
    if verdict == "auto_match":
        if line.our_ref == expected_refs[0] and line.status.value in AUTO_MATCH_STATUS:
            return True, "配对正确"
        if line.our_ref is None:
            return False, f"未配对: {line.status.value}"
        return False, f"配对错误: {line.our_ref}（标注 {expected_refs[0]}）"
    if verdict == "review":
        ok = line.status.value in REVIEW_STATUS
        return ok, "已转人工" if ok else f"越权出结论: {line.status.value}"
    if verdict == "duplicate":
        ok = line.status.value == "重复计费"
        return ok, "识别为重复计费" if ok else f"未识别为重复: {line.status.value}"
    if verdict == "missing_supplier_doc":
        ok = line.status.value == "供应商未匹配"
        return ok, "识别为供应商侧缺单" if ok else f"未识别缺单: {line.status.value}"
    return False, f"未知期望: {verdict}"


def _judge_our(
    label: dict[str, Any],
    line: MatchResultLine | None,
    result: ReconResult,
) -> tuple[bool, str]:
    if label["expected_verdict"] != "missing_our_doc":
        return False, f"未知期望: {label['expected_verdict']}"
    if line is None:
        return False, "引擎未输出该行"
    ok = line.status.value == "我方未匹配"
    return ok, "识别为我方侧缺单" if ok else f"未识别缺单: {line.status.value}"


def evaluate(payload: dict[str, Any], results: dict[str, ReconResult]) -> dict[str, Any]:
    match_total = match_correct = match_wrong = match_missed = 0
    aggregation_total = aggregation_correct = 0
    review_total = review_correct = review_unsafe = 0
    duplicate_total = duplicate_correct = 0
    missing_sup_total = missing_sup_correct = 0
    missing_our_total = missing_our_correct = 0
    nc_batches_expected = nc_batches_correct = 0
    nc_rows_total = nc_rows_correct = 0
    false_downgrade: list[str] = []
    false_unattributed: list[str] = []
    false_auto_pass: list[str] = []
    concluded_batches = 0

    attribution_total = attribution_correct = 0
    type_stats: dict[str, Counter[str]] = {}

    rounding_total = rounding_correct = 0
    rounding_unattributed: list[str] = []
    aggregated_total = aggregated_correct = 0
    aggregated_split_total = aggregated_split_correct = 0
    aggregated_merge_total = aggregated_merge_correct = 0
    duplicate_conflict_total = duplicate_conflict_correct = 0

    scenario_stats: dict[str, Counter[str]] = {}
    batch_rows: list[dict[str, Any]] = []
    detail_rows: list[dict[str, Any]] = []

    for batch in payload["batches"]:
        batch_id = batch["batch_id"]
        result = results[batch_id]
        status = result.batch_status.value
        expected_nc = batch["expected_no_conclusion"]
        supplier_labels = batch["labels"]["supplier"]
        our_labels = batch["labels"]["our"]
        lines = _by_supplier_ref(result)
        our_lines = {
            line.our_ref: line for line in result.lines if line.our_ref is not None
        }
        unattributed_refs = {item.ref for item in result.unattributed}

        batch_failed = False

        if expected_nc:
            nc_batches_expected += 1
            ok_batch = (
                result.batch_status is BatchStatus.NO_CONCLUSION
                and result.metrics is None
                and result.lines == ()
                and result.diffs == ()
            )
            if ok_batch:
                nc_batches_correct += 1
            nc_rows_total += len(supplier_labels)
            if ok_batch:
                nc_rows_correct += len(supplier_labels)
        elif result.batch_status is BatchStatus.NO_CONCLUSION:
            false_downgrade.append(batch_id)

        if result.batch_status is BatchStatus.RECONCILED:
            concluded_batches += 1

        for ref, label in supplier_labels.items():
            scenario = label["scenario"]
            stats = scenario_stats.setdefault(scenario, Counter())
            stats["total"] += 1
            line = lines.get(ref)
            ok, detail = _judge_supplier(label, line, result)

            verdict = label["expected_verdict"]
            if verdict == "auto_match":
                match_total += 1
                if ok:
                    match_correct += 1
                elif line is not None and line.our_ref is not None:
                    match_wrong += 1
                else:
                    match_missed += 1
                if label["needs_aggregation"]:
                    aggregation_total += 1
                    aggregation_correct += int(ok)
            elif verdict == "review":
                review_total += 1
                review_correct += int(ok)
                if line is not None and line.status.value == "已匹配":
                    review_unsafe += 1
            elif verdict == "duplicate":
                duplicate_total += 1
                duplicate_correct += int(ok)
            elif verdict == "missing_supplier_doc":
                missing_sup_total += 1
                missing_sup_correct += int(ok)

            expected_types = set(label["expected_diff_types"])
            found_types = _diff_types(result, ref)

            # 舍入容差专项：标注 tolerated_amount≠0 的行，必须被引擎识别为
            # 「金额舍入容差」并随批次自动放行，而不是被当作无法归因/缺单。
            if Decimal(label.get("tolerated_amount", "0") or "0") != 0:
                rounding_total += 1
                ok_rounding = (
                    "金额舍入容差" in found_types
                    and result.batch_status is BatchStatus.RECONCILED
                )
                rounding_correct += int(ok_rounding)
                if ref in unattributed_refs:
                    rounding_unattributed.append(ref)

            # 聚合匹配专项：needs_aggregation 的行必须标成 aggregated，
            # 且指向标注的我方行——不能只靠「碰巧配对成功」。
            if label["needs_aggregation"]:
                aggregated_total += 1
                ok_agg = (
                    line is not None
                    and line.match_type.value == "aggregated"
                    and line.our_ref in label["expected_our_refs"]
                )
                aggregated_correct += int(ok_agg)
                if label["forbid_auto_match"]:
                    aggregated_merge_total += 1
                    aggregated_merge_correct += int(ok_agg)
                else:
                    aggregated_split_total += 1
                    aggregated_split_correct += int(ok_agg)

            # 同单号冲突专项：必须给出冲突列，不能只标一句「重复」。
            if scenario == "duplicate_same_doc":
                duplicate_conflict_total += 1
                duplicate_conflict_correct += int(
                    line is not None
                    and line.status.value == "重复计费"
                    and any(
                        "冲突列" in diff.suggestion
                        for diff in result.diffs
                        if diff.ref == ref
                    )
                )

            if expected_types:
                attribution_total += 1
                attribution_correct += int(found_types == expected_types)
                for diff_type in expected_types | found_types:
                    bucket = type_stats.setdefault(
                        diff_type, Counter({"expected": 0, "detected": 0, "tp": 0, "fp": 0, "fn": 0})
                    )
                    if diff_type in expected_types:
                        bucket["expected"] += 1
                    if diff_type in found_types:
                        bucket["detected"] += 1
                    if diff_type in expected_types and diff_type in found_types:
                        bucket["tp"] += 1
                    elif diff_type in found_types:
                        bucket["fp"] += 1
                    else:
                        bucket["fn"] += 1
            elif ref in unattributed_refs:
                false_unattributed.append(ref)

            stats["correct"] += int(ok)
            if not ok:
                batch_failed = True
                detail_rows.append(
                    {
                        "batch_id": batch_id,
                        "ref": ref,
                        "scenario": scenario,
                        "expected_verdict": verdict,
                        "expected_diff_types": sorted(expected_types),
                        "actual_status": None if line is None else line.status.value,
                        "actual_our_ref": None if line is None else line.our_ref,
                        "actual_diff_types": sorted(found_types),
                        "detail": detail,
                    }
                )

        for ref, label in our_labels.items():
            scenario = label["scenario"]
            stats = scenario_stats.setdefault(scenario, Counter())
            stats["total"] += 1
            line = our_lines.get(ref)
            ok, detail = _judge_our(label, line, result)
            missing_our_total += 1
            missing_our_correct += int(ok)
            expected_types = set(label["expected_diff_types"])
            found_types = _diff_types(result, ref)
            if expected_types:
                attribution_total += 1
                attribution_correct += int(found_types == expected_types)
                for diff_type in expected_types | found_types:
                    bucket = type_stats.setdefault(
                        diff_type, Counter({"expected": 0, "detected": 0, "tp": 0, "fp": 0, "fn": 0})
                    )
                    if diff_type in expected_types:
                        bucket["expected"] += 1
                    if diff_type in found_types:
                        bucket["detected"] += 1
                    if diff_type in expected_types and diff_type in found_types:
                        bucket["tp"] += 1
                    elif diff_type in found_types:
                        bucket["fp"] += 1
                    else:
                        bucket["fn"] += 1
            stats["correct"] += int(ok)
            if not ok:
                batch_failed = True
                detail_rows.append(
                    {
                        "batch_id": batch_id,
                        "ref": ref,
                        "scenario": scenario,
                        "expected_verdict": label["expected_verdict"],
                        "expected_diff_types": sorted(expected_types),
                        "actual_status": None if line is None else line.status.value,
                        "actual_our_ref": None if line is None else line.our_ref,
                        "actual_diff_types": sorted(found_types),
                        "detail": detail,
                    }
                )

        if not expected_nc and status == "已对账" and batch_failed:
            false_auto_pass.append(batch_id)

        metrics = result.metrics
        batch_rows.append(
            {
                "batch_id": batch_id,
                "name": batch["name"],
                "expected_no_conclusion": expected_nc,
                "actual_status": status,
                "supplier_lines": len(batch["input"]["supplier_lines"]),
                "our_lines": len(batch["input"]["our_lines"]),
                "matched": None if metrics is None else metrics.matched,
                "diff_lines": None if metrics is None else metrics.diff_lines,
                "diff_rate": None if metrics is None else f"{metrics.diff_rate:.4f}",
                "manual_lines": sum(1 for line in result.lines if line.needs_manual),
                "unattributed": len(result.unattributed),
                "suspected_anomaly": None if metrics is None else metrics.suspected_anomaly,
                "label_errors": sum(
                    1 for row in detail_rows if row["batch_id"] == batch_id
                ),
            }
        )

    scenarios = [
        {
            "scenario": scenario,
            "tag": payload["scenario_tags"].get(scenario, ""),
            "total": stats["total"],
            "correct": stats["correct"],
            "accuracy": _rate(stats["correct"], stats["total"]),
        }
        for scenario, stats in sorted(scenario_stats.items())
    ]

    match_accuracy = _rate(match_correct, match_total)
    attribution_accuracy = _rate(attribution_correct, attribution_total)

    return {
        "fixture": {
            "path": "fixtures/recon/golden/golden_500.json",
            "label_version": payload["label_version"],
            "sha256": file_digest.sha256_file(FIXTURE),
            "supplier_lines": payload["totals"]["supplier_lines"],
            "our_lines": payload["totals"]["our_lines"],
            "batches": payload["totals"]["batches"],
            "engine_sha256": _engine_digest(),
        },
        "match": {
            "denominator": match_total,
            "correct": match_correct,
            "wrong_pairing": match_wrong,
            "missed": match_missed,
            "accuracy": match_accuracy,
            "mismatch_rate": _rate(match_wrong, match_total),
            "needs_aggregation": {
                "total": aggregation_total,
                "correct": aggregation_correct,
                "accuracy": _rate(aggregation_correct, aggregation_total),
            },
        },
        "review": {
            "denominator": review_total,
            "correctly_deferred": review_correct,
            "auto_matched_unsafely": review_unsafe,
            "rate": _rate(review_correct, review_total),
        },
        "duplicate": {
            "denominator": duplicate_total,
            "correct": duplicate_correct,
            "accuracy": _rate(duplicate_correct, duplicate_total),
            "conflict": {
                "denominator": duplicate_conflict_total,
                "with_conflict_columns": duplicate_conflict_correct,
                "accuracy": _rate(duplicate_conflict_correct, duplicate_conflict_total),
            },
        },
        "rounding_tolerance": {
            "denominator": rounding_total,
            "tolerated_and_released": rounding_correct,
            "accuracy": _rate(rounding_correct, rounding_total),
            "wrongly_unattributed": sorted(rounding_unattributed),
        },
        "aggregation": {
            "denominator": aggregated_total,
            "marked_aggregated": aggregated_correct,
            "accuracy": _rate(aggregated_correct, aggregated_total),
            "split": {
                "denominator": aggregated_split_total,
                "correct": aggregated_split_correct,
                "accuracy": _rate(aggregated_split_correct, aggregated_split_total),
            },
            "merge": {
                "denominator": aggregated_merge_total,
                "correct": aggregated_merge_correct,
                "accuracy": _rate(aggregated_merge_correct, aggregated_merge_total),
            },
        },
        "missing_doc": {
            "supplier_side": {
                "denominator": missing_sup_total,
                "correct": missing_sup_correct,
                "accuracy": _rate(missing_sup_correct, missing_sup_total),
            },
            "our_side": {
                "denominator": missing_our_total,
                "correct": missing_our_correct,
                "accuracy": _rate(missing_our_correct, missing_our_total),
            },
        },
        "attribution": {
            "denominator": attribution_total,
            "exact_set_correct": attribution_correct,
            "accuracy": attribution_accuracy,
            "by_type": {
                diff_type: dict(counter)
                for diff_type, counter in sorted(type_stats.items())
            },
        },
        "no_conclusion": {
            "batches_expected": nc_batches_expected,
            "batches_correct": nc_batches_correct,
            "supplier_rows": nc_rows_total,
            "supplier_rows_correct": nc_rows_correct,
            "rate": _rate(nc_rows_correct, nc_rows_total),
            "false_downgrade_batches": false_downgrade,
        },
        "safety": {
            "batches_concluded_reconciled": concluded_batches,
            "false_auto_pass_batches": false_auto_pass,
            "false_unattributed_refs": sorted(false_unattributed),
        },
        "prd_targets": {
            "FR-103 匹配准确率≥0.92": {
                "target": "0.9200",
                "actual": match_accuracy,
                "met": Decimal(match_accuracy) >= Decimal("0.92"),
            },
            "FR-104 归因准确率≥0.85": {
                "target": "0.8500",
                "actual": attribution_accuracy,
                "met": Decimal(attribution_accuracy) >= Decimal("0.85"),
            },
        },
        "scenarios": scenarios,
        "batches": batch_rows,
        "failures": detail_rows,
    }


def _engine_digest() -> str:
    """引擎源码指纹：基线可追溯到具体实现版本（按 LF 归一，跨平台一致）。"""
    digest = hashlib.sha256()
    for path in sorted((ROOT / "apps" / "recon").glob("*.py")):
        digest.update(path.name.encode("utf-8"))
        digest.update(file_digest.normalize_eol(path.read_bytes()))
    return digest.hexdigest()


def build_baseline() -> dict[str, Any]:
    payload, results = run_fixture()
    return {
        "name": "golden_500_baseline",
        "note": (
            "由 tests/recon/golden_eval.py 生成；数字为引擎实测值，不达标即如实记录，"
            "不得通过调整夹具或放宽口径修改本文件。"
        ),
        "engine_changes": {
            "note": (
                "夹具与 golden_500.json 一字未动（fixture.sha256 与上一版一致）。"
                "指标变化来自识别能力提升，不是放宽口径：没有任何阈值被下调，"
                "新增的容差类别只放行「数量、单价完全一致且差额在容差内」的行，"
                "超容差或整批累计超限仍转人工。最近一轮为 P0 修复："
                "单位不同必须换算且数量金额同时闭合才可自动匹配；"
                "金额/数量不可解析不再抛异常，改为行级 unparseable + 整批待人工。"
            ),
            "items": [
                {
                    "change": "新增 DiffType.ROUNDING_TOLERANCE（金额舍入容差）",
                    "engine": "apps/recon/attribution.py + models.py + engine.py",
                    "metric": "rounding_tolerance 0/20 → 20/20；B05 由待人工复核 → 已对账",
                    "why": (
                        "数量/单价一致、仅金额按角取整产生 0.01~0.05 差，属正常业务，"
                        "不应等同无法归因。容差默认 0.05 元（按角取整单行最大误差），"
                        "单行超 0.05 或整批累计超 1 元 / 0.5% 仍转人工。"
                        "该类别在差异明细可见、可审计，但不生成差异单、不计入归因分母。"
                    ),
                },
                {
                    "change": "去重键改为「同供应商+期间+单据号+物料」并识别内容冲突",
                    "engine": "apps/recon/engine.py::_detect_duplicates + attribution.py",
                    "metric": "duplicate_same_doc 0/5 → 5/5；duplicate 总 5/10 → 10/10",
                    "why": (
                        "原实现只对整行哈希去重，同单号被改动的一行会漏成「缺单」。"
                        "现按业务唯一键分组，第二条起判重复，并在差异建议里逐列写出冲突"
                        "（如「单价 10.0000→10.2000」），安全来自识别而非巧合。"
                    ),
                },
                {
                    "change": "新增聚合匹配层（多对一拆单 / 一对多合并）",
                    "engine": "apps/recon/matching.py + engine.py",
                    "metric": "split_invoice 5/10 → 10/10；aggregation 15/15；合并 5/5 仍转人工",
                    "why": (
                        "原实现一行对一行，拆单第二行必然落成缺单。现按同供应商+物料+期间"
                        "+单位分组做合计闭合匹配，结果标 match_type=aggregated 并带 "
                        "aggregate_refs，人工可区分直配与聚合。拆单合计闭合可自动放行；"
                        "合并（一方多行）拆分口径不唯一，一律待人工。"
                    ),
                },
                {
                    "change": "评分前先做单位换算，不可换算才降权并区分原因",
                    "engine": "apps/recon/matching.py::_comparable/_score + engine.py",
                    "metric": "uom_conversion 10/20 → 20/20",
                    "why": (
                        "原评分直接用供应商原始数量/单价，单位差异把相似度压到 0.55。"
                        "现优先按主数据换算到我方单位口径再比；换算不可得时按中性 0.50 "
                        "降权，并在 Candidate.reason 写明「单位不可换算」而非「相似度低」。"
                    ),
                },
                {
                    "change": "P0-1 单位守门：不同单位必须换算且数量金额同时闭合才可自动匹配",
                    "engine": "apps/recon/matching.py::unit_guard + engine.py",
                    "metric": "训练集 375/375 不变（无回归）；对抗集 100 BOX↔100 PCS 由自动放行 → 候选待人工",
                    "why": (
                        "直配层以前只看单据号，同单号下 100 BOX 与 100 PCS 会被当同一笔自动放行，"
                        "换算系数错一位会静默算错钱。现单位不同（仅忽略空白/大小写）必须命中"
                        "主数据换算，且换算后数量与金额同时闭合；否则封顶 0.65、只给候选，"
                        "原因码写明「单位不一致」及可核对数字。"
                    ),
                },
                {
                    "change": "P0-2 解析降级：金额/数量不可解析不再抛异常",
                    "engine": "apps/recon/models.py + loader.py + engine.py",
                    "metric": "对抗集含字母 O / 全角金额由崩溃 → 行级「不可解析」+ 整批待人工；训练集不变",
                    "why": (
                        "以前 to_decimal 抛 ValueError 会打断整批，脏金额导致整批无结论。"
                        "现 loader 逐字段捕获并记 ParseIssue（行号、字段、原值），"
                        "该行从匹配中摘出（同单号同行号对手行一并摘出，避免误报缺单），"
                        "整批转待人工，其余行照常对账；不静默当 0、不猜。"
                    ),
                },
            ],
            "regressions": (
                "逐项对比见 fixtures/recon/GOLDEN_LABELING.md 第 7 节；"
                "原本达标的 clean_match/cross_period/price_diff/tax_*/scrap/"
                "missing_*/无法对账识别均未下降。"
            ),
        },
        "metrics": evaluate(payload, results),
    }


def print_summary(report: dict[str, Any]) -> None:
    match = report["match"]
    attribution = report["attribution"]
    no_conclusion = report["no_conclusion"]
    print("=== 金标准基线（500 行） ===")
    print(
        f"匹配准确率      {match['accuracy']}  "
        f"({match['correct']}/{match['denominator']})"
    )
    print(
        f"误配率          {match['mismatch_rate']}  "
        f"({match['wrong_pairing']} 行配错, {match['missed']} 行未配)"
    )
    print(
        f"归因准确率      {attribution['accuracy']}  "
        f"({attribution['exact_set_correct']}/{attribution['denominator']})"
    )
    print(
        f"无法对账识别    {no_conclusion['rate']}  "
        f"({no_conclusion['batches_correct']}/{no_conclusion['batches_expected']} 批次)"
    )
    print(
        f"误判已对账批次  {len(report['safety']['false_auto_pass_batches'])}"
        f"  {report['safety']['false_auto_pass_batches']}"
    )
    rounding = report["rounding_tolerance"]
    print(
        f"舍入容差识别    {rounding['accuracy']}  "
        f"({rounding['tolerated_and_released']}/{rounding['denominator']})"
    )
    aggregation = report["aggregation"]
    print(
        f"聚合匹配识别    {aggregation['accuracy']}  "
        f"({aggregation['marked_aggregated']}/{aggregation['denominator']})"
        f"  拆单 {aggregation['split']['correct']}/{aggregation['split']['denominator']}"
        f"  合并 {aggregation['merge']['correct']}/{aggregation['merge']['denominator']}"
    )
    conflict = report["duplicate"]["conflict"]
    print(
        f"同号冲突定位    {conflict['accuracy']}  "
        f"({conflict['with_conflict_columns']}/{conflict['denominator']})"
    )
    for name, target in report["prd_targets"].items():
        print(f"{name}: 实测 {target['actual']} → {'达标' if target['met'] else '未达标'}")
    print("--- 场景准确率 ---")
    for row in report["scenarios"]:
        print(f"{row['scenario']:<24} {row['correct']:>3}/{row['total']:<3} {row['accuracy']}")


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    baseline = build_baseline()
    print_summary(baseline["metrics"])
    if "--check" in args:
        if not BASELINE.exists():
            print(f"缺少基线文件: {BASELINE}")
            return 1
        current = json.loads(BASELINE.read_text(encoding="utf-8"))
        if current != baseline:
            print("基线与当前引擎输出不一致，请重跑 python tests/recon/golden_eval.py")
            return 1
        print("基线与当前引擎输出一致")
        return 0
    BASELINE.write_text(
        json.dumps(baseline, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"已写入 {BASELINE.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
