from __future__ import annotations

import json
from decimal import Decimal

import pytest

from apps.masterdata import (
    DowngradeListUnavailable,
    InspectionOptions,
    ObjectType,
    ReconLine,
    Severity,
    build_downgrade_list,
    evaluate_block_impact,
    export_downgrade_list,
    inspect,
    load_downgrade_list,
    parse_downgrade_list,
    recon_lines_from_dict,
)


def test_impact_counts_blocked_lines(dirty_report, dirty_snapshot, recon_lines):
    impact = evaluate_block_impact(
        dirty_report, recon_lines, snapshot=dirty_snapshot
    )
    assert impact.total_lines == 10
    assert impact.affected_lines == 4
    assert impact.affected_ratio == Decimal("0.4")
    assert impact.global_downgrade is False
    assert impact.threshold_exceeded is True
    assert impact.triggered is True
    assert impact.conclusion.startswith("当前规则下本期 40% 的行会被降级（4/10 行）")


def test_impact_aggregates_subjects_into_three_types(
    dirty_report, dirty_snapshot, recon_lines
):
    impact = evaluate_block_impact(
        dirty_report, recon_lines, snapshot=dirty_snapshot
    )
    subjects = {
        (subject.object_type, subject.code): subject for subject in impact.subjects
    }
    assert set(impact.by_subject_type()) == {"供应商", "客户", "物料"}
    assert subjects[(ObjectType.ITEM, "IT-1004")].blocked_rules == ("UOM-003",)
    assert subjects[(ObjectType.SUPPLIER, "SUP-012")].blocked_rules == ("TAX-001",)
    assert subjects[(ObjectType.CUSTOMER, "CUS-001")].blocked_rules == (
        "CUST-001",
    )
    # 税码类阻断按快照反查到引用主体
    sup010 = subjects[(ObjectType.SUPPLIER, "SUP-010")]
    assert sup010.blocked_rules == ("TAX-002",)
    assert sup010.affected_lines == 1
    assert sup010.reasons == ("开票税率 0.06，申报口径税率 0.05",)


def test_threshold_is_configurable(dirty_report, dirty_snapshot, recon_lines):
    relaxed = evaluate_block_impact(
        dirty_report,
        recon_lines,
        threshold=Decimal("0.5"),
        snapshot=dirty_snapshot,
    )
    assert relaxed.threshold_exceeded is False
    assert relaxed.triggered is False
    assert relaxed.alerts == ()

    strict = evaluate_block_impact(
        dirty_report,
        recon_lines,
        threshold=Decimal("0.2"),
        snapshot=dirty_snapshot,
    )
    assert strict.threshold_exceeded is True
    assert [alert.code for alert in strict.alerts] == [
        "BLOCK_LINE_RATIO_EXCEEDED"
    ]
    assert strict.alerts[0].level == "告警"


def test_default_threshold_comes_from_options(dirty_snapshot, recon_lines):
    report = inspect(
        dirty_snapshot,
        InspectionOptions(block_line_ratio_threshold=Decimal("0.5")),
    )
    impact = evaluate_block_impact(report, recon_lines, snapshot=dirty_snapshot)
    assert impact.threshold == Decimal("0.5")
    assert impact.threshold_exceeded is False


def test_out_of_period_lines_are_excluded(dirty_report, dirty_snapshot):
    lines = recon_lines_from_dict(
        {
            "lines": [
                {"line_id": "L1", "supplier_code": "SUP-012", "period": "2026-09", "billable": True},
                {"line_id": "L2", "supplier_code": "SUP-012", "period": "2026-10", "billable": True},
                {"line_id": "L3", "supplier_code": "SUP-004", "period": "2026-10", "billable": True},
            ]
        }
    )
    impact = evaluate_block_impact(dirty_report, lines, snapshot=dirty_snapshot)
    assert impact.out_of_period_lines == 1
    assert impact.total_lines == 2
    assert impact.affected_lines == 1
    assert impact.affected_ratio == Decimal("0.5")


def test_unattributed_block_downgrades_whole_batch(dirty_report, recon_lines):
    # 不传快照时税码类阻断无法反查主体，必须按 fail-closed 整批降级
    impact = evaluate_block_impact(dirty_report, recon_lines)
    assert impact.global_downgrade is True
    assert impact.affected_lines == impact.total_lines == 10
    assert impact.affected_ratio == Decimal(1)
    assert impact.unattributed_rule_ids == ("TAX-002",)
    assert {alert.code for alert in impact.alerts} == {
        "BLOCK_LINE_RATIO_EXCEEDED",
        "UNATTRIBUTED_BLOCK",
    }


