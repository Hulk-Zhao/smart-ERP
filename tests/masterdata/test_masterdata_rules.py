from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest

from apps.masterdata import RULES, InspectionOptions, Severity, inspect
from apps.masterdata.models import ObjectType


def findings_for(report, rule_id):
    return [item for item in report.findings if item.rule_id == rule_id]


def test_clean_snapshot_has_no_findings(clean_report):
    assert clean_report.findings == ()
    assert clean_report.blocked_count == 0


@pytest.mark.parametrize("rule_id", [rule.rule_id for rule in RULES])
def test_clean_snapshot_is_negative_case_for_every_rule(clean_report, rule_id):
    """反例：干净数据对每条规则都不应报差异。"""
    assert findings_for(clean_report, rule_id) == []


@pytest.mark.parametrize(
    ("rule_id", "object_code"),
    [
        ("UOM-001", "IT-1001"),
        ("UOM-002", "IT-2001"),
        ("UOM-003", "IT-1004"),
        ("UOM-004", "IT-3003"),
        ("UOM-005", "IT-4001"),
        ("MISS-001", "SUP-005"),
        ("MISS-002", "SUP-006"),
        ("MISS-003", "IT-5001"),
        ("DUP-001", "SUP-001"),
        ("DUP-002", "SUP-008"),
        ("DUP-003", "IT-1001"),
        ("CODE-001", "IT-1001"),
        ("PERIOD-001", "2026-08"),
        ("PERIOD-002", "D-2026-0013"),
        ("LEDGER-001", "D-2026-0014"),
        ("CUR-001", "D-2026-0017"),
        ("CUR-002", "D-2026-0016"),
        ("CUR-003", "EUR"),
        ("TAX-001", "SUP-012"),
        ("TAX-002", "VAT06"),
        ("PAY-001", "SUP-006"),
        ("PAY-002", "SUP-002"),
        ("CUST-001", "CUS-001"),
        ("CUST-002", "CUS-003"),
    ],
)
def test_dirty_snapshot_is_positive_case_for_every_rule(
    dirty_report, rule_id, object_code
):
    """正例：脏数据夹具必须覆盖全部规则，且能定位到对象。"""
    hits = findings_for(dirty_report, rule_id)
    assert hits, f"{rule_id} 未命中"
    assert object_code in {item.object_code for item in hits}


def test_dirty_fixture_covers_all_rules(dirty_report):
    assert {item.rule_id for item in dirty_report.findings} == {
        rule.rule_id for rule in RULES
    }


def test_multi_conversion_is_block(dirty_report):
    hits = findings_for(dirty_report, "UOM-001")
    target = next(item for item in hits if item.object_code == "IT-1001")
    assert target.severity is Severity.BLOCK
    actual = target.evidence[0].actual
    assert "factor=100" in actual and "factor=10" in actual


def test_reciprocal_conversion_is_block(dirty_report):
    target = next(
        item for item in findings_for(dirty_report, "UOM-002")
        if item.object_code == "IT-2001"
    )
    assert target.severity is Severity.BLOCK
    assert "乘积=25" in target.evidence[0].actual


def test_peer_outlier_blocks_at_three_times(dirty_report):
    target = next(
        item for item in findings_for(dirty_report, "UOM-003")
        if item.object_code == "IT-1004"
    )
    assert target.severity is Severity.BLOCK
    assert "偏离 10 倍" in target.evidence[0].actual


def test_peer_outlier_warns_between_thresholds(clean_snapshot):
    conversions = tuple(
        replace(conv, factor=Decimal(200))
        if conv.item_code == "IT-1001" and conv.from_unit == "PCS"
        else conv
        for conv in clean_snapshot.unit_conversions
    )
    report = inspect(replace(clean_snapshot, unit_conversions=conversions))
    target = next(
        item for item in findings_for(report, "UOM-003")
        if item.object_code == "IT-1001"
    )
    assert target.severity is Severity.WARN
    assert "偏离 2 倍" in target.evidence[0].actual


