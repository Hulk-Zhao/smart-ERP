from __future__ import annotations

import csv
import io
import json
from pathlib import Path

import pytest

from apps.masterdata import RULES
from apps.masterdata.cli import main

ROOT = Path(__file__).resolve().parents[2]
DIRTY = ROOT / "fixtures" / "masterdata" / "dirty.json"
CLEAN = ROOT / "fixtures" / "masterdata" / "clean.json"
LINES = ROOT / "fixtures" / "masterdata" / "recon_lines_sample.json"
EXEMPTIONS = ROOT / "fixtures" / "masterdata" / "exemptions_sample.json"


def test_list_rules_prints_why_and_false_positive(capsys):
    assert main(["--list-rules"]) == 0
    output = capsys.readouterr().out
    for rule in RULES:
        assert rule.rule_id in output
    assert "为什么值得修" in output
    assert "误报处理" in output


def test_cli_exports_csv_and_machine_readable_json(tmp_path, capsys):
    before = DIRTY.read_bytes()
    target = tmp_path / "findings.csv"
    code = main(
        [
            "--input",
            str(DIRTY),
            "--csv",
            str(target),
            "--format",
            "json",
        ]
    )
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["summary"]["total"] == len(payload["findings"])
    rows = list(
        csv.reader(io.StringIO(target.read_text(encoding="utf-8-sig")))
    )
    assert len(rows) == len(payload["findings"]) + 1
    # 只读：输入文件一个字节都不能变
    assert DIRTY.read_bytes() == before


def test_cli_filters_and_fail_on_block(tmp_path, capsys):
    target = tmp_path / "sup001.csv"
    code = main(
        [
            "--input",
            str(DIRTY),
            "--supplier",
            "SUP-001",
            "--csv",
            str(target),
            "--format",
            "json",
            "--fail-on-block",
        ]
    )
    assert code == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["summary"]["blocked"] > 0
    assert payload["filters"]["supplier"] == "SUP-001"
    for finding in payload["findings"]:
        assert finding["supplier_code"] == "SUP-001" or (
            finding["object_type"] == "供应商"
            and finding["object_code"] == "SUP-001"
        )


def test_cli_clean_snapshot_returns_zero(capsys):
    code = main(["--input", str(CLEAN), "--format", "json"])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["summary"]["total"] == 0


def test_cli_requires_input():
    with pytest.raises(SystemExit) as exc:
        main([])
    assert exc.value.code == 2


def test_cli_severity_override_applies_to_findings_and_catalog(capsys):
    code = main(
        [
            "--input",
            str(DIRTY),
            "--format",
            "json",
            "--severity",
            "MISS-003=提示",
        ]
    )
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    miss = [item for item in payload["findings"] if item["rule_id"] == "MISS-003"]
    assert miss and all(item["severity"] == "提示" for item in miss)
    rule = next(
        item for item in payload["rules"] if item["rule_id"] == "MISS-003"
    )
    assert rule["default_severity"] == "阻断"
    assert rule["effective_severity"] == "提示"
    assert rule["severity_source"] == "配置覆盖"
    assert payload["block_semantics"]


def test_cli_list_rules_shows_effective_severity(capsys):
    assert main(["--list-rules", "--severity", "UOM-004=阻断"]) == 0
    output = capsys.readouterr().out
    assert "阻断语义" in output
    assert "配置覆盖" in output
    assert "UOM-004" in output


def test_cli_rejects_unknown_severity_rule():
    with pytest.raises(SystemExit) as exc:
        main(["--list-rules", "--severity", "NOPE-999=提示"])
    assert exc.value.code == 2


def test_cli_reports_impact_and_exports_downgrade_list(tmp_path, capsys):
    target = tmp_path / "downgrades.json"
    code = main(
        [
            "--input",
            str(DIRTY),
            "--lines",
            str(LINES),
            "--downgrades",
            str(target),
            "--format",
            "json",
        ]
    )
    assert code == 0
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["metrics"]["block_hit_rate_pct"] == "52.5%"
    assert payload["impact"]["affected_lines"] == 4
    assert payload["impact"]["affected_ratio_pct"] == "40%"
    assert payload["impact"]["threshold_exceeded"] is True
    assert payload["impact"]["triggered"] is True
    assert payload["impact"]["alerts"][0]["code"] == "BLOCK_LINE_RATIO_EXCEEDED"
    assert "降级清单已导出" in captured.err

    exported = json.loads(target.read_text(encoding="utf-8"))
    assert exported["policy"]["on_unavailable"] == "fail_closed"
    assert exported["downgrade_all"] is False
    assert exported["summary"]["subject_count"] > 0


