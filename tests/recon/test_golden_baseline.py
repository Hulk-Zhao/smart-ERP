"""金标准夹具与基线的回归：结构完整性 + 基线可复现。

这里只锁「夹具与基线是否自洽、是否可复现」，不把指标当门槛——指标高低
记录在 ``golden_500_baseline.json``，跑不过就如实报低，不在测试里改写期望。
"""

from __future__ import annotations

import json
from pathlib import Path

import golden_eval
import golden_gen

from apps.recon import build_request, reconcile
from apps.recon.models import BatchStatus

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "fixtures" / "recon" / "golden" / "golden_500.json"
BASELINE = ROOT / "fixtures" / "recon" / "golden" / "golden_500_baseline.json"

REQUIRED_SCENARIOS = {
    "clean_match",
    "price_diff",
    "tax_rate_diff",
    "tax_basis_diff",
    "scrap_not_deducted",
    "partial_receipt",
    "rounding_diff",
    "uom_conversion",
    "uom_conversion_missing",
    "duplicate_exact",
    "duplicate_same_doc",
    "split_invoice",
    "merged_billing",
    "supplier_only",
    "our_only",
    "cross_period",
    "missing_item_code",
    "missing_line_data",
    "unbound_supplier",
}


def _fixture() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _refs(rows: list[dict]) -> set[str]:
    return {f"{row['system']}:{row['doc_no']}#{row['line_no']}" for row in rows}


def test_fixture_is_generated_by_script():
    assert _fixture() == golden_gen.build_fixture()


def test_totals_are_500_lines_over_14_batches():
    payload = _fixture()
    assert payload["totals"]["supplier_lines"] == 500
    assert payload["totals"]["batches"] == 14
    assert payload["totals"]["no_conclusion_supplier_lines"] == 100
    assert payload["totals"]["labeled_supplier_lines"] == 500


def test_every_line_has_a_label_and_every_label_points_to_a_real_line():
    for batch in _fixture()["batches"]:
        supplier_refs = _refs(batch["input"]["supplier_lines"])
        our_refs = _refs(batch["input"]["our_lines"])
        labels = batch["labels"]
        assert set(labels["supplier"]) == supplier_refs, batch["batch_id"]
        assert set(labels["our"]) <= our_refs, batch["batch_id"]
        for label in list(labels["supplier"].values()) + list(labels["our"].values()):
            assert label["scenario"] in golden_gen.SCENARIO_TAGS
            assert label["expected_verdict"] in {
                "auto_match",
                "review",
                "duplicate",
                "missing_supplier_doc",
                "missing_our_doc",
                "no_conclusion",
            }
            assert set(label["expected_our_refs"]) <= our_refs
            assert label["note"], "标注必须写明理由"


def test_required_dirty_data_forms_are_covered():
    payload = _fixture()
    covered = {row["scenario"] for row in payload["scenario_coverage"]}
    # 缺换算批次复用 uom_conversion 场景键，这里单独校验其存在性
    batch_names = {batch["name"] for batch in payload["batches"]}
    assert "missing_uom_conversion" in batch_names
    assert REQUIRED_SCENARIOS - {"uom_conversion_missing"} <= covered


def test_no_conclusion_batches_are_refused_without_output():
    for batch in _fixture()["batches"]:
        if not batch["expected_no_conclusion"]:
            continue
        result = reconcile(build_request(batch["input"]))
        assert result.batch_status is BatchStatus.NO_CONCLUSION, batch["batch_id"]
        assert result.metrics is None
        assert result.lines == ()
        assert result.diffs == ()
        assert result.missing_items, "降级必须给出缺失清单"


def test_reconcilable_batches_are_not_downgraded():
    for batch in _fixture()["batches"]:
        if batch["expected_no_conclusion"]:
            continue
        result = reconcile(build_request(batch["input"]))
        assert result.batch_status is not BatchStatus.NO_CONCLUSION, batch["batch_id"]


def test_baseline_is_reproducible():
    committed = json.loads(BASELINE.read_text(encoding="utf-8"))
    assert committed == golden_eval.build_baseline()


def test_report_can_be_printed():
    report = golden_eval.build_baseline()["metrics"]
    golden_eval.print_summary(report)
    assert report["match"]["denominator"] == 375
    assert report["attribution"]["denominator"] == 99
    assert report["no_conclusion"]["batches_expected"] == 4