def test_unused_conversion_is_warn(dirty_report):
    target = next(
        item for item in findings_for(dirty_report, "UOM-004")
        if item.object_code == "IT-3003"
    )
    assert target.severity is Severity.WARN
    assert "未使用约 33 个月" in target.evidence[0].actual


def test_missing_conversion_blocks_item_rows(dirty_report):
    target = next(
        item for item in findings_for(dirty_report, "UOM-005")
        if item.object_code == "IT-4001"
    )
    assert target.severity is Severity.BLOCK
    assert target.period == "2026-09"
    assert target.supplier_code == "SUP-003"
    assert "无换算路径" in target.evidence[0].actual


def test_missing_supplier_fields_are_warn(dirty_report):
    tax = next(
        item for item in findings_for(dirty_report, "MISS-001")
        if item.object_code == "SUP-005"
    )
    bank = next(
        item for item in findings_for(dirty_report, "MISS-002")
        if item.object_code == "SUP-006"
    )
    assert tax.severity is Severity.WARN
    assert bank.severity is Severity.WARN


def test_missing_base_unit_is_block(dirty_report):
    target = next(
        item for item in findings_for(dirty_report, "MISS-003")
        if item.object_code == "IT-5001"
    )
    assert target.severity is Severity.BLOCK
    assert target.object_type is ObjectType.ITEM


def test_duplicate_tax_blocks_both_codes(dirty_report):
    codes = {
        item.object_code for item in findings_for(dirty_report, "DUP-001")
    }
    assert {"SUP-001", "SUP-007"} <= codes
    hits = findings_for(dirty_report, "DUP-001")
    assert all(item.severity is Severity.BLOCK for item in hits)


def test_duplicate_bank_and_name_are_warn(dirty_report):
    hits = findings_for(dirty_report, "DUP-002")
    assert all(item.severity is Severity.WARN for item in hits)
    name_hit = next(item for item in hits if item.object_code == "SUP-008")
    assert "名称归一后相同" in name_hit.evidence[0].actual
    bank_hit = next(item for item in hits if item.object_code == "SUP-010")
    assert "银行账号相同" in bank_hit.evidence[0].actual


def test_duplicate_item_blocks_both_codes(dirty_report):
    hits = findings_for(dirty_report, "DUP-003")
    codes = {item.object_code for item in hits}
    assert {"IT-1001", "IT-1001-DUP"} <= codes
    assert all(item.severity is Severity.BLOCK for item in hits)


def test_confusable_code_collisions_block(dirty_report):
    hits = findings_for(dirty_report, "CODE-001")
    item_codes = {
        item.object_code
        for item in hits
        if item.object_type is ObjectType.ITEM
    }
    doc_codes = {
        item.object_code
        for item in hits
        if item.object_type is ObjectType.DOCUMENT
    }
    assert {"IT-1001", "IT-100I"} <= item_codes
    assert {"D-2026-0012", "D-2O26-0012"} <= doc_codes
    assert all(item.severity is Severity.BLOCK for item in hits)


def test_unclosed_period_warns_but_current_period_is_allowed(dirty_report):
    hits = findings_for(dirty_report, "PERIOD-001")
    codes = {item.object_code for item in hits}
    assert "2026-08" in codes
    assert "2026-10" not in codes
    assert all(item.severity is Severity.WARN for item in hits)


def test_cross_period_is_warn(dirty_report):
    target = next(
        item for item in findings_for(dirty_report, "PERIOD-002")
        if item.object_code == "D-2026-0013"
    )
    assert target.severity is Severity.WARN
    assert target.period == "2026-09"


def test_ledger_mismatch_blocks_docs_and_warns_master_data(dirty_report):
    hits = findings_for(dirty_report, "LEDGER-001")
    doc_hit = next(item for item in hits if item.object_code == "D-2026-0014")
    supplier_hit = next(item for item in hits if item.object_code == "SUP-011")
    item_hit = next(item for item in hits if item.object_code == "IT-6001")
    assert doc_hit.severity is Severity.BLOCK
    assert supplier_hit.severity is Severity.WARN
    assert item_hit.severity is Severity.WARN


