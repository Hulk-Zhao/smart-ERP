"""豁免影响面：本期豁免了谁、放过了多少行、其中多少计费行（只读）。

这个模块是「计量表」的数据源：它不修改任何快照，只把 `apply_exemptions` 前后
两份报告与本期对账行做差，回答三个客户财务一定会问的问题：

1. 本期到底豁免了哪些主体 / 规则？
2. 这些豁免让多少原本要人工的行变成了自动放行？
3. 其中多少是计费行？

输出的每条记录都带 ``subject_type / subject_code / rule_id / exemption_id /
approved_by_role / findings_count / affected_billable_lines``，API 与计量表可以
直接消费；CSV 只是同一份数据的扁平化。
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

from .exemptions import (
    EXEMPTION_LINE_RATIO_LIMIT,
    MAX_CUMULATIVE_EXEMPTIONS,
)
from .impact import ReconLine, resolve_subject_keys
from .models import (
    InspectionReport,
    MasterDataSnapshot,
    ObjectType,
    Severity,
    format_ratio,
)


@dataclass(frozen=True)
class ExemptionAlert:
    level: str
    code: str
    message: str

    def to_dict(self) -> dict[str, str]:
        return {"level": self.level, "code": self.code, "message": self.message}


@dataclass(frozen=True)
class ExemptionImpactEntry:
    exemption_id: str
    subject_type: ObjectType
    subject_code: str
    rule_id: str
    submitted_by: str
    approved_by: str
    approved_role: str
    basis_type: str
    basis_ref: str
    basis_valid_from: str
    basis_valid_to: str
    granted_at: str
    expires_at: str
    cumulative_count: int
    findings_count: int
    covered_lines: int
    freed_lines: int
    affected_billable_lines: int
    billable_conflict_lines: int
    second_reviewer: str = ""
    escalated_review_by: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "exemption_id": self.exemption_id,
            "subject_type": self.subject_type.value,
            "subject_code": self.subject_code,
            "rule_id": self.rule_id,
            "submitted_by": self.submitted_by,
            "approved_by": self.approved_by,
            "approved_by_role": self.approved_role,
            "basis_type": self.basis_type,
            "basis_ref": self.basis_ref,
            "basis_valid_from": self.basis_valid_from,
            "basis_valid_to": self.basis_valid_to,
            "granted_at": self.granted_at,
            "expires_at": self.expires_at,
            "cumulative_count": self.cumulative_count,
            "findings_count": self.findings_count,
            "covered_lines": self.covered_lines,
            "freed_lines": self.freed_lines,
            "affected_billable_lines": self.affected_billable_lines,
            "billable_conflict_lines": self.billable_conflict_lines,
            "second_reviewer": self.second_reviewer,
            "escalated_review_by": self.escalated_review_by,
        }


@dataclass(frozen=True)
class ExemptionImpact:
    as_of_period: str
    total_lines: int
    out_of_period_lines: int
    covered_lines: int
    freed_lines: int
    affected_billable_lines: int
    billable_conflict_lines: int
    billable_conflict_line_ids: tuple[str, ...]
    covered_ratio: Decimal
    freed_ratio: Decimal
    threshold: Decimal
    threshold_exceeded: bool
    cap_checked: bool
    cap_note: str
    entries: tuple[ExemptionImpactEntry, ...]
    alerts: tuple[ExemptionAlert, ...]
    conclusion: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "as_of_period": self.as_of_period,
            "total_lines": self.total_lines,
            "out_of_period_lines": self.out_of_period_lines,
            "covered_lines": self.covered_lines,
            "freed_lines": self.freed_lines,
            "affected_billable_lines": self.affected_billable_lines,
            "billable_conflict_lines": self.billable_conflict_lines,
            "billable_conflict_line_ids": list(self.billable_conflict_line_ids),
            "covered_ratio": _ratio_text(self.covered_ratio),
            "covered_ratio_pct": format_ratio(self.covered_ratio),
            "freed_ratio": _ratio_text(self.freed_ratio),
            "freed_ratio_pct": format_ratio(self.freed_ratio),
            "threshold": _ratio_text(self.threshold),
            "threshold_pct": format_ratio(self.threshold),
            "threshold_exceeded": self.threshold_exceeded,
            "exemption_cap_checked": self.cap_checked,
            "exemption_cap_note": self.cap_note,
            "entries": [item.to_dict() for item in self.entries],
            "alerts": [item.to_dict() for item in self.alerts],
            "conclusion": self.conclusion,
        }


def _ratio_text(value: Decimal) -> str:
    text = format(value.quantize(Decimal("0.0001")), "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def _in_period(report: InspectionReport, lines: list[ReconLine]) -> list[ReconLine]:
    return [
        line
        for line in lines
        if line.period is None or line.period == report.as_of_period
    ]


def _line_keys(line: ReconLine) -> tuple[tuple[ObjectType, str], ...]:
    keys: list[tuple[ObjectType, str]] = []
    if line.supplier_code:
        keys.append((ObjectType.SUPPLIER, line.supplier_code))
    if line.customer_code:
        keys.append((ObjectType.CUSTOMER, line.customer_code))
    if line.item_code:
        keys.append((ObjectType.ITEM, line.item_code))
    return tuple(keys)


def _blocked_state(
    report: InspectionReport,
    snapshot: MasterDataSnapshot | None,
) -> tuple[set[tuple[ObjectType, str]], set[tuple[ObjectType, str, str]], bool]:
    subjects: set[tuple[ObjectType, str]] = set()
    pairs: set[tuple[ObjectType, str, str]] = set()
    global_block = False
    for finding in report.findings:
        if finding.severity is not Severity.BLOCK:
            continue
        resolved = resolve_subject_keys(finding, snapshot)
        if not resolved:
            global_block = True
            continue
        for key in resolved:
            subjects.add(key)
            pairs.add((key[0], key[1], finding.rule_id))
    return subjects, pairs, global_block


def evaluate_exemption_impact(
    before: InspectionReport,
    after: InspectionReport,
    lines: list[ReconLine] | tuple[ReconLine, ...],
    snapshot: MasterDataSnapshot | None = None,
    threshold: Decimal | None = None,
) -> ExemptionImpact:
    """对比应用豁免前后的报告，计算豁免覆盖/放过/计费行。"""
    limit = threshold or EXEMPTION_LINE_RATIO_LIMIT
    in_period = _in_period(before, list(lines))
    total = len(in_period)
    before_subjects, before_pairs, before_global = _blocked_state(before, snapshot)
    after_subjects, _, after_global = _blocked_state(after, snapshot)

    exempted_pairs = {
        (item.subject_type, item.subject_code, item.rule_id): item
        for item in after.exemptions
    }
    covered_by: dict[str, set[str]] = {}
    covered: set[str] = set()
    freed: set[str] = set()
    for line in in_period:
        keys = _line_keys(line)
        for key in keys:
            for (subject_type, subject_code, rule_id), item in exempted_pairs.items():
                if key != (subject_type, subject_code):
                    continue
                if (subject_type, subject_code, rule_id) not in before_pairs:
                    continue
                covered.add(line.line_id)
                covered_by.setdefault(item.exemption_id, set()).add(line.line_id)
        was_blocked = before_global or any(key in before_subjects for key in keys)
        still_blocked = after_global or any(key in after_subjects for key in keys)
        if was_blocked and not still_blocked:
            freed.add(line.line_id)

    billable_freed = {line.line_id for line in in_period if line.billable} & freed
    # 计费标记与放行状态矛盾：标记为不计费的行却被豁免自动放过。
    billable_conflicts = {
        line.line_id for line in in_period if not line.billable
    } & freed
    covered_ratio = _safe_ratio(len(covered), total)
    freed_ratio = _safe_ratio(len(freed), total)

    entries = tuple(
        ExemptionImpactEntry(
            exemption_id=item.exemption_id,
            subject_type=item.subject_type,
            subject_code=item.subject_code,
            rule_id=item.rule_id,
            submitted_by=item.submitted_by,
            approved_by=item.approved_by,
            approved_role=item.approved_role,
            basis_type=item.basis_type,
            basis_ref=item.basis_ref,
            basis_valid_from=item.basis_valid_from,
            basis_valid_to=item.basis_valid_to,
            granted_at=item.granted_at,
            expires_at=item.expires_at,
            cumulative_count=item.cumulative_count,
            findings_count=len(item.findings),
            covered_lines=len(covered_by.get(item.exemption_id, ())),
            freed_lines=len(covered_by.get(item.exemption_id, set()) & freed),
            affected_billable_lines=len(
                covered_by.get(item.exemption_id, set()) & billable_freed
            ),
            billable_conflict_lines=len(
                covered_by.get(item.exemption_id, set()) & billable_conflicts
            ),
            second_reviewer=item.second_reviewer,
            escalated_review_by=item.escalated_review_by,
        )
        for item in after.exemptions
    )

    alerts: list[ExemptionAlert] = []
    if billable_conflicts:
        alerts.append(
            ExemptionAlert(
                "告警",
                "BILLABLE_FLAG_CONFLICT",
                (
                    f"{len(billable_conflicts)} 行标记为不计费却被豁免自动放过"
                    f"（{', '.join(sorted(billable_conflicts))}）："
                    "计费标记与放行状态矛盾，请先核对 billable 标注，"
                    "不得静默按任一边处理"
                ),
            )
        )
    if covered_ratio > limit:
        alerts.append(
            ExemptionAlert(
                "告警",
                "EXEMPTION_LINE_RATIO_EXCEEDED",
                (
                    f"单批豁免影响行占比 {format_ratio(covered_ratio)} 超过上限 "
                    f"{format_ratio(limit)}，已要求二人复核；请核对是否集中豁免了"
                    "高频主体"
                ),
            )
        )
    for entry in entries:
        if entry.cumulative_count > MAX_CUMULATIVE_EXEMPTIONS:
            alerts.append(
                ExemptionAlert(
                    "告警",
                    "EXEMPTION_RENEWAL_EXCEEDED",
                    (
                        f"豁免 {entry.exemption_id} 是同一主体同规则第 "
                        f"{entry.cumulative_count} 次豁免，超过 "
                        f"{MAX_CUMULATIVE_EXEMPTIONS} 次，已要求升级复核"
                        f"（{entry.escalated_review_by or '未填写'}）"
                    ),
                )
            )
    conclusion = (
        f"本期 {total} 行，豁免覆盖 {len(covered)} 行"
        f"（{format_ratio(covered_ratio)}），放过 {len(freed)} 行"
        f"（{format_ratio(freed_ratio)}），其中计费行 {len(billable_freed)} 行"
    )
    if billable_conflicts:
        conclusion += (
            f"；另有 {len(billable_conflicts)} 行标记不计费却被放过，"
            "计费标记冲突待人工核对"
        )
    return ExemptionImpact(
        as_of_period=before.as_of_period,
        total_lines=total,
        out_of_period_lines=len(lines) - total,
        covered_lines=len(covered),
        freed_lines=len(freed),
        affected_billable_lines=len(billable_freed),
        billable_conflict_lines=len(billable_conflicts),
        billable_conflict_line_ids=tuple(sorted(billable_conflicts)),
        covered_ratio=covered_ratio,
        freed_ratio=freed_ratio,
        threshold=limit,
        threshold_exceeded=covered_ratio > limit,
        cap_checked=True,
        cap_note="已校验：整批豁免拒绝 + 15% 二次复核",
        entries=entries,
        alerts=tuple(alerts),
        conclusion=conclusion,
    )


def unchecked_exemption_impact(report: InspectionReport) -> ExemptionImpact:
    """没有行级数据时的显式结果：所有行数未知，上限未校验。"""
    entries = tuple(
        ExemptionImpactEntry(
            exemption_id=item.exemption_id,
            subject_type=item.subject_type,
            subject_code=item.subject_code,
            rule_id=item.rule_id,
            submitted_by=item.submitted_by,
            approved_by=item.approved_by,
            approved_role=item.approved_role,
            basis_type=item.basis_type,
            basis_ref=item.basis_ref,
            basis_valid_from=item.basis_valid_from,
            basis_valid_to=item.basis_valid_to,
            granted_at=item.granted_at,
            expires_at=item.expires_at,
            cumulative_count=item.cumulative_count,
            findings_count=len(item.findings),
            covered_lines=0,
            freed_lines=0,
            affected_billable_lines=0,
            billable_conflict_lines=0,
            second_reviewer=item.second_reviewer,
            escalated_review_by=item.escalated_review_by,
        )
        for item in report.exemptions
    )
    alerts = [
        ExemptionAlert(
            "告警",
            "EXEMPTION_CAP_UNCHECKED",
            "未提供行级数据 → 豁免影响面上限未校验"
            "（15% 二次复核与整批豁免判定均未执行）",
        )
    ]
    for entry in entries:
        if entry.cumulative_count > MAX_CUMULATIVE_EXEMPTIONS:
            alerts.append(
                ExemptionAlert(
                    "告警",
                    "EXEMPTION_RENEWAL_EXCEEDED",
                    (
                        f"豁免 {entry.exemption_id} 是同一主体同规则第 "
                        f"{entry.cumulative_count} 次豁免，超过 "
                        f"{MAX_CUMULATIVE_EXEMPTIONS} 次，已要求升级复核"
                    ),
                )
            )
    return ExemptionImpact(
        as_of_period=report.as_of_period,
        total_lines=0,
        out_of_period_lines=0,
        covered_lines=0,
        freed_lines=0,
        affected_billable_lines=0,
        billable_conflict_lines=0,
        billable_conflict_line_ids=(),
        covered_ratio=Decimal(0),
        freed_ratio=Decimal(0),
        threshold=EXEMPTION_LINE_RATIO_LIMIT,
        threshold_exceeded=False,
        cap_checked=False,
        cap_note=(
            "未提供行级数据 → 豁免影响面上限未校验"
            "（15% 二次复核与整批豁免判定均未执行）"
        ),
        entries=entries,
        alerts=tuple(alerts),
        conclusion="未提供行级数据，豁免放过行数与计费行影响无法评估",
    )


def _safe_ratio(value: int, total: int) -> Decimal:
    if total <= 0:
        return Decimal(0)
    return Decimal(value) / Decimal(total)


_CSV_HEADERS = (
    "豁免编号",
    "主体类型",
    "主体编码",
    "规则ID",
    "提单人",
    "批准角色",
    "批准人",
    "依据类型",
    "依据编号",
    "生效日",
    "到期日",
    "累计次数",
    "命中差异数",
    "覆盖行数",
    "放过行数",
    "影响计费行数",
    "计费冲突行数",
    "二次复核人",
    "升级复核人",
    "影响面上限已校验",
)


def render_exemption_impact_csv(impact: ExemptionImpact) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(_CSV_HEADERS)
    for entry in impact.entries:
        writer.writerow(
            (
                entry.exemption_id,
                entry.subject_type.value,
                entry.subject_code,
                entry.rule_id,
                entry.submitted_by,
                entry.approved_role,
                entry.approved_by,
                entry.basis_type,
                entry.basis_ref,
                entry.granted_at,
                entry.expires_at,
                entry.cumulative_count,
                entry.findings_count,
                entry.covered_lines,
                entry.freed_lines,
                entry.affected_billable_lines,
                entry.billable_conflict_lines,
                entry.second_reviewer,
                entry.escalated_review_by,
                "是" if impact.cap_checked else "否",
            )
        )
    if not impact.cap_checked:
        writer.writerow(
            (
                "—",
                "—",
                "—",
                "—",
                "—",
                "—",
                "—",
                "—",
                "—",
                "—",
                "—",
                "—",
                "—",
                "未校验",
                "未校验",
                "未校验",
                "未校验",
                "—",
                "—",
                "否",
            )
        )
    return buffer.getvalue()


def write_exemption_impact_csv(impact: ExemptionImpact, path: str | Path) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(render_exemption_impact_csv(impact), encoding="utf-8")
    return target
