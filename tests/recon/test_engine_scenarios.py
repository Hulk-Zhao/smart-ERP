"""夹具驱动的端到端对账场景：完全匹配 / 金额差异 / 数量差异 / 缺主数据降级。"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from apps.recon import load_scenarios, reconcile
from apps.recon.models import BatchStatus, DiffType, MissingKind

SCENARIOS = load_scenarios(Path(__file__).resolve().parents[2] / "fixtures" / "recon")


def test_fixture_set_covers_required_scenarios():
    names = {scenario.name for scenario in SCENARIOS}
    assert len(SCENARIOS) >= 3
    assert "scenario_01_exact_match" in names
    assert "scenario_02_amount_diff" in names
    assert "scenario_03_qty_scrap" in names
    assert "scenario_04_missing_masterdata" in names


@pytest.mark.parametrize(
    "scenario", SCENARIOS, ids=[scenario.name for scenario in SCENARIOS]
)
def test_scenario_expected_outcome(scenario):
    result = reconcile(scenario.request)
    expected = scenario.expected

    assert result.batch_status.value == expected["batch_status"]
    assert result.conclusive is expected["conclusive"]

    if result.conclusive:
        assert result.metrics is not None
        assert result.metrics.matched == expected["matched"]
        if "matched_rate" in expected:
            assert f"{result.metrics.matched_rate:.4f}" == expected["matched_rate"]
        if expected["diff_types"]:
            assert f"{result.metrics.diff_amount:.4f}" == expected["diff_amount"]
    else:
        # 无法对账时不产出任何指标，避免被误读成「已算过」
        assert result.metrics is None
        assert expected["matched"] == 0

    assert sorted(diff.type.value for diff in result.diffs) == sorted(
        expected["diff_types"]
    )

    assert sorted(item.kind.value for item in result.missing_items) == sorted(
        expected["missing_kinds"]
    )


def test_no_conclusion_scenario_has_no_matching_or_diff_output():
    scenario = next(s for s in SCENARIOS if s.name == "scenario_04_missing_masterdata")
    result = reconcile(scenario.request)

    assert result.batch_status is BatchStatus.NO_CONCLUSION
    assert result.conclusive is False
    assert result.lines == ()
    assert result.diffs == ()
    assert result.unattributed == ()
    assert result.metrics is None
    kinds = {item.kind for item in result.missing_items}
    assert MissingKind.UNIT_CONVERSION in kinds
    assert MissingKind.ITEM in kinds


def test_exact_match_scenario_is_clean():
    scenario = next(s for s in SCENARIOS if s.name == "scenario_01_exact_match")
    result = reconcile(scenario.request)

    assert result.batch_status is BatchStatus.RECONCILED
    assert result.diffs == ()
    assert result.unattributed == ()
    assert all(line.match_type.value == "exact" for line in result.lines)
    assert result.metrics.auto_disposal_allowed is True
    assert result.evidence, "已对账也必须留下可下钻证据（G-06）"


def test_amount_diff_scenario_attributes_price_and_tax():
    scenario = next(s for s in SCENARIOS if s.name == "scenario_02_amount_diff")
    result = reconcile(scenario.request)

    by_type = {diff.type: diff for diff in result.diffs}
    assert set(by_type) == {DiffType.UNIT_PRICE, DiffType.TAX}
    assert by_type[DiffType.UNIT_PRICE].amount.amount == Decimal("50")
    assert by_type[DiffType.TAX].amount.amount == Decimal("27")
    assert all(diff.needs_manual is False for diff in result.diffs)


def test_qty_scenario_attributes_scrap_and_qty():
    scenario = next(s for s in SCENARIOS if s.name == "scenario_03_qty_scrap")
    result = reconcile(scenario.request)

    by_type = {diff.type: diff for diff in result.diffs}
    assert set(by_type) == {DiffType.SCRAP, DiffType.QTY}
    assert by_type[DiffType.SCRAP].amount.amount == Decimal("60")
    assert by_type[DiffType.QTY].amount.amount == Decimal("100")
    assert result.metrics.suspected_anomaly is False