def test_cli_block_threshold_override_removes_alert(capsys):
    code = main(
        [
            "--input",
            str(DIRTY),
            "--lines",
            str(LINES),
            "--block-threshold",
            "50%",
            "--format",
            "json",
        ]
    )
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["impact"]["threshold"] == "0.5"
    assert payload["impact"]["threshold_exceeded"] is False
    assert payload["impact"]["triggered"] is False
    assert payload["impact"]["alerts"] == []


def test_cli_text_report_prints_metric_and_conclusion(capsys):
    assert main(["--input", str(DIRTY), "--lines", str(LINES)]) == 0
    output = capsys.readouterr().out
    assert "阻断命中率: 52.5%" in output
    assert "当前规则下本期 40% 的行会被降级（4/10 行）" in output
    assert "告警 已触发" in output
    assert "按主体聚合（供应商/客户/物料）" in output


def test_cli_rejects_invalid_block_threshold():
    with pytest.raises(SystemExit) as exc:
        main(["--input", str(DIRTY), "--block-threshold", "1.5"])
    assert exc.value.code == 2


def test_cli_exemptions_reduce_block_and_downgrade_lines(capsys):
    code = main(
        [
            "--input",
            str(DIRTY),
            "--lines",
            str(LINES),
            "--exemptions",
            str(EXEMPTIONS),
            "--as-of",
            "2026-11-11",
            "--format",
            "json",
        ]
    )
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["metrics"]["blocked_findings"] == 20
    assert payload["exemption_count"] == 1
    assert payload["exemption_cap_checked"] is True
    assert payload["exemptions"][0]["exemption_id"] == "EX-2026-001"
    assert payload["exemptions"][0]["approved_role"] == "财务"
    assert payload["exemptions"][0]["cumulative_count"] == 2
    assert payload["exemptions"][0]["findings"] == ["单位换算:IT-1004"]
    audit = payload["exemption_audit"]
    assert audit["as_of"] == "2026-11-11"
    assert audit["active"] == 1
    assert audit["expired"] == 1
    assert audit["unused"] == 0
    assert audit["exemption_cap_checked"] is True
    assert audit["line_ratio_limit"] == "0.15"
    assert [item["exemption_id"] for item in audit["expired_entries"]] == [
        "EX-2026-000"
    ]
    impact = payload["exemption_impact"]
    assert impact["exemption_cap_checked"] is True
    assert impact["covered_lines"] == 1
    assert impact["freed_lines"] == 1
    assert impact["affected_billable_lines"] == 1
    entry = impact["entries"][0]
    assert entry["subject_type"] == "物料"
    assert entry["subject_code"] == "IT-1004"
    assert entry["rule_id"] == "UOM-003"
    assert entry["approved_by_role"] == "财务"
    assert entry["findings_count"] == 1
    assert entry["affected_billable_lines"] == 1
    assert payload["impact"]["affected_lines"] == 3
    assert payload["impact"]["affected_ratio_pct"] == "30%"
    assert payload["impact"]["triggered"] is False


def test_cli_without_lines_marks_exemption_cap_unchecked(capsys):
    code = main(
        [
            "--input",
            str(DIRTY),
            "--exemptions",
            str(EXEMPTIONS),
            "--as-of",
            "2026-11-11",
            "--format",
            "json",
        ]
    )
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["exemption_cap_checked"] is False
    assert "未提供行级数据" in payload["exemption_cap_note"]
    assert payload["exemption_impact"]["exemption_cap_checked"] is False
    assert payload["exemption_impact"]["alerts"][0]["code"] == (
        "EXEMPTION_CAP_UNCHECKED"
    )


def test_cli_text_warns_cap_unchecked(capsys):
    assert (
        main(
            [
                "--input",
                str(DIRTY),
                "--exemptions",
                str(EXEMPTIONS),
                "--as-of",
                "2026-11-11",
            ]
        )
        == 0
    )
    output = capsys.readouterr().out
    assert "[未校验]" in output
    assert "豁免影响面上限未校验" in output


