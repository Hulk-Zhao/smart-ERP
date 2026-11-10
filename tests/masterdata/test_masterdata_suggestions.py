from __future__ import annotations

from decimal import Decimal

from apps.masterdata import (
    PRIORITY_FORMULA,
    RULES,
    build_fix_priorities,
    priorities_payload,
)
from apps.masterdata.suggestions import DIFFICULTY_LABELS, RULE_DIFFICULTY


def test_priorities_return_top_five_sorted(dirty_report, dirty_snapshot):
    items = build_fix_priorities(dirty_report, snapshot=dirty_snapshot, top=5)
    assert len(items) == 5
    assert items[0].rule_id == "TAX-001"
    scores = [item.score for item in items]
    assert scores == sorted(scores, reverse=True)
    assert {item.rule_id for item in items} == {
        "TAX-001",
        "MISS-003",
        "CODE-001",
        "CUST-001",
        "UOM-005",
    }


def test_score_follows_formula(dirty_report, dirty_snapshot):
    for item in build_fix_priorities(dirty_report, snapshot=dirty_snapshot, top=24):
        expected = (
            Decimal(item.blocked_findings)
            * Decimal(item.exclusive_subjects)
            / Decimal(item.difficulty)
        )
        assert item.score == expected
        assert 1 <= item.difficulty <= 5
        assert item.difficulty_label == DIFFICULTY_LABELS[item.difficulty]
        assert item.reason


def test_lines_add_line_level_contribution(dirty_report, dirty_snapshot, recon_lines):
    with_lines = build_fix_priorities(
        dirty_report, recon_lines, snapshot=dirty_snapshot, top=5
    )
    assert any(item.affected_lines > 0 for item in with_lines)
    tax = next(item for item in with_lines if item.rule_id == "TAX-001")
    assert tax.affected_lines >= 1
    assert "影响" in tax.reason and "放行" in tax.reason


def test_clean_report_has_no_priorities(clean_report):
    assert build_fix_priorities(clean_report) == ()


def test_top_zero_returns_empty(dirty_report, dirty_snapshot):
    assert build_fix_priorities(dirty_report, snapshot=dirty_snapshot, top=0) == ()


def test_payload_exposes_formula_and_scale(dirty_report, dirty_snapshot):
    items = build_fix_priorities(dirty_report, snapshot=dirty_snapshot, top=3)
    payload = priorities_payload(items)
    assert payload["formula"] == PRIORITY_FORMULA
    assert payload["difficulty_scale"] == DIFFICULTY_LABELS
    assert len(payload["items"]) == 3
    first = payload["items"][0]
    assert first["rule_id"] == items[0].rule_id
    assert first["score"] == "2"
    assert isinstance(first["score"], str)


def test_every_rule_has_a_difficulty():
    assert set(RULE_DIFFICULTY) == {rule.rule_id for rule in RULES}
    assert set(RULE_DIFFICULTY.values()) <= set(DIFFICULTY_LABELS)
