"""留出集 / 对抗集回归：样本自洽、可复现、互不污染。

只锁结构与可复现性，不用指标当门槛：留出集与对抗集的真实数字记录在
``golden_holdout_baseline.json``，跑不过就如实报低。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import golden_holdout_eval
import golden_holdout_gen

from apps.recon import build_request, reconcile
from apps.recon.models import BatchStatus

ROOT = Path(__file__).resolve().parents[2]
GOLDEN_DIR = ROOT / "fixtures" / "recon" / "golden"
FIXTURE = GOLDEN_DIR / "golden_500.json"
HOLDOUT = GOLDEN_DIR / "golden_holdout.json"
ADVERSARIAL = GOLDEN_DIR / "golden_adversarial.json"
REPORT = GOLDEN_DIR / "golden_holdout_baseline.json"

MIN_HOLDOUT_ROWS = 80

REQUIRED_ADVERSARIAL_KINDS = {
    "same_doc_other_batch",
    "price_4dp",
    "uom_magnitude",
    "ocr_doc_no",
    "ocr_item_code",
    "ocr_amount",
    "fullwidth_amount",
    "fullwidth_qty",
    "supplier_name_space",
}


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _ref(row: dict) -> str:
    return f"{row['system']}:{row['doc_no']}#{row['line_no']}"


def test_holdout_is_generated_by_script():
    assert _load(HOLDOUT) == golden_holdout_gen.build_holdout()


def test_adversarial_is_generated_by_script():
    assert _load(ADVERSARIAL) == golden_holdout_gen.build_adversarial()


def test_holdout_metadata_is_honest():
    holdout = _load(HOLDOUT)
    fixture = _load(FIXTURE)
    assert holdout["holdout_round"] == 1
    assert holdout["holdout_version"]
    assert holdout["carve_version"]
    assert holdout["source_sha256"] == _sha256(FIXTURE)
    assert holdout["label_version"] == fixture["label_version"]
    assert holdout["totals"]["supplier_lines"] >= MIN_HOLDOUT_ROWS
    # 夹具实际 19 类场景，必须全覆盖；需求方口径的 24 类不存在于夹具
    assert holdout["totals"]["scenario_tags"] == len(fixture["scenario_tags"]) == 19
    assert set(holdout["scenario_coverage"]) == set(fixture["scenario_tags"])
    # v0 取样缺陷必须留痕，不能只写「第 1 轮」就把首跑抹掉
    history = holdout["carve_history"]
    assert [item["version"] for item in history] == ["0.1.0", "1.0.0"]
    assert "作废" in history[0]["status"]
    assert history[0]["engine_changed"] is False


def test_holdout_keeps_labels_and_rows_consistent():
    holdout = _load(HOLDOUT)
    for batch in holdout["batches"]:
        supplier_refs = {_ref(row) for row in batch["input"]["supplier_lines"]}
        our_refs = {_ref(row) for row in batch["input"]["our_lines"]}
        assert set(batch["labels"]["supplier"]) == supplier_refs, batch["batch_id"]
        assert set(batch["labels"]["our"]) <= our_refs, batch["batch_id"]
        for label in batch["labels"]["supplier"].values():
            assert set(label["expected_our_refs"]) <= our_refs, batch["batch_id"]


def test_holdout_preserves_no_conclusion_semantics():
    """降级批次抽样后仍必须因主数据/数据缺失而拒绝出结论，不能只留空壳。"""
    for batch in _load(HOLDOUT)["batches"]:
        if not batch["expected_no_conclusion"]:
            continue
        result = reconcile(build_request(batch["input"]))
        assert result.batch_status is BatchStatus.NO_CONCLUSION, batch["batch_id"]
        assert result.missing_items, batch["batch_id"]


def test_adversarial_is_isolated_from_holdout():
    holdout = _load(HOLDOUT)
    adversarial = _load(ADVERSARIAL)
    holdout_ids = {batch["batch_id"] for batch in holdout["batches"]}
    adversarial_ids = {batch["batch_id"] for batch in adversarial["batches"]}
    assert holdout_ids.isdisjoint(adversarial_ids)
    kinds = {batch["adversarial_kind"] for batch in adversarial["batches"]}
    assert REQUIRED_ADVERSARIAL_KINDS <= kinds
    # 期望值修正必须留痕（对抗集只报指标，不调参）
    revision = adversarial["label_revision"][0]
    assert revision["engine_changed"] is False
    assert len(revision["changes"]) == 2
    # P0 修复后的口径变更单独留痕，且明确写 engine_changed=True
    latest = adversarial["label_revision"][-1]
    assert latest["version"].endswith("1.2.0")
    assert latest["engine_changed"] is True
    assert len(latest["changes"]) == 2


def test_report_is_reproducible():
    committed = _load(REPORT)
    assert committed == golden_holdout_eval.build_report()
    assert committed["training"]["match_denominator"] == 375
    assert committed["holdout"]["evaluated_times"] == 1
    assert committed["holdout"]["statistically_independent"] is False
    assert committed["holdout"]["sha256"] == _sha256(HOLDOUT)
    assert committed["adversarial"]["sha256"] == _sha256(ADVERSARIAL)
    assert committed["adversarial"]["tuned_on"] is False
