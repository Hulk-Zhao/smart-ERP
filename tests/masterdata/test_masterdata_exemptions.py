from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from apps.masterdata import (
    NON_EXEMPTIBLE_RULES,
    RULES,
    ExemptionInvalid,
    ExemptionPolicyViolation,
    Finding,
    InspectionReport,
    ObjectType,
    ReconLine,
    Severity,
    apply_exemptions,
    load_exemptions,
    migrate_v1_to_v2,
    parse_exemptions,
)

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "fixtures" / "masterdata" / "exemptions_sample.json"
RENEWAL = ROOT / "fixtures" / "masterdata" / "exemptions_renewal_sample.json"
AS_OF = date(2026, 11, 11)


def _entry(**overrides):
    entry = {
        "exemption_id": "EX-T-001",
        "subject_type": "物料",
        "subject_code": "IT-1004",
        "rule_id": "UOM-003",
        "submitted_by": "对账运营-测试",
        "submitted_role": "对账运营",
        "submitted_at": "2026-09-28",
        "approved_by": "财务经理-测试",
        "approved_role": "财务",
        "reason": "已核实包装规格",
        "granted_at": "2026-10-01",
        "expires_at": "2026-12-31",
    }
    entry.update(overrides)
    if "basis" not in overrides:
        entry["basis"] = {
            "type": "客户书面确认",
            "ref": f"客户确认函-{entry['exemption_id']}",
            "valid_from": "2026-01-01",
            "valid_to": "2027-12-31",
        }
    return entry


def _payload(*entries):
    return {
        "schema_version": "2.0",
        "kind": "masterdata_exemptions",
        "entries": list(entries),
    }


def _document_report(*, supplier_code=None, item_code=None):
    finding = Finding(
        rule_id="DUP-002",
        rule_name="客户名称重复",
        category="重复主数据",
        severity=Severity.BLOCK,
        object_type=ObjectType.DOCUMENT,
        object_code="DOC-1",
        supplier_code=supplier_code,
        item_code=item_code,
    )
    return InspectionReport(
        source="unit-test",
        org_id=None,
        as_of_period="2026-10",
        findings=(finding,),
        rules=RULES,
    )


def test_sample_registry_tracks_active_and_expired():
    registry = load_exemptions(FIXTURE)
    active = registry.active(AS_OF)
    expired = registry.expired(AS_OF)
    assert [item.exemption_id for item in active] == ["EX-2026-001"]
    assert [item.exemption_id for item in expired] == ["EX-2026-000"]
    assert registry.pending(AS_OF) == ()
    assert active[0].cumulative_count == 2
    payload = registry.to_dict(AS_OF)
    assert payload["schema_version"] == "2.0"
    assert payload["active_ids"] == ["EX-2026-001"]
    assert payload["expired_ids"] == ["EX-2026-000"]
    assert payload["non_exemptible_rules"] == NON_EXEMPTIBLE_RULES
    assert payload["policy"]["billing_roles"] == ["商务", "销售", "客户成功"]
    assert payload["policy"]["line_ratio_limit"] == "0.15"


def test_exemption_has_full_audit_fields():
    entry = parse_exemptions(_payload(_entry())).entries[0]
    payload = entry.to_dict()
    assert payload["submitted_by"] == "对账运营-测试"
    assert payload["approved_role"] == "财务"
    assert payload["basis_type"] == "客户书面确认"
    assert payload["basis_valid_from"] == "2026-01-01"
    assert payload["basis_valid_to"] == "2027-12-31"
    assert payload["cumulative_count"] == 1
    assert payload["second_reviewer"] == ""
    assert payload["escalated_review_by"] == ""


def test_expiry_is_automatic():
    registry = load_exemptions(FIXTURE)
    current = next(
        item for item in registry.entries if item.exemption_id == "EX-2026-001"
    )
    assert current.is_active(date(2026, 10, 1)) is True
    assert current.is_active(date(2026, 12, 31)) is True
    assert current.is_expired(date(2027, 1, 1)) is True
    assert current.is_pending(date(2026, 9, 30)) is True
    expired = next(
        item for item in registry.entries if item.exemption_id == "EX-2026-000"
    )
    assert expired.is_active(AS_OF) is False
    assert expired.is_expired(AS_OF) is True


def test_apply_without_lines_marks_cap_unchecked(dirty_report, dirty_snapshot):
    registry = load_exemptions(FIXTURE)
    report = apply_exemptions(dirty_report, registry, dirty_snapshot, at=AS_OF)
    assert report.blocked_count == 20
    assert report.exemption_cap_checked is False
    assert "未提供行级数据" in report.exemption_cap_note


