from __future__ import annotations

import csv
import io
from decimal import Decimal

from apps.masterdata import (
    RULES,
    FindingFilter,
    InspectionOptions,
    Severity,
    inspect,
    render_csv,
    write_csv,
)
from apps.masterdata.models import ObjectType


def test_summary_counts_match_findings(dirty_report):
    summary = dirty_report.summary()
    assert summary["total"] == len(dirty_report.findings)
    assert summary["blocked"] + summary["warnings"] == summary["total"]
    assert set(summary["by_rule"]) == {item.rule_id for item in dirty_report.findings}


def test_filter_by_supplier(dirty_report):
    filtered = dirty_report.filter(supplier="SUP-001")
    assert filtered.findings
    assert filtered.filters.supplier == "SUP-001"
    for finding in filtered.findings:
        hit = finding.supplier_code == "SUP-001" or (
            finding.object_type is ObjectType.SUPPLIER
            and finding.object_code == "SUP-001"
        )
        assert hit
    # 原始报表不受影响
    assert len(dirty_report.findings) > len(filtered.findings)


def test_filter_by_item(dirty_report):
    filtered = dirty_report.filter(item="IT-1001")
    assert filtered.findings
    for finding in filtered.findings:
        hit = finding.item_code == "IT-1001" or (
            finding.object_type is ObjectType.ITEM
            and finding.object_code == "IT-1001"
        )
        assert hit


def test_filter_by_period_keeps_masterdata_findings(dirty_report):
    filtered = dirty_report.filter(period="2026-09")
    assert filtered.findings
    # 带期间的事实只能是 2026-09；主数据级问题 period=None，始终保留
    assert all(
        finding.period in (None, "2026-09") for finding in filtered.findings
    )
    assert any(finding.rule_id == "UOM-005" for finding in filtered.findings)
    assert any(finding.rule_id == "MISS-001" for finding in filtered.findings)
    # 2026-08 未结账不应出现在 2026-09 的过滤结果里
    assert not any(
        finding.rule_id == "PERIOD-001" for finding in filtered.findings
    )


def test_filter_with_no_match_returns_empty(dirty_report):
    filtered = dirty_report.filter(supplier="SUP-NOT-EXIST")
    assert filtered.findings == ()
    assert filtered.blocked_count == 0
    assert filtered.filters == FindingFilter(supplier="SUP-NOT-EXIST")


def test_render_csv_header_and_row_count(dirty_report):
    rows = list(csv.reader(io.StringIO(render_csv(dirty_report))))
    assert rows[0][0] == "规则ID"
    assert "为什么值得修" in rows[0]
    assert "误报处理" in rows[0]
    assert len(rows) == len(dirty_report.findings) + 1
    assert all(len(row) == len(rows[0]) for row in rows[1:])


def test_render_csv_contains_rule_metadata(dirty_report):
    rows = list(csv.reader(io.StringIO(render_csv(dirty_report))))
    header = rows[0]
    rule_index = header.index("规则ID")
    severity_index = header.index("严重度")
    why_index = header.index("为什么值得修")
    target = next(row for row in rows[1:] if row[rule_index] == "UOM-001")
    assert target[severity_index] == Severity.BLOCK.value
    assert target[why_index]


def test_write_csv_uses_bom_for_excel(tmp_path, dirty_report):
    target = tmp_path / "masterdata_findings.csv"
    result = write_csv(dirty_report, target)
    assert result == target
    assert target.read_bytes().startswith(b"\xef\xbb\xbf")
    rows = list(
        csv.reader(io.StringIO(target.read_text(encoding="utf-8-sig")))
    )
    assert len(rows) == len(dirty_report.findings) + 1


def test_write_filtered_csv(tmp_path, dirty_report):
    filtered = dirty_report.filter(supplier="SUP-001")
    target = write_csv(filtered, tmp_path / "supplier_sup001.csv")
    rows = list(
        csv.reader(io.StringIO(target.read_text(encoding="utf-8-sig")))
    )
    assert len(rows) == len(filtered.findings) + 1
    assert len(rows) > 1


def test_empty_report_csv_has_only_header(clean_report):
    rows = list(csv.reader(io.StringIO(render_csv(clean_report))))
    assert len(rows) == 1


def test_rule_catalog_is_complete():
    ids = [rule.rule_id for rule in RULES]
    assert len(ids) == len(set(ids))
    for rule in RULES:
        assert rule.why.strip()
        assert rule.false_positive.strip()
        assert rule.default_severity in (Severity.BLOCK, Severity.WARN)


def test_report_to_dict_contains_catalog_and_options(dirty_report):
    payload = dirty_report.to_dict()
    assert payload["summary"]["total"] == len(dirty_report.findings)
    assert payload["options"]["factor_outlier_ratio"]
    assert len(payload["rules"]) == len(RULES)
    assert all(
        rule["why"] and rule["false_positive"] for rule in payload["rules"]
    )
    first = payload["findings"][0]
    assert {"rule_id", "severity", "evidence", "suggestion"} <= set(first)


def test_metrics_block_hit_rate(dirty_report):
    metrics = dirty_report.metrics()
    assert metrics["total_findings"] == len(dirty_report.findings)
    assert metrics["blocked_findings"] == dirty_report.blocked_count
    assert metrics["warning_findings"] == dirty_report.warning_count
    assert metrics["block_hit_rate"] == "0.525"
    assert metrics["block_hit_rate_pct"] == "52.5%"
    assert metrics["blocked_rule_count"] == len(metrics["blocked_rules"])
    assert metrics["rules_hit"] <= metrics["total_rules"] == len(RULES)
    assert "阻断命中率" in metrics["block_hit_rate_definition"]
    assert dirty_report.to_dict()["metrics"] == metrics


def test_options_report_block_line_ratio_threshold(dirty_report):
    assert dirty_report.options.block_line_ratio_threshold == Decimal("0.30")
    assert (
        dirty_report.to_dict()["options"]["block_line_ratio_threshold"]
        == "0.30"
    )


def test_options_are_reported_with_findings(clean_snapshot):
    report = inspect(clean_snapshot)
    assert report.options.unused_conversion_months == 12
    assert report.to_dict()["options"]["unused_conversion_months"] == 12


def test_rule_catalog_reports_declared_and_effective_severity(dirty_snapshot):
    report = inspect(
        dirty_snapshot,
        InspectionOptions(
            severity_overrides=(("MISS-003", Severity.WARN),)
        ),
    )
    payload = report.to_dict()
    miss = next(rule for rule in payload["rules"] if rule["rule_id"] == "MISS-003")
    assert miss["default_severity"] == Severity.BLOCK.value
    assert miss["effective_severity"] == Severity.WARN.value
    assert miss["severity_source"] == "配置覆盖"
    untouched = next(rule for rule in payload["rules"] if rule["rule_id"] == "UOM-001")
    assert untouched["effective_severity"] == untouched["default_severity"]
    assert untouched["severity_source"] == "规则默认"
    assert payload["block_semantics"]
    assert payload["options"]["severity_overrides"] == {"MISS-003": "提示"}


def test_csv_uses_effective_severity_after_override(dirty_snapshot):
    report = inspect(
        dirty_snapshot,
        InspectionOptions(
            severity_overrides=(("MISS-003", Severity.WARN),)
        ),
    )
    rows = list(csv.reader(io.StringIO(render_csv(report))))
    header = rows[0]
    rule_index = header.index("规则ID")
    severity_index = header.index("严重度")
    target = next(row for row in rows[1:] if row[rule_index] == "MISS-003")
    assert target[severity_index] == Severity.WARN.value