def test_cli_exports_exemption_impact_csv(tmp_path, capsys):
    target = tmp_path / "exemption_impact.csv"
    code = main(
        [
            "--input",
            str(DIRTY),
            "--lines",
            str(LINES),
            "--exemptions",
            str(EXEMPTIONS),
            "--as-of",
            "2026-11-11",
            "--exemption-csv",
            str(target),
            "--format",
            "json",
        ]
    )
    assert code == 0
    captured = capsys.readouterr()
    assert "豁免影响面 CSV 已导出" in captured.err
    text = target.read_text(encoding="utf-8")
    assert "影响面上限已校验" in text
    assert "EX-2026-001" in text
    assert "客户书面确认" in text


def test_cli_migrates_schema_1_to_2(tmp_path, capsys):
    v1 = {
        "schema_version": "1.0",
        "kind": "masterdata_exemptions",
        "entries": [
            {
                "exemption_id": "EX-OLD",
                "subject_type": "物料",
                "subject_code": "IT-1004",
                "rule_id": "UOM-003",
                "approved_by": "财务经理-张示例",
                "basis": {"type": "工单", "ref": "WO-1"},
                "reason": "旧样例",
                "granted_at": "2026-10-01",
                "expires_at": "2026-12-31",
            }
        ],
    }
    roles = {
        "EX-OLD": {
            "submitted_by": "对账运营-李示例",
            "submitted_role": "对账运营",
            "submitted_at": "2026-09-28",
            "approved_role": "财务",
            "basis_ref": "客户确认函-2026-0912",
            "basis_valid_from": "2026-09-12",
            "basis_valid_to": "2026-12-31",
        }
    }
    source = tmp_path / "v1.json"
    mapping = tmp_path / "roles.json"
    out = tmp_path / "v2.json"
    source.write_text(json.dumps(v1, ensure_ascii=False), encoding="utf-8")
    mapping.write_text(json.dumps(roles, ensure_ascii=False), encoding="utf-8")
    assert (
        main(
            [
                "--migrate-exemptions",
                str(source),
                "--roles",
                str(mapping),
                "--out",
                str(out),
            ]
        )
        == 0
    )
    assert "已迁移到 schema 2.0" in capsys.readouterr().err
    migrated = json.loads(out.read_text(encoding="utf-8"))
    assert migrated["schema_version"] == "2.0"
    assert migrated["entries"][0]["approved_role"] == "财务"
    assert migrated["entries"][0]["basis"]["type"] == "客户书面确认"


def test_cli_json_includes_suggestions_and_dirt_tiers(capsys):
    code = main(["--input", str(DIRTY), "--format", "json"])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    suggestions = payload["suggestions"]
    assert suggestions["formula"]
    assert suggestions["difficulty_scale"]
    assert next(item["rule_id"] for item in suggestions["items"]) == "TAX-001"
    tiers = payload["dirt_tiers"]
    assert "dirty.json" in tiers["caveat"]
    assert [tier["tier"] for tier in tiers["tiers"]] == ["轻脏", "中脏", "重脏"]
    assert len(tiers["tiers"]) == 3


def test_cli_text_shows_exemption_audit_and_tiers(capsys):
    assert (
        main(
            [
                "--input",
                str(DIRTY),
                "--lines",
                str(LINES),
                "--exemptions",
                str(EXEMPTIONS),
                "--as-of",
                "2026-11-11",
            ]
        )
        == 0
    )
    output = capsys.readouterr().out
    assert "豁免（评估日 2026-11-11）: 生效 1，已过期 1" in output
    assert "EX-2026-001" in output
    assert "已自动失效" in output
    assert "数据口径:" in output
    assert "轻脏" in output and "中脏" in output and "重脏" in output
    assert "规则收敛建议 Top 5" in output


def test_cli_rejects_non_exemptible_rule(tmp_path):
    payload = json.loads(EXEMPTIONS.read_text(encoding="utf-8"))
    payload["entries"][0]["rule_id"] = "TAX-001"
    target = tmp_path / "bad_exemptions.json"
    target.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(SystemExit) as exc:
        main(["--input", str(DIRTY), "--exemptions", str(target)])
    assert exc.value.code == 2


def test_cli_rejects_invalid_as_of(capsys):
    with pytest.raises(SystemExit) as exc:
        main(
            [
                "--input",
                str(DIRTY),
                "--exemptions",
                str(EXEMPTIONS),
                "--as-of",
                "2026/11/11",
            ]
        )
    assert exc.value.code == 2