def test_clean_report_has_zero_impact(clean_report, clean_snapshot, recon_lines):
    impact = evaluate_block_impact(
        clean_report, recon_lines, snapshot=clean_snapshot
    )
    assert impact.affected_lines == 0
    assert impact.affected_ratio == Decimal(0)
    assert impact.triggered is False
    assert impact.subjects == ()
    assert "0%" in impact.conclusion


def test_downgrade_list_is_deterministic_and_sorted(dirty_report, dirty_snapshot):
    first = build_downgrade_list(dirty_report, dirty_snapshot)
    second = build_downgrade_list(dirty_report, dirty_snapshot)
    assert first.to_dict() == second.to_dict()
    assert first.entries
    keys = [
        (
            entry.subject_type.value,
            entry.subject_code,
            entry.rule_id,
            entry.period or "",
            entry.reason,
        )
        for entry in first.entries
    ]
    assert keys == sorted(keys)
    assert all(entry.severity is Severity.BLOCK for entry in first.entries)
    assert all(entry.reason for entry in first.entries)
    assert first.downgrade_all is False
    assert first.unattributed_rule_ids == ()
    assert sum(
        1
        for entry in first.entries
        if entry.subject_code == "SUP-010" and entry.rule_id == "TAX-002"
    ) == 1


def test_downgrade_list_policy_is_fail_closed(dirty_report, dirty_snapshot):
    payload = build_downgrade_list(dirty_report, dirty_snapshot).to_dict()
    assert payload["schema_version"] == "1.0"
    assert payload["kind"] == "masterdata_downgrade_list"
    assert payload["policy"]["on_unavailable"] == "fail_closed"
    assert payload["policy"]["on_unattributed"] == "downgrade_all"
    assert payload["summary"]["subject_count"] == len(
        {(entry["subject_type"], entry["subject_code"]) for entry in payload["entries"]}
    )


def test_export_and_load_roundtrip(tmp_path, dirty_report, dirty_snapshot):
    target = export_downgrade_list(
        dirty_report, tmp_path / "downgrades.json", dirty_snapshot
    )
    text = target.read_text(encoding="utf-8")
    assert text.endswith("\n")
    assert not text.startswith("\ufeff")
    loaded = load_downgrade_list(target)
    assert loaded.to_dict() == build_downgrade_list(
        dirty_report, dirty_snapshot
    ).to_dict()


def test_export_without_snapshot_marks_downgrade_all(
    tmp_path, dirty_report
):
    target = export_downgrade_list(dirty_report, tmp_path / "downgrades.json")
    payload = json.loads(target.read_text(encoding="utf-8"))
    assert payload["downgrade_all"] is True
    assert payload["unattributed_rule_ids"] == ["TAX-002"]


def test_load_missing_file_fails_closed(tmp_path):
    with pytest.raises(DowngradeListUnavailable):
        load_downgrade_list(tmp_path / "missing.json")


def test_load_corrupt_json_fails_closed(tmp_path):
    target = tmp_path / "downgrades.json"
    target.write_text("{not json", encoding="utf-8")
    with pytest.raises(DowngradeListUnavailable):
        load_downgrade_list(target)


def test_parse_rejects_incompatible_schema():
    with pytest.raises(DowngradeListUnavailable):
        parse_downgrade_list({})
    with pytest.raises(DowngradeListUnavailable):
        parse_downgrade_list(
            {"schema_version": "9.9", "kind": "masterdata_downgrade_list"}
        )


def test_parse_rejects_non_block_entry(dirty_report, dirty_snapshot):
    payload = build_downgrade_list(dirty_report, dirty_snapshot).to_dict()
    payload["entries"][0]["severity"] = "提示"
    with pytest.raises(DowngradeListUnavailable):
        parse_downgrade_list(payload)


def test_recon_line_billable_has_no_default():
    # 计费行默认不计费：漏标直接报错，绝不默认计入。
    with pytest.raises(TypeError):
        ReconLine("L1", supplier_code="SUP-012")


def test_recon_lines_from_dict_requires_explicit_billable():
    with pytest.raises(TypeError, match="缺少 billable"):
        recon_lines_from_dict({"lines": [{"line_id": "L1"}]})
    with pytest.raises(TypeError, match="必须是布尔值"):
        recon_lines_from_dict({"lines": [{"line_id": "L2", "billable": "yes"}]})
