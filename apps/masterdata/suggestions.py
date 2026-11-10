"""规则收敛建议：告诉客户「先修哪 5 条最划算」（只读）。

排序依据用户给定的三因子：命中量 × 修复难度 × 修复后对自动放行的贡献。
实现上把「修复难度」当作成本项（越大越靠后），即：

    优先级得分 = 命中量（阻断差异数） × 独占主体数 ÷ 修复难度（1~5）

「独占主体」= 只被这一条规则阻断的主体；修完它这些主体就能自动放行，
不会被其他规则继续拦住。行级贡献（受影响行、独占行）同时列出，避免只看条数。
行级数据可选：不传行时仍可按主体给出建议，只是行贡献为空。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from .impact import ReconLine, resolve_subject_keys
from .models import (
    InspectionReport,
    MasterDataSnapshot,
    ObjectType,
    Severity,
)

PRIORITY_FORMULA = (
    "优先级得分 = 阻断差异数 × 独占主体数 ÷ 修复难度（1~5）；"
    "独占主体指只被该规则阻断、修完即可放行的供应商/客户/物料"
)

# 修复难度：1=配置补录，2=单条主数据修正，3=需业务/税务确认，
# 4=需清理历史单据，5=需合并主体或改编码。
RULE_DIFFICULTY: dict[str, int] = {
    "UOM-001": 3,
    "UOM-002": 2,
    "UOM-003": 3,
    "UOM-004": 1,
    "UOM-005": 3,
    "MISS-001": 2,
    "MISS-002": 2,
    "MISS-003": 1,
    "DUP-001": 5,
    "DUP-002": 4,
    "DUP-003": 5,
    "CODE-001": 5,
    "PERIOD-001": 2,
    "PERIOD-002": 2,
    "LEDGER-001": 4,
    "CUR-001": 2,
    "CUR-002": 2,
    "CUR-003": 1,
    "TAX-001": 2,
    "TAX-002": 3,
    "PAY-001": 1,
    "PAY-002": 2,
    "CUST-001": 5,
    "CUST-002": 4,
}

DIFFICULTY_LABELS: dict[int, str] = {
    1: "配置补录",
    2: "单条主数据修正",
    3: "需业务/税务确认",
    4: "需清理历史单据",
    5: "需合并主体或改编码",
}


@dataclass(frozen=True)
class FixPriority:
    rule_id: str
    rule_name: str
    category: str
    blocked_findings: int
    blocked_subjects: int
    exclusive_subjects: int
    affected_lines: int
    exclusive_lines: int
    difficulty: int
    difficulty_label: str
    score: Decimal
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "rule_name": self.rule_name,
            "category": self.category,
            "blocked_findings": self.blocked_findings,
            "blocked_subjects": self.blocked_subjects,
            "exclusive_subjects": self.exclusive_subjects,
            "affected_lines": self.affected_lines,
            "exclusive_lines": self.exclusive_lines,
            "difficulty": self.difficulty,
            "difficulty_label": self.difficulty_label,
            "score": _score_text(self.score),
            "reason": self.reason,
        }


def _score_text(value: Decimal) -> str:
    text = format(value.quantize(Decimal("0.001")), "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def _line_rule_hits(
    report: InspectionReport,
    lines: tuple[ReconLine, ...] | list[ReconLine],
    subject_rules: dict[tuple[ObjectType, str], set[str]],
) -> tuple[dict[str, set[str]], set[str]]:
    """返回「规则 → 影响行」与「修完该规则即可放行的独占行」。"""
    in_period = [
        line
        for line in lines
        if line.period is None or line.period == report.as_of_period
    ]
    line_rules: dict[str, set[str]] = {}
    exclusive: set[str] = set()
    for line in in_period:
        keys = []
        if line.supplier_code:
            keys.append((ObjectType.SUPPLIER, line.supplier_code))
        if line.customer_code:
            keys.append((ObjectType.CUSTOMER, line.customer_code))
        if line.item_code:
            keys.append((ObjectType.ITEM, line.item_code))
        rules: set[str] = set()
        for key in keys:
            rules.update(subject_rules.get(key, set()))
        if not rules:
            continue
        if len(rules) == 1:
            exclusive.add(line.line_id)
        for rule_id in rules:
            line_rules.setdefault(rule_id, set()).add(line.line_id)
    return line_rules, exclusive


def build_fix_priorities(
    report: InspectionReport,
    lines: tuple[ReconLine, ...] | list[ReconLine] = (),
    *,
    snapshot: MasterDataSnapshot | None = None,
    top: int = 5,
) -> tuple[FixPriority, ...]:
    """按用户给定的三因子排序，返回最值得先修的规则清单（默认前 5）。"""
    blocked = [item for item in report.findings if item.severity is Severity.BLOCK]
    if not blocked:
        return ()

    subject_rules: dict[tuple[ObjectType, str], set[str]] = {}
    findings_by_rule: dict[str, int] = {}
    rule_meta = {}
    for finding in blocked:
        findings_by_rule[finding.rule_id] = (
            findings_by_rule.get(finding.rule_id, 0) + 1
        )
        rule_meta[finding.rule_id] = finding
        for key in resolve_subject_keys(finding, snapshot):
            subject_rules.setdefault(key, set()).add(finding.rule_id)

    subjects_by_rule: dict[str, set[tuple[ObjectType, str]]] = {}
    for key, rule_ids in subject_rules.items():
        for rule_id in rule_ids:
            subjects_by_rule.setdefault(rule_id, set()).add(key)

    line_rules, exclusive_lines = _line_rule_hits(report, lines, subject_rules)
    rule_by_id = {rule.rule_id: rule for rule in report.rules}

    priorities: list[FixPriority] = []
    for rule_id, findings_count in findings_by_rule.items():
        subjects = subjects_by_rule.get(rule_id, set())
        exclusive_subjects = sum(
            1 for key in subjects if subject_rules[key] == {rule_id}
        )
        difficulty = RULE_DIFFICULTY.get(rule_id, 3)
        label = DIFFICULTY_LABELS.get(difficulty, "需业务确认")
        score = (
            Decimal(findings_count)
            * Decimal(max(exclusive_subjects, 0))
            / Decimal(difficulty)
        )
        affected = len(line_rules.get(rule_id, set()))
        freed = len(
            line_rules.get(rule_id, set()) & exclusive_lines
        )
        spec = rule_by_id.get(rule_id)
        meta = rule_meta[rule_id]
        why = spec.why if spec is not None else meta.rule_name
        if len(why) > 150:
            why = why[:147] + "…"
        reason = (
            f"命中 {findings_count} 条阻断差异、{len(subjects)} 个主体，"
            f"其中 {exclusive_subjects} 个主体只被本规则阻断；"
            f"样例行中影响 {affected} 行、修完可独占放行 {freed} 行；"
            f"修复难度 {difficulty}/5（{label}）。{why}"
        )
        priorities.append(
            FixPriority(
                rule_id=rule_id,
                rule_name=meta.rule_name,
                category=meta.category,
                blocked_findings=findings_count,
                blocked_subjects=len(subjects),
                exclusive_subjects=exclusive_subjects,
                affected_lines=affected,
                exclusive_lines=freed,
                difficulty=difficulty,
                difficulty_label=label,
                score=score,
                reason=reason,
            )
        )

    priorities.sort(
        key=lambda item: (
            -item.score,
            -item.blocked_findings,
            item.rule_id,
        )
    )
    limit = max(int(top), 0)
    return tuple(priorities[:limit])


def priorities_payload(
    priorities: tuple[FixPriority, ...],
) -> dict[str, Any]:
    return {
        "formula": PRIORITY_FORMULA,
        "difficulty_scale": DIFFICULTY_LABELS,
        "items": [item.to_dict() for item in priorities],
    }