def test_apply_with_lines_checks_cap(dirty_report, dirty_snapshot, recon_lines):
    registry = load_exemptions(FIXTURE)
    report = apply_exemptions(
        dirty_report, registry, dirty_snapshot, at=AS_OF, lines=recon_lines
    )
    assert report.blocked_count == 20
    assert report.exemption_cap_checked is True
    assert "已校验" in report.exemption_cap_note


def test_apply_downgrades_only_covered_block(dirty_report, dirty_snapshot):
    registry = load_exemptions(FIXTURE)
    before = dirty_report.blocked_count
    report = apply_exemptions(dirty_report, registry, dirty_snapshot, at=AS_OF)

    assert before == 21
    assert report.blocked_count == before - 1
    assert report.warning_count == dirty_report.warning_count + 1

    exempted = [
        item
        for item in report.findings
        if item.rule_id == "UOM-003" and item.severity is Severity.WARN
    ]
    assert len(exempted) == 1
    assert any(evidence.field == "豁免" for evidence in exempted[0].evidence)

    assert len(report.exemptions) == 1
    applied = report.exemptions[0]
    assert applied.exemption_id == "EX-2026-001"
    assert applied.subject_code == "IT-1004"
    assert applied.findings == ("单位换算:IT-1004",)
    assert applied.basis_ref == "客户确认函-2026-0912-包装规格"
    assert applied.cumulative_count == 2


def test_apply_before_grant_keeps_block(dirty_report, dirty_snapshot):
    registry = load_exemptions(FIXTURE)
    report = apply_exemptions(
        dirty_report, registry, dirty_snapshot, at=date(2026, 6, 1)
    )
    assert report.blocked_count == dirty_report.blocked_count
    assert report.exemptions == ()


def test_apply_after_expiry_keeps_block(dirty_report, dirty_snapshot):
    registry = load_exemptions(FIXTURE)
    report = apply_exemptions(
        dirty_report, registry, dirty_snapshot, at=date(2027, 1, 1)
    )
    assert report.blocked_count == dirty_report.blocked_count
    assert report.exemptions == ()


def test_apply_requires_every_subject_to_be_exempted():
    report = _document_report(supplier_code="SUP-X", item_code="IT-X")
    only_supplier = parse_exemptions(
        _payload(
            _entry(
                exemption_id="EX-SUP",
                rule_id="DUP-002",
                subject_type="供应商",
                subject_code="SUP-X",
            )
        )
    )
    partial = apply_exemptions(report, only_supplier, snapshot=None, at=AS_OF)
    assert partial.findings[0].severity is Severity.BLOCK
    assert partial.exemptions == ()

    both = parse_exemptions(
        _payload(
            _entry(
                exemption_id="EX-SUP",
                rule_id="DUP-002",
                subject_type="供应商",
                subject_code="SUP-X",
            ),
            _entry(
                exemption_id="EX-ITEM",
                rule_id="DUP-002",
                subject_type="物料",
                subject_code="IT-X",
            ),
        )
    )
    covered = apply_exemptions(report, both, snapshot=None, at=AS_OF)
    assert covered.findings[0].severity is Severity.WARN
    assert {item.exemption_id for item in covered.exemptions} == {
        "EX-SUP",
        "EX-ITEM",
    }


def test_billing_role_cannot_approve():
    with pytest.raises(ExemptionInvalid, match="计费受益角色"):
        parse_exemptions(_payload(_entry(approved_role="销售")))
    with pytest.raises(ExemptionInvalid, match="计费受益角色"):
        parse_exemptions(_payload(_entry(approved_role="客户成功")))


def test_only_finance_or_internal_control_can_approve():
    with pytest.raises(ExemptionInvalid, match="批准角色必须是财务/内控"):
        parse_exemptions(_payload(_entry(approved_role="运营")))


def test_submitter_must_be_reconciliation_ops():
    with pytest.raises(ExemptionInvalid, match="提单角色必须是"):
        parse_exemptions(_payload(_entry(submitted_role="财务")))


def test_submitter_and_approver_must_differ():
    with pytest.raises(ExemptionInvalid, match="不能是同一人"):
        parse_exemptions(
            _payload(_entry(submitted_by="同一人", approved_by="同一人"))
        )


