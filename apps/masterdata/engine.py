"""体检执行器：按规则目录跑一遍只读快照，输出可过滤的差异清单。"""

from __future__ import annotations

from dataclasses import replace

from .models import (
    Finding,
    FindingFilter,
    InspectionOptions,
    InspectionReport,
    MasterDataSnapshot,
    Severity,
)
from .rules import RULES, RULES_BY_ID

_SEVERITY_ORDER = {Severity.BLOCK: 0, Severity.WARN: 1}


def _validate_overrides(options: InspectionOptions) -> None:
    """配置里出现未知规则 ID 时直接报错，避免覆盖静默失效。"""
    unknown = sorted(
        {rule_id for rule_id, _ in options.severity_overrides}
        - set(RULES_BY_ID)
    )
    if unknown:
        raise ValueError(f"未知规则ID: {', '.join(unknown)}")


def _apply_severity_override(
    finding: Finding, options: InspectionOptions
) -> Finding:
    """规则只负责产生默认严重度；配置覆盖统一在这里生效。"""
    effective = options.severity_of(finding.rule_id, finding.severity)
    if effective is finding.severity:
        return finding
    return replace(finding, severity=effective)


def _dedupe(findings: list[Finding]) -> list[Finding]:
    seen: set[tuple] = set()
    result: list[Finding] = []
    for finding in findings:
        key = (
            finding.rule_id,
            finding.object_type,
            finding.object_code,
            finding.period,
            tuple(
                (item.field, item.actual, item.expected, item.source)
                for item in finding.evidence
            ),
        )
        if key in seen:
            continue
        seen.add(key)
        result.append(finding)
    return result


def inspect(
    snapshot: MasterDataSnapshot,
    options: InspectionOptions | None = None,
    filters: FindingFilter | None = None,
) -> InspectionReport:
    """执行全部规则；只读，不修改快照，也不写任何外部系统。

    严重度先由规则目录声明、再被 ``options.severity_overrides`` 覆盖；
    覆盖只改变处置档位（阻断 = 涉及行强制转人工），不改变数据加载与清单输出。
    """
    options = options or InspectionOptions()
    _validate_overrides(options)
    findings: list[Finding] = []
    for spec in RULES:
        findings.extend(spec.check(snapshot, options, spec))

    findings = [
        _apply_severity_override(finding, options) for finding in _dedupe(findings)
    ]
    findings.sort(
        key=lambda item: (
            _SEVERITY_ORDER[item.severity],
            item.rule_id,
            item.object_type.value,
            item.object_code,
            item.period or "",
        )
    )
    report = InspectionReport(
        source=snapshot.source,
        org_id=snapshot.org_id,
        as_of_period=snapshot.as_of_period,
        findings=tuple(findings),
        rules=RULES,
        options=options,
    )
    if filters is not None:
        return report.apply_filter(filters)
    return report
