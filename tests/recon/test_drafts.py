"""差异单草稿生成（FR-106）：只产草稿、按供应商×类型聚合、置信度取最弱一环。"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from apps.recon import load_scenario, reconcile

SCENARIO_DIR = Path(__file__).resolve().parents[2] / "fixtures" / "recon"


def test_exact_match_produces_no_draft():
    result = reconcile(load_scenario(SCENARIO_DIR / "scenario_01_exact_match.json").request)
    assert result.discrepancy_drafts == ()


def test_amount_diff_produces_one_draft_per_type():
    result = reconcile(load_scenario(SCENARIO_DIR / "scenario_02_amount_diff.json").request)

    assert len(result.discrepancy_drafts) == 2
    by_type = {draft.diff_type.value: draft for draft in result.discrepancy_drafts}
    assert set(by_type) == {"单价不一致", "税率口径"}
    assert by_type["单价不一致"].amount.amount == Decimal(50)
    assert by_type["税率口径"].amount.amount == Decimal(27)
    assert all(draft.status == "草稿" for draft in result.discrepancy_drafts)
    assert all(draft.supplier_code == "SUP-DEMO-01" for draft in result.discrepancy_drafts)
    assert all(draft.period == "2026-10" for draft in result.discrepancy_drafts)
    assert all(draft.needs_manual is False for draft in result.discrepancy_drafts)
    assert all(draft.refs for draft in result.discrepancy_drafts)


def test_draft_numbers_are_deterministic():
    scenario = load_scenario(SCENARIO_DIR / "scenario_03_qty_scrap.json")
    first = reconcile(scenario.request)
    second = reconcile(scenario.request)

    assert [d.draft_no for d in first.discrepancy_drafts] == [
        d.draft_no for d in second.discrepancy_drafts
    ]
    assert len(first.discrepancy_drafts) == 2