def test_customer_written_confirmation_is_required():
    with pytest.raises(ExemptionInvalid, match="客户书面确认"):
        parse_exemptions(
            _payload(_entry(basis={"type": "工单", "ref": "WO-1"}))
        )


def test_wildcard_subject_is_rejected_as_blanket():
    for code in ("*", "ALL", "全部"):
        with pytest.raises(ExemptionInvalid, match="整批/通配"):
            parse_exemptions(_payload(_entry(subject_code=code)))


@pytest.mark.parametrize(
    "rule_id",
    ["DUP-001", "TAX-001", "TAX-002", "MISS-003", "CODE-001", "CUST-001"],
)
def test_structural_rules_cannot_be_exempted(rule_id):
    assert rule_id in NON_EXEMPTIBLE_RULES
    with pytest.raises(ExemptionInvalid, match="不允许豁免"):
        parse_exemptions(_payload(_entry(rule_id=rule_id)))


def test_non_exemptible_list_is_a_subset_of_catalog():
    assert set(NON_EXEMPTIBLE_RULES) <= {rule.rule_id for rule in RULES}
    assert len(NON_EXEMPTIBLE_RULES) == 13
    assert all(reason for reason in NON_EXEMPTIBLE_RULES.values())


def test_unknown_rule_is_rejected():
    with pytest.raises(ExemptionInvalid, match="不在规则目录"):
        parse_exemptions(_payload(_entry(rule_id="NOPE-999")))


def test_missing_basis_is_rejected():
    entry = _entry()
    entry.pop("basis")
    with pytest.raises(ExemptionInvalid, match="basis"):
        parse_exemptions(_payload(entry))


def test_basis_validity_is_required():
    entry = _entry()
    entry["basis"].pop("valid_from")
    with pytest.raises(ExemptionInvalid, match="basis.valid_from"):
        parse_exemptions(_payload(entry))


def test_exemption_must_fit_within_confirmation_validity():
    with pytest.raises(ExemptionInvalid, match="超出客户确认函"):
        parse_exemptions(
            _payload(
                _entry(
                    basis={
                        "type": "客户书面确认",
                        "ref": "客户确认函-越界",
                        "valid_from": "2026-09-01",
                        "valid_to": "2026-11-30",
                    }
                )
            )
        )


def test_basis_validity_range_must_be_ordered():
    with pytest.raises(ExemptionInvalid, match="有效期非法"):
        parse_exemptions(
            _payload(
                _entry(
                    basis={
                        "type": "客户书面确认",
                        "ref": "客户确认函-倒置",
                        "valid_from": "2027-01-01",
                        "valid_to": "2026-12-31",
                    }
                )
            )
        )


def test_basis_ref_cannot_be_reused_across_exemptions():
    shared = "客户确认函-同一份"
    with pytest.raises(ExemptionInvalid, match="不得跨豁免复用"):
        parse_exemptions(
            _payload(
                _entry(
                    exemption_id="EX-A",
                    basis={
                        "type": "客户书面确认",
                        "ref": shared,
                        "valid_from": "2026-01-01",
                        "valid_to": "2027-12-31",
                    },
                ),
                _entry(
                    exemption_id="EX-B",
                    basis={
                        "type": "客户书面确认",
                        "ref": shared,
                        "valid_from": "2026-01-01",
                        "valid_to": "2027-12-31",
                    },
                ),
            )
        )


def test_expiry_before_grant_is_rejected():
    with pytest.raises(ExemptionInvalid, match="早于生效日"):
        parse_exemptions(
            _payload(_entry(granted_at="2026-12-31", expires_at="2026-10-01"))
        )


def test_duplicate_id_is_rejected():
    with pytest.raises(ExemptionInvalid, match="重复"):
        parse_exemptions(_payload(_entry(), _entry()))


def test_overlapping_window_same_subject_and_rule_is_rejected():
    with pytest.raises(ExemptionInvalid, match="重叠"):
        parse_exemptions(
            _payload(
                _entry(exemption_id="EX-A"),
                _entry(
                    exemption_id="EX-B",
                    granted_at="2026-12-01",
                    expires_at="2027-01-31",
                ),
            )
        )


def test_adjacent_windows_are_allowed():
    registry = parse_exemptions(
        _payload(
            _entry(exemption_id="EX-A"),
            _entry(
                exemption_id="EX-B",
                granted_at="2027-01-01",
                expires_at="2027-03-31",
            ),
        )
    )
    assert len(registry.entries) == 2


