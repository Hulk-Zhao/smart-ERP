"""留出集 / 对抗集评估：训练集、留出集、对抗集三组数字。

纪律：
- 留出集只在引擎冻结后跑一次；本脚本不参与调参，也不因结果修改引擎；
- 对抗集只报指标，不调参；
- 输出 golden_holdout_baseline.json，训练集数字直接引用 golden_500_baseline.json。

用法：
    python tests/recon/golden_holdout_eval.py
    python tests/recon/golden_holdout_eval.py --check
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import file_digest
from golden_eval import _by_supplier_ref, _judge_supplier, evaluate

from apps.recon import build_request, reconcile
from apps.recon.models import BatchStatus

ROOT = Path(__file__).resolve().parents[2]
GOLDEN_DIR = ROOT / "fixtures" / "recon" / "golden"
TRAIN_FIXTURE = GOLDEN_DIR / "golden_500.json"
TRAIN_BASELINE = GOLDEN_DIR / "golden_500_baseline.json"
HOLDOUT = GOLDEN_DIR / "golden_holdout.json"
ADVERSARIAL = GOLDEN_DIR / "golden_adversarial.json"
REPORT = GOLDEN_DIR / "golden_holdout_baseline.json"

REPORT_NOTE = (
    "三组数字：训练集 375 行、留出集第 1 轮、对抗集。"
    "留出集是冻结夹具的子集，不是统计独立的样本，只能证明规则没有按单据过拟合；"
    "对外承诺自动化率应以对抗集与真实数据为准，不得引用训练集满分。"
    "留出集指标取自第一次评估；之后的复跑仅作确定性校验，结果必须一致，未据此调整任何规则。"
    "夹具实际是 19 类场景（不是需求方口径中的 24 类），本文件按 19/19 报告覆盖。"
    "本轮（v1.2.0）为 P0 修复后的重跑：单位守门 + 解析降级，训练/留出集回归情况见 metrics。"
)

ENGINE_CHANGES = [
    {
        "id": "P0-1",
        "summary": (
            "单位守门：单位不同（非大小写/空白差异）必须显式换算且换算后数量与金额同时闭合，"
            "否则禁止自动匹配（直配层同样受限），只给候选转人工，原因码写明「单位不一致」。"
        ),
        "files": ["apps/recon/matching.py"],
    },
    {
        "id": "P0-2",
        "summary": (
            "解析降级：金额/数量不可解析不再向外抛异常，记录行号+字段+原值（parse_issues），"
            "该行状态为「不可解析」并从匹配中摘出（同单号同行号的对手行一并摘出），整批转待人工，"
            "其余行照常对账。"
        ),
        "files": ["apps/recon/models.py", "apps/recon/loader.py", "apps/recon/engine.py"],
    },
]


HOLDOUT_CAVEAT = (
    "按用户要求从冻结夹具按单据组切出，因此与训练集重叠；"
    "逐行规则确定，任何子集都会得到与训练集相同的判定。"
    "批次为单据组子集，批次级差异率会与训练集不同（如 B07 0.2000 vs 0.2300），"
    "因此留出集满分不等于训练集结论可外推。它不能替代真实新数据。"
)


def _run(payload: dict[str, Any]) -> dict[str, Any]:
    results = {}
    for batch in payload["batches"]:
        results[batch["batch_id"]] = reconcile(build_request(batch["input"]))
    return results


def _judge(label: dict[str, Any], line, result):
    """对抗集判定：在通用判定之上补「行级不可解析 + 整批待人工」这一期望。"""
    if label["expected_verdict"] == "unparseable":
        if line is None:
            return False, "引擎未输出该行"
        ok = (
            line.status.value == "不可解析"
            and result.batch_status is BatchStatus.NEEDS_REVIEW
        )
        if ok:
            return True, "行级降级为不可解析，整批待人工"
        return False, (
            f"行状态 {line.status.value}、批次 {result.batch_status.value}"
            "（期望不可解析 + 待人工复核）"
        )
    return _judge_supplier(label, line, result)


def evaluate_adversarial(payload: dict[str, Any]) -> dict[str, Any]:
    """对抗集逐批判定：引擎异常、错误结论、错误归因都算失败。"""
    cases: list[dict[str, Any]] = []
    rows = 0
    for batch in payload["batches"]:
        rows += len(batch["input"]["supplier_lines"])
        try:
            result = reconcile(build_request(batch["input"]))
        except Exception as exc:  # noqa: BLE001 - 对抗集就是要暴露解析崩溃
            cases.append(
                {
                    "case_id": batch["batch_id"],
                    "kind": batch["adversarial_kind"],
                    "passed": False,
                    "actual_status": "引擎异常",
                    "reason": f"{type(exc).__name__}: {exc}",
                }
            )
            continue

        problems: list[str] = []
        expect_nc = batch["expected_no_conclusion"]
        actual_nc = result.batch_status is BatchStatus.NO_CONCLUSION
        if expect_nc != actual_nc:
            problems.append(
                f"批次结论 {result.batch_status.value}，期望"
                f"{'无法对账' if expect_nc else '出结论'}"
            )
        lines = _by_supplier_ref(result)
        for ref, label in batch["labels"]["supplier"].items():
            ok, reason = _judge(label, lines.get(ref), result)
            expected_types = set(label["expected_diff_types"])
            if expected_types:
                found = {diff.type.value for diff in result.diffs if diff.ref == ref}
                if found != expected_types:
                    ok = False
                    reason = f"{reason}；差异类型 {sorted(found)}，期望 {sorted(expected_types)}"
            if not ok:
                problems.append(f"{ref}: {reason}")
        cases.append(
            {
                "case_id": batch["batch_id"],
                "kind": batch["adversarial_kind"],
                "passed": not problems,
                "actual_status": result.batch_status.value,
                "reason": "；".join(problems) if problems else "符合期望",
            }
        )
    passed = sum(1 for case in cases if case["passed"])
    return {
        "rows": rows,
        "cases": cases,
        "passed": passed,
        "total": len(cases),
        "accuracy": round(passed / len(cases), 4) if cases else 0.0,
    }


def build_report() -> dict[str, Any]:
    training = json.loads(TRAIN_BASELINE.read_text(encoding="utf-8"))["metrics"]

    holdout_payload = json.loads(HOLDOUT.read_text(encoding="utf-8"))
    holdout_metrics = evaluate(holdout_payload, _run(holdout_payload))

    adversarial_payload = json.loads(ADVERSARIAL.read_text(encoding="utf-8"))
    adversarial = evaluate_adversarial(adversarial_payload)

    return {
        "name": "golden_holdout_baseline",
        "note": REPORT_NOTE,
        "engine_changes": ENGINE_CHANGES,
        "training": {
            "file": "fixtures/recon/golden/golden_500.json",
            "sha256": file_digest.sha256_file(TRAIN_FIXTURE),
            "supplier_lines": 500,
            "match_denominator": 375,
            "engine_frozen": True,
            "metrics": training,
        },
        "holdout": {
            "file": "fixtures/recon/golden/golden_holdout.json",
            "sha256": file_digest.sha256_file(HOLDOUT),
            "holdout_version": holdout_payload["holdout_version"],
            "holdout_round": holdout_payload["holdout_round"],
            "carve_version": holdout_payload["carve_version"],
            "carve_history": holdout_payload["carve_history"],
            "evaluated_times": 1,
            "statistically_independent": False,
            "caveat": HOLDOUT_CAVEAT,
            "totals": holdout_payload["totals"],
            "scenario_coverage": len(holdout_payload["scenario_coverage"]),
            "metrics": holdout_metrics,
        },
        "adversarial": {
            "file": "fixtures/recon/golden/golden_adversarial.json",
            "sha256": file_digest.sha256_file(ADVERSARIAL),
            "adversarial_version": adversarial_payload["adversarial_version"],
            "label_revision": adversarial_payload["label_revision"],
            "tuned_on": False,
            "result": adversarial,
        },
    }


def print_report(report: dict[str, Any]) -> None:
    training = report["training"]["metrics"]
    holdout = report["holdout"]["metrics"]
    adversarial = report["adversarial"]["result"]
    print("样本                 量           匹配率    归因率    无法对账率  误配  误判已对账")
    print(
        f"训练集               {report['training']['match_denominator']:<11}"
        f" {training['match']['accuracy']:<9} {training['attribution']['accuracy']:<9}"
        f" {training['no_conclusion']['rate']:<11} {training['match']['wrong_pairing']:<4}"
        f" {len(training['safety']['false_auto_pass_batches'])}"
    )
    print(
        f"留出集(第{report['holdout']['holdout_round']}轮)       "
        f"{report['holdout']['totals']['supplier_lines']:<11}"
        f" {holdout['match']['accuracy']:<9} {holdout['attribution']['accuracy']:<9}"
        f" {holdout['no_conclusion']['rate']:<11} {holdout['match']['wrong_pairing']:<4}"
        f" {len(holdout['safety']['false_auto_pass_batches'])}"
    )
    print(
        f"对抗集               {adversarial['rows']:<11}"
        f" 用例通过 {adversarial['passed']}/{adversarial['total']}"
        f"（{adversarial['accuracy']}）"
    )
    print("\n对抗集逐项：")
    for case in adversarial["cases"]:
        flag = "PASS" if case["passed"] else "FAIL"
        print(f"  {flag} {case['case_id']:<8} {case['kind']:<24} {case['reason']}")


def main() -> None:
    parser = argparse.ArgumentParser(description="留出集与对抗集评估")
    parser.add_argument("--check", action="store_true", help="只校验基线是否可复现")
    args = parser.parse_args()

    report = build_report()
    if args.check:
        if not REPORT.exists():
            raise SystemExit(f"基线不存在：{REPORT}，请先运行不带 --check 的命令")
        current = json.loads(REPORT.read_text(encoding="utf-8"))
        if current != report:
            raise SystemExit("基线不一致：引擎或样本已变化，请重新生成并评审")
        print("留出集/对抗集基线与当前引擎一致")
        return

    REPORT.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print_report(report)


if __name__ == "__main__":
    main()