def test_missing_currency_rate_blocks_foreign_document(dirty_report):
    target = next(
        item for item in findings_for(dirty_report, "CUR-001")
        if item.object_code == "D-2026-0017"
    )
    assert target.severity is Severity.BLOCK
    assert target.period == "2026-08"
    assert "无汇率配置" in target.evidence[0].actual
    assert "未填 exchange_rate" in target.evidence[0].actual


def test_currency_rate_mismatch_blocks_document(dirty_report):
    target = next(
        item for item in findings_for(dirty_report, "CUR-002")
        if item.object_code == "D-2026-0016"
    )
    assert target.severity is Severity.BLOCK
    assert "单据汇率 7" in target.evidence[0].actual
    assert "账套汇率 7.2" in target.evidence[0].actual


def test_stale_currency_rate_warns(dirty_report):
    target = next(
        item for item in findings_for(dirty_report, "CUR-003")
        if item.object_code == "EUR"
    )
    assert target.severity is Severity.WARN
    assert "33 个月" in target.evidence[0].actual


def test_invalid_tax_code_blocks_supplier_and_customer(dirty_report):
    hits = findings_for(dirty_report, "TAX-001")
    codes = {item.object_code for item in hits}
    assert {"SUP-012", "CUS-006"} <= codes
    assert all(item.severity is Severity.BLOCK for item in hits)
    assert all("VAT99" in item.evidence[0].actual for item in hits)


def test_tax_rate_mismatch_blocks_tax_code(dirty_report):
    target = next(
        item for item in findings_for(dirty_report, "TAX-002")
        if item.object_code == "VAT06"
    )
    assert target.severity is Severity.BLOCK
    assert target.object_type is ObjectType.TAX_CODE
    assert "0.06" in target.evidence[0].actual
    assert "0.05" in target.evidence[0].actual


def test_missing_payment_term_warns_supplier_and_customer(dirty_report):
    hits = findings_for(dirty_report, "PAY-001")
    codes = {item.object_code for item in hits}
    assert {"SUP-006", "CUS-005"} <= codes
    assert all(item.severity is Severity.WARN for item in hits)


def test_payment_term_conflict_warns_against_contract(dirty_report):
    target = next(
        item for item in findings_for(dirty_report, "PAY-002")
        if item.object_code == "SUP-002"
    )
    assert target.severity is Severity.WARN
    assert "NET60" in target.evidence[0].actual
    assert "NET30" in target.evidence[0].actual
    assert "C-2026-002" in target.evidence[0].actual


def test_duplicate_customer_tax_blocks_both_codes(dirty_report):
    hits = findings_for(dirty_report, "CUST-001")
    codes = {item.object_code for item in hits}
    assert {"CUS-001", "CUS-002"} <= codes
    assert all(item.severity is Severity.BLOCK for item in hits)


def test_duplicate_customer_name_normalizes_full_width_parentheses(dirty_report):
    hits = findings_for(dirty_report, "CUST-002")
    codes = {item.object_code for item in hits}
    assert {"CUS-003", "CUS-004"} <= codes
    assert all(item.severity is Severity.WARN for item in hits)


def test_severity_override_downgrades_block_to_warn(dirty_snapshot):
    report = inspect(
        dirty_snapshot,
        InspectionOptions(
            severity_overrides=(("MISS-003", Severity.WARN),)
        ),
    )
    hits = findings_for(report, "MISS-003")
    assert hits
    assert all(item.severity is Severity.WARN for item in hits)
    # 未覆盖的规则不受影响
    assert any(item.severity is Severity.BLOCK for item in report.findings)


def test_severity_override_upgrades_warn_to_block(dirty_snapshot):
    report = inspect(
        dirty_snapshot,
        InspectionOptions(
            severity_overrides=(("PERIOD-001", Severity.BLOCK),)
        ),
    )
    hits = findings_for(report, "PERIOD-001")
    assert hits
    assert all(item.severity is Severity.BLOCK for item in hits)


def test_severity_override_rejects_unknown_rule(dirty_snapshot):
    with pytest.raises(ValueError, match="未知规则ID"):
        inspect(
            dirty_snapshot,
            InspectionOptions(
                severity_overrides=(("NOPE-999", Severity.BLOCK),)
            ),
        )