def test_invalid_subject_type_is_rejected():
    with pytest.raises(ExemptionInvalid, match="主体类型"):
        parse_exemptions(_payload(_entry(subject_type="账套")))


def test_schema_1_0_error_is_self_explanatory():
    with pytest.raises(ExemptionInvalid) as exc:
        parse_exemptions(
            {"schema_version": "1.0", "kind": "masterdata_exemptions", "entries": []}
        )
    message = str(exc.value)
    assert "schema 1.0" in message
    assert "--migrate-exemptions" in message
    assert "EXEMPTIONS.md" in message
    assert "不能自动猜" in message


def test_migrate_v1_to_v2_requires_manual_roles():
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
    with pytest.raises(ExemptionInvalid, match="必须手工提供"):
        migrate_v1_to_v2(v1, {})

    migrated = migrate_v1_to_v2(
        v1,
        {
            "EX-OLD": {
                "submitted_by": "对账运营-李示例",
                "submitted_role": "对账运营",
                "submitted_at": "2026-09-28",
                "approved_role": "财务",
                "basis_ref": "客户确认函-2026-0912",
                "basis_valid_from": "2026-09-12",
                "basis_valid_to": "2026-12-31",
            }
        },
    )
    assert migrated["schema_version"] == "2.0"
    assert migrated["entries"][0]["basis"]["type"] == "客户书面确认"
    assert migrated["entries"][0]["basis"]["valid_to"] == "2026-12-31"
    parse_exemptions(migrated)


def test_cumulative_count_over_three_requires_escalation():
    base = load_exemptions(RENEWAL)
    current = next(
        item for item in base.entries if item.exemption_id == "EX-2026-104"
    )
    assert current.cumulative_count == 4
    assert current.escalated_review_by == "财务总监-赵示例"

    raw = {
        "schema_version": "2.0",
        "kind": "masterdata_exemptions",
        "entries": [
            {
                **_entry(
                    exemption_id=f"EX-{index}",
                    submitted_at=f"2026-0{index}-01",
                    granted_at=f"2026-0{index}-01",
                    expires_at=f"2026-0{index}-28",
                )
            }
            for index in range(1, 5)
        ],
    }
    with pytest.raises(ExemptionInvalid, match="第 4 次豁免"):
        parse_exemptions(raw)


def test_blanket_exemption_is_rejected(dirty_report, dirty_snapshot):
    registry = load_exemptions(FIXTURE)
    lines = (
        ReconLine("L1", item_code="IT-1004", billable=True),
        ReconLine("L2", item_code="IT-1004", billable=True),
    )
    with pytest.raises(ExemptionPolicyViolation, match="整批豁免被拒绝"):
        apply_exemptions(
            dirty_report, registry, dirty_snapshot, at=AS_OF, lines=lines
        )


def test_over_15_percent_requires_second_reviewer(dirty_report, dirty_snapshot):
    registry = parse_exemptions(_payload(_entry()))
    lines = (
        ReconLine("L1", item_code="IT-1004", billable=True),
        ReconLine("L2", item_code="IT-1004", billable=True),
        ReconLine("L3", item_code="IT-1004", billable=True),
        ReconLine("L4", item_code="IT-1004", billable=True),
        ReconLine("L5", item_code="IT-3001", billable=True),
        ReconLine("L6", item_code="IT-3001", billable=True),
        ReconLine("L7", item_code="IT-3001", billable=True),
        ReconLine("L8", item_code="IT-3001", billable=True),
        ReconLine("L9", item_code="IT-3001", billable=True),
        ReconLine("L10", item_code="IT-3001", billable=True),
    )
    with pytest.raises(ExemptionPolicyViolation, match="缺少二次复核人"):
        apply_exemptions(
            dirty_report, registry, dirty_snapshot, at=AS_OF, lines=lines
        )


def test_over_15_percent_passes_with_second_reviewer(dirty_report, dirty_snapshot):
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
    report = apply_exemptions(
        dirty_report, registry, dirty_snapshot, at=AS_OF, lines=lines
    )
    assert report.exemption_cap_checked is True
    assert report.exemptions[0].second_reviewer == "内控-王示例"


def test_load_rejects_missing_or_corrupt_file(tmp_path):
    with pytest.raises(ExemptionInvalid, match="无法读取"):
        load_exemptions(tmp_path / "missing.json")
    target = tmp_path / "corrupt.json"
    target.write_text("{not json", encoding="utf-8")
    with pytest.raises(ExemptionInvalid, match="不是合法 JSON"):
        load_exemptions(target)
