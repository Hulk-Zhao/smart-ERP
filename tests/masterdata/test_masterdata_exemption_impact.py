from __future__ import annotations

from apps.masterdata import (
    ReconLine,
    apply_exemptions,
    evaluate_exemption_impact,
    load_exemptions,
    parse_exemptions,
    render_exemption_impact_csv,
    unchecked_exemption_impact,
)
from apps.masterdata.exemptions import EXEMPTION_LINE_RATIO_LIMIT
from tests.masterdata.test_masterdata_exemptions import (
    AS_OF,
    FIXTURE,
    RENEWAL,
    _entry,
    _payload,
)


def test_impact_reports_covered_freed_and_billable(
    dirty_report, dirty_snapshot, recon_lines
):
    registry = load_exemptions(FIXTURE)
    after = apply_exemptions(
        dirty_report, registry, dirty_snapshot, at=AS_OF, lines=recon_lines
    )
    impact = evaluate_exemption_impact(
        dirty_report, after, recon_lines, dirty_snapshot
    )
    assert impact.cap_checked is True
    assert impact.total_lines == 10
    assert impact.covered_lines == 1
    assert impact.freed_lines == 1
    assert impact.affected_billable_lines == 1
    assert impact.threshold == EXEMPTION_LINE_RATIO_LIMIT
    assert impact.threshold_exceeded is False
    assert impact.alerts == ()
    entry = impact.entries[0]
    assert entry.exemption_id == "EX-2026-001"
    assert entry.findings_count == 1
    assert entry.covered_lines == 1
    assert entry.freed_lines == 1
    assert entry.affected_billable_lines == 1
    assert entry.basis_valid_from == "2026-09-12"
    assert entry.basis_valid_to == "2027-01-31"
    assert entry.cumulative_count == 2
    payload = entry.to_dict()
    for key in (
        "subject_type",
        "subject_code",
        "rule_id",
        "exemption_id",
        "approved_by_role",
        "findings_count",
        "affected_billable_lines",
    ):
        assert key in payload


def test_non_billable_lines_are_not_counted(
    dirty_report, dirty_snapshot, recon_lines
):
    registry = load_exemptions(FIXTURE)
    lines = tuple(
        ReconLine(
            line.line_id,
            supplier_code=line.supplier_code,
            customer_code=line.customer_code,
            item_code=line.item_code,
            period=line.period,
            billable=line.line_id != "LINE-0002",
        )
        for line in recon_lines
    )
    after = apply_exemptions(
        dirty_report, registry, dirty_snapshot, at=AS_OF, lines=lines
    )
    impact = evaluate_exemption_impact(dirty_report, after, lines, dirty_snapshot)
    assert impact.covered_lines == 1
    assert impact.freed_lines == 1
    assert impact.affected_billable_lines == 1
    assert impact.billable_conflict_lines == 0
    assert all(alert.code != "BILLABLE_FLAG_CONFLICT" for alert in impact.alerts)


def test_billable_flag_conflict_is_alerted(
    dirty_report, dirty_snapshot, recon_lines
):
    registry = load_exemptions(FIXTURE)
    # LINE-0001 是唯一被豁免放过的行，却标记为不计费 → 自相矛盾，必须告警。
    lines = tuple(
        ReconLine(
            line.line_id,
            supplier_code=line.supplier_code,
            customer_code=line.customer_code,
            item_code=line.item_code,
            period=line.period,
            billable=line.line_id != "LINE-0001",
        )
        for line in recon_lines
    )
    after = apply_exemptions(
        dirty_report, registry, dirty_snapshot, at=AS_OF, lines=lines
    )
    impact = evaluate_exemption_impact(dirty_report, after, lines, dirty_snapshot)
    assert impact.affected_billable_lines == 0
    assert impact.billable_conflict_lines == 1
    assert impact.billable_conflict_line_ids == ("LINE-0001",)
    assert impact.entries[0].billable_conflict_lines == 1
    alert = next(
        item for item in impact.alerts if item.code == "BILLABLE_FLAG_CONFLICT"
    )
    assert "LINE-0001" in alert.message
    assert "不得静默" in alert.message


def test_unchecked_impact_is_explicit(dirty_report, dirty_snapshot):
    registry = load_exemptions(FIXTURE)
    after = apply_exemptions(dirty_report, registry, dirty_snapshot, at=AS_OF)
    impact = unchecked_exemption_impact(after)
    assert impact.cap_checked is False
    assert "未校验" in impact.cap_note
    assert impact.covered_lines == 0
    assert impact.alerts[0].code == "EXEMPTION_CAP_UNCHECKED"
    assert impact.to_dict()["exemption_cap_checked"] is False


def test_renewal_over_three_emits_alert(dirty_report, dirty_snapshot, recon_lines):
    registry = load_exemptions(RENEWAL)
    after = apply_exemptions(
        dirty_report, registry, dirty_snapshot, at=AS_OF, lines=recon_lines
    )
    impact = evaluate_exemption_impact(
        dirty_report, after, recon_lines, dirty_snapshot
    )
    renewal = [
        alert
        for alert in impact.alerts
        if alert.code == "EXEMPTION_RENEWAL_EXCEEDED"
    ]
    assert len(renewal) == 1
    assert "第 4 次豁免" in renewal[0].message
    assert impact.entries[0].cumulative_count == 4
    assert impact.entries[0].escalated_review_by == "财务总监-赵示例"


def test_ratio_alert_when_over_15_percent(
    dirty_report, dirty_snapshot
):
    registry = parse_exemptions(
        _payload(
            _entry(
                second_reviewer="内控-王示例",
                second_reviewed_role="内控",
            )
        )
    )
    lines = (
        ReconLine("L1", item_code="IT-1004", billable=True),
        ReconLine("L2", item_code="IT-1004", billable=True),
        ReconLine("L3", item_code="IT-3001", billable=True),
        ReconLine("L4", item_code="IT-3001", billable=True),
        ReconLine("L5", item_code="IT-3001", billable=True),
        ReconLine("L6", item_code="IT-3001", billable=True),
        ReconLine("L7", item_code="IT-3001", billable=True),
        ReconLine("L8", item_code="IT-3001", billable=True),
        ReconLine("L9", item_code="IT-3001", billable=True),
        ReconLine("L10", item_code="IT-3001", billable=True),
    )
    after = apply_exemptions(
        dirty_report, registry, dirty_snapshot, at=AS_OF, lines=lines
    )
    impact = evaluate_exemption_impact(dirty_report, after, lines, dirty_snapshot)
    assert impact.threshold_exceeded is True
    assert any(
        alert.code == "EXEMPTION_LINE_RATIO_EXCEEDED"
        for alert in impact.alerts
    )


def test_csv_contains_metering_columns_and_unchecked_marker(
    dirty_report, dirty_snapshot, recon_lines
):
    registry = load_exemptions(FIXTURE)
    after = apply_exemptions(
        dirty_report, registry, dirty_snapshot, at=AS_OF, lines=recon_lines
    )
    impact = evaluate_exemption_impact(
        dirty_report, after, recon_lines, dirty_snapshot
    )
    text = render_exemption_impact_csv(impact)
    header = text.splitlines()[0]
    for column in (
        "豁免编号",
        "主体类型",
        "主体编码",
        "规则ID",
        "批准角色",
        "命中差异数",
        "放过行数",
        "影响计费行数",
        "计费冲突行数",
        "影响面上限已校验",
    ):
        assert column in header
    assert ",是" in text

    unchecked = render_exemption_impact_csv(unchecked_exemption_impact(after))
    assert "未校验" in unchecked
    assert ",否" in unchecked
