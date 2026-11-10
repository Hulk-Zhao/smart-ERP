"""命令行入口：只读体检 + 差异清单导出 + 阻断影响面 / 降级清单 + 豁免与收敛建议。

示例：
    python -m apps.masterdata --list-rules
    python -m apps.masterdata -i fixtures/masterdata/dirty.json --csv out.csv
    python -m apps.masterdata -i fixtures/masterdata/dirty.json --supplier SUP-001
    python -m apps.masterdata -i fixtures/masterdata/dirty.json \
        --lines fixtures/masterdata/recon_lines_sample.json \
        --exemptions fixtures/masterdata/exemptions_sample.json \
        --downgrades dist/masterdata_downgrades.json
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path

from .engine import inspect
from .exemption_impact import (
    ExemptionImpact,
    evaluate_exemption_impact,
    unchecked_exemption_impact,
    write_exemption_impact_csv,
)
from .exemptions import (
    EXEMPTION_LINE_RATIO_LIMIT,
    MAX_CUMULATIVE_EXEMPTIONS,
    ExemptionInvalid,
    ExemptionPolicyViolation,
    apply_exemptions,
    load_exemptions,
    migrate_v1_to_v2,
)
from .export import write_csv
from .impact import (
    BlockImpact,
    evaluate_block_impact,
    export_downgrade_list,
    load_recon_lines,
)
from .loader import load_snapshot
from .models import (
    BLOCK_SEMANTICS,
    FindingFilter,
    InspectionOptions,
    InspectionReport,
    Severity,
    format_ratio,
)
from .rules import RULES, RULES_BY_ID
from .simulate import SAMPLE_CAVEAT, SIMULATION_NOTE, dirt_tiers_payload
from .suggestions import (
    PRIORITY_FORMULA,
    FixPriority,
    build_fix_priorities,
    priorities_payload,
)

_SEVERITY_ALIASES: dict[str, Severity] = {
    "阻断": Severity.BLOCK,
    "block": Severity.BLOCK,
    "提示": Severity.WARN,
    "warn": Severity.WARN,
    "warning": Severity.WARN,
}


def _parse_severity_overrides(
    parser: argparse.ArgumentParser, values: list[str] | None
) -> tuple[tuple[str, Severity], ...]:
    """解析 ``--severity RULE_ID=阻断|提示``，同一规则后写覆盖前写。"""
    overrides: dict[str, Severity] = {}
    for raw in values or []:
        rule_id, sep, label = raw.partition("=")
        rule_id = rule_id.strip()
        label = label.strip().lower()
        if not sep or not rule_id or not label:
            parser.error(f"--severity 需要 RULE_ID=阻断|提示，收到 {raw!r}")
        if rule_id not in RULES_BY_ID:
            parser.error(
                f"--severity 未知规则ID {rule_id!r}，可用 --list-rules 查看"
            )
        severity = _SEVERITY_ALIASES.get(label)
        if severity is None:
            parser.error(f"--severity 严重度只能是 阻断/提示，收到 {label!r}")
        overrides[rule_id] = severity
    return tuple(overrides.items())


def _parse_threshold(
    parser: argparse.ArgumentParser, raw: str
) -> Decimal:
    """解析 ``0.30`` 或 ``30%`` 形式的降级行占比阈值。"""
    text = raw.strip()
    try:
        value = (
            Decimal(text[:-1]) / Decimal(100)
            if text.endswith("%")
            else Decimal(text)
        )
    except InvalidOperation:
        parser.error(f"--block-threshold 需要 0~1 的小数或百分比，收到 {raw!r}")
    if not Decimal(0) <= value <= Decimal(1):
        parser.error(f"--block-threshold 必须在 0~1 之间，收到 {raw!r}")
    return value


def _parse_as_of(parser: argparse.ArgumentParser, raw: str | None) -> date:
    if not raw:
        return date.today()
    try:
        return date.fromisoformat(raw)
    except ValueError:
        parser.error(f"--as-of 需要 YYYY-MM-DD，收到 {raw!r}")
        raise  # 不可达，保持类型检查


def _print_catalog(options: InspectionOptions) -> None:
    print("规则目录（只读体检）：")
    print(f"阻断语义: {BLOCK_SEMANTICS}")
    for rule in RULES:
        effective = options.severity_of(rule.rule_id, rule.default_severity)
        overridden = effective is not rule.default_severity
        suffix = "（配置覆盖）" if overridden else ""
        print(f"- {rule.rule_id} {rule.name} [{effective.value}]{suffix}")
        if overridden:
            print(f"  规则默认: {rule.default_severity.value}")
        print(f"  为什么值得修: {rule.why}")
        print(f"  误报处理: {rule.false_positive}")


def _print_impact(impact: BlockImpact) -> None:
    print(f"阻断影响面: {impact.conclusion}")
    print(
        f"  本期行 {impact.total_lines}，降级行 {impact.affected_lines}，"
        f"阈值 {format_ratio(impact.threshold)}，"
        f"告警 {'已触发' if impact.triggered else '未触发'}"
    )
    if impact.out_of_period_lines:
        print(f"  非本期行（未计入）: {impact.out_of_period_lines}")
    if impact.global_downgrade:
        print(
            "  整批降级: 是（无法归属主体的阻断规则 "
            f"{', '.join(impact.unattributed_rule_ids)}）"
        )
    for alert in impact.alerts:
        print(f"  [{alert.level}] {alert.message}")
    if impact.subjects:
        print("  按主体聚合（供应商/客户/物料）:")
        for subject in impact.subjects:
            print(
                f"  - [{subject.object_type.value}] {subject.code} "
                f"{subject.name} | 规则 {', '.join(subject.blocked_rules)} "
                f"| 影响 {subject.affected_lines} 行"
            )


def _print_exemptions(report: InspectionReport, audit: dict) -> None:
    print(
        f"豁免（评估日 {audit['as_of']}）: "
        f"生效 {audit['active']}，已过期 {audit['expired']}，"
        f"未到期 {audit['pending']}，未命中 {audit['unused']}；"
        f"覆盖差异 {len(report.exemptions)} 条"
    )
    for item in report.exemptions:
        print(
            f"  - {item.exemption_id} [{item.subject_type.value}] "
            f"{item.subject_code} 规则 {item.rule_id} | 提单 "
            f"{item.submitted_by}（{item.submitted_role}）| 批准 "
            f"{item.approved_by}（{item.approved_role}）"
            f"| 依据 {item.basis_type}:{item.basis_ref} "
            f"| 到期 {item.expires_at} | 累计 {item.cumulative_count} 次"
            f"| 覆盖 {len(item.findings)} 条"
        )
        if item.second_reviewer:
            print(
                f"    二次复核: {item.second_reviewer}"
                f"（{item.second_reviewed_role}）"
            )
        if item.escalated_review_by:
            print(f"    升级复核: {item.escalated_review_by}")
    for item in audit["expired_entries"]:
        print(
            f"  - [已过期] {item['exemption_id']} [{item['subject_type']}] "
            f"{item['subject_code']} 规则 {item['rule_id']} | "
            f"到期 {item['expires_at']}，已自动失效"
        )


def _print_exemption_impact(impact: ExemptionImpact) -> None:
    print(f"豁免影响面: {impact.conclusion}")
    if not impact.cap_checked:
        print(f"  [未校验] {impact.cap_note}")
    for entry in impact.entries:
        print(
            f"  - {entry.exemption_id} [{entry.subject_type.value}] "
            f"{entry.subject_code} 规则 {entry.rule_id} | 命中差异 "
            f"{entry.findings_count} 条 | 覆盖 {entry.covered_lines} 行 / "
            f"放过 {entry.freed_lines} 行 / 计费 {entry.affected_billable_lines} 行 "
            f"| 累计 {entry.cumulative_count} 次"
        )
    for alert in impact.alerts:
        print(f"  [{alert.level}] {alert.code}: {alert.message}")


def _print_priorities(priorities: tuple[FixPriority, ...]) -> None:
    if not priorities:
        print("规则收敛建议: 当前没有阻断差异，无需收敛")
        return
    print("规则收敛建议 Top 5（先修最划算的）:")
    for index, item in enumerate(priorities, 1):
        print(
            f"  {index}. {item.rule_id} {item.rule_name} | 得分 "
            f"{item.score:.3f} | 命中 {item.blocked_findings} 条 / "
            f"{item.blocked_subjects} 主体 | 独占主体 "
            f"{item.exclusive_subjects} | 难度 {item.difficulty}/5 "
            f"{item.difficulty_label}"
        )
        print(f"     理由: {item.reason}")
    print(f"  排序公式: {PRIORITY_FORMULA}")


def _print_dirt_tiers() -> None:
    print(f"数据口径: {SAMPLE_CAVEAT}")
    print("脏数据分级模拟（合成样本，非客户实测）:")
    for tier in dirt_tiers_payload()["tiers"]:
        print(
            f"  {tier['tier']}（缺陷率 {tier['defect_rate_pct']}）→ "
            f"降级行 {tier['range_pct']}"
            f"（均匀 {tier['ratio_uniform_pct']}，"
            f"脏主体行量 1.5 倍 {tier['ratio_skewed_pct']}）"
        )
    print(f"  {SIMULATION_NOTE}")


def _print_report(
    report: InspectionReport,
    impact: BlockImpact | None = None,
    exemption_audit: dict | None = None,
    priorities: tuple[FixPriority, ...] = (),
    exemption_impact: ExemptionImpact | None = None,
) -> None:
    print(f"数据源: {report.source or '—'}")
    print(f"组织: {report.org_id or '—'}  当前期间: {report.as_of_period}")
    print(
        f"差异: 共 {len(report.findings)} 条"
        f"（阻断 {report.blocked_count}，提示 {report.warning_count}）"
    )
    metrics = report.metrics()
    print(
        f"阻断命中率: {metrics['block_hit_rate_pct']}"
        f"（{metrics['blocked_findings']}/{metrics['total_findings']} 条差异，"
        "仅用于判断阻断规则是否过严）"
    )
    print(f"阻断语义: {BLOCK_SEMANTICS}")
    if exemption_audit is not None:
        _print_exemptions(report, exemption_audit)
    if exemption_impact is not None:
        _print_exemption_impact(exemption_impact)
    if impact is not None:
        _print_impact(impact)
    elif report.findings:
        print(
            "阻断影响面: 未提供行级数据；用 --lines 传入本期对账行即可计算"
            "降级行占比与告警"
        )
    _print_dirt_tiers()
    _print_priorities(priorities)
    if not report.filters.is_empty():
        print(
            "过滤: "
            f"supplier={report.filters.supplier or '—'} "
            f"item={report.filters.item or '—'} "
            f"period={report.filters.period or '—'}"
        )
    for finding in report.findings:
        evidence = finding.evidence[0] if finding.evidence else None
        actual = evidence.actual if evidence else ""
        expected = evidence.expected if evidence else ""
        print(
            f"[{finding.severity.value}] {finding.rule_id} {finding.rule_name} | "
            f"{finding.object_type.value} {finding.object_code} "
            f"{finding.object_name}".rstrip()
        )
        if actual or expected:
            print(f"  原值: {actual}")
            print(f"  期望: {expected}")
        if finding.suggestion:
            print(f"  建议: {finding.suggestion}")
    if report.findings:
        print("每条规则的「为什么值得修 / 误报处理」见 --list-rules。")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m apps.masterdata",
        description="S9 主数据体检：只读扫描快照，输出差异清单（可导出 CSV）。",
    )
    parser.add_argument("--input", "-i", type=Path, help="只读快照 JSON 路径")
    parser.add_argument("--csv", type=Path, help="差异清单 CSV 输出路径")
    parser.add_argument(
        "--lines",
        type=Path,
        help="本期对账行 JSON（行级数据），用于评估阻断影响面与降级行占比",
    )
    parser.add_argument(
        "--block-threshold",
        metavar="0~1|30%",
        help="降级行占比告警阈值，默认沿用配置（30%）",
    )
    parser.add_argument(
        "--downgrades",
        type=Path,
        help="导出「降级清单」JSON 的路径（只读导出，本次不接 API）",
    )
    parser.add_argument(
        "--exemptions",
        type=Path,
        help="有期限豁免配置 JSON；到期自动失效，结构性问题拒绝豁免",
    )
    parser.add_argument(
        "--as-of",
        metavar="YYYY-MM-DD",
        help="评估豁免是否生效的日期，默认今天",
    )
    parser.add_argument(
        "--exemption-csv",
        type=Path,
        help="计量就绪的豁免影响面 CSV 输出路径（需同时提供 --exemptions）",
    )
    parser.add_argument(
        "--migrate-exemptions",
        type=Path,
        metavar="V1_JSON",
        help="把 schema 1.0 豁免迁移为 2.0；需配合 --roles 与 --out",
    )
    parser.add_argument(
        "--roles",
        type=Path,
        help="迁移时手工补齐的角色/客户确认 JSON：{exemption_id: {...}}",
    )
    parser.add_argument(
        "--out",
        type=Path,
        help="迁移结果输出路径",
    )
    parser.add_argument("--supplier", help="按供应商编码过滤")
    parser.add_argument("--item", help="按物料编码过滤")
    parser.add_argument("--period", help="按期间过滤（主数据级问题始终保留）")
    parser.add_argument(
        "--severity",
        action="append",
        metavar="RULE_ID=阻断|提示",
        help="覆盖规则严重度（可重复），未列出的规则沿用规则目录声明",
    )
    parser.add_argument(
        "--list-rules",
        action="store_true",
        help="列出规则目录、生效严重度、为什么值得修与误报处理",
    )
    parser.add_argument(
        "--format",
        choices=("text", "json"),
        default="text",
        help="标准输出格式，默认 text",
    )
    parser.add_argument(
        "--fail-on-block",
        action="store_true",
        help="存在阻断级差异时以退出码 2 结束，便于 CI/巡检使用",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    threshold = (
        _parse_threshold(parser, args.block_threshold)
        if args.block_threshold
        else None
    )
    options = InspectionOptions(
        severity_overrides=_parse_severity_overrides(parser, args.severity),
        block_line_ratio_threshold=(
            threshold if threshold is not None else Decimal("0.30")
        ),
    )

    if args.list_rules:
        _print_catalog(options)
        return 0

    if args.migrate_exemptions is not None:
        if args.roles is None or args.out is None:
            parser.error("--migrate-exemptions 需要同时提供 --roles 与 --out")
        try:
            raw = json.loads(args.migrate_exemptions.read_text(encoding="utf-8"))
            mappings = json.loads(args.roles.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            parser.error(f"无法读取迁移输入: {exc}")
        try:
            migrated = migrate_v1_to_v2(raw, mappings)
        except ExemptionInvalid as exc:
            parser.error(str(exc))
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(
            json.dumps(migrated, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"豁免已迁移到 schema 2.0: {args.out}", file=sys.stderr)
        return 0

    if args.input is None:
        parser.error("需要 --input（除非只查看 --list-rules 或 --migrate-exemptions）")

    snapshot = load_snapshot(args.input)
    base_report = inspect(
        snapshot,
        options=options,
        filters=FindingFilter(
            supplier=args.supplier,
            item=args.item,
            period=args.period,
        ),
    )

    lines = None
    if args.lines is not None:
        try:
            lines = load_recon_lines(args.lines)
        except (OSError, ValueError, TypeError) as exc:
            parser.error(f"无法读取对账行 {args.lines}: {exc}")

    report = base_report
    exemption_audit = None
    exemption_impact = None
    if args.exemptions is not None:
        try:
            registry = load_exemptions(args.exemptions)
        except ExemptionInvalid as exc:
            parser.error(str(exc))
        as_of = _parse_as_of(parser, args.as_of)
        try:
            report = apply_exemptions(
                report, registry, snapshot, at=as_of, lines=lines
            )
        except (ExemptionInvalid, ExemptionPolicyViolation) as exc:
            parser.error(str(exc))
        applied_ids = {item.exemption_id for item in report.exemptions}
        active_entries = registry.active(as_of)
        exemption_audit = {
            "as_of": as_of.isoformat(),
            "source": registry.source,
            "active": len(active_entries),
            "expired": len(registry.expired(as_of)),
            "pending": len(registry.pending(as_of)),
            "unused": sum(
                1
                for item in active_entries
                if item.exemption_id not in applied_ids
            ),
            "applied": [item.to_dict() for item in report.exemptions],
            "expired_entries": [
                item.to_dict() for item in registry.expired(as_of)
            ],
            "exemption_cap_checked": report.exemption_cap_checked,
            "exemption_cap_note": report.exemption_cap_note,
            "line_ratio_limit": str(EXEMPTION_LINE_RATIO_LIMIT),
            "max_cumulative_count": MAX_CUMULATIVE_EXEMPTIONS,
        }
        exemption_impact = (
            evaluate_exemption_impact(base_report, report, lines, snapshot)
            if lines is not None
            else unchecked_exemption_impact(report)
        )

    impact = (
        evaluate_block_impact(report, lines, snapshot=snapshot)
        if lines is not None
        else None
    )
    priorities = build_fix_priorities(
        report, lines or (), snapshot=snapshot, top=5
    )

    if args.csv:
        target = write_csv(report, args.csv)
        # 机器可读模式下 stdout 只留 JSON，导出提示走 stderr
        print(
            f"CSV 已导出: {target}（{len(report.findings)} 条）",
            file=sys.stderr,
        )

    if args.downgrades:
        target = export_downgrade_list(report, args.downgrades, snapshot)
        print(f"降级清单已导出: {target}", file=sys.stderr)

    if args.exemption_csv:
        if exemption_impact is None:
            parser.error("--exemption-csv 需要同时提供 --exemptions")
        target = write_exemption_impact_csv(exemption_impact, args.exemption_csv)
        print(f"豁免影响面 CSV 已导出: {target}", file=sys.stderr)

    if args.format == "json":
        payload = report.to_dict()
        if impact is not None:
            payload["impact"] = impact.to_dict()
        if exemption_audit is not None:
            payload["exemption_audit"] = exemption_audit
        if exemption_impact is not None:
            payload["exemption_impact"] = exemption_impact.to_dict()
        payload["suggestions"] = priorities_payload(priorities)
        payload["dirt_tiers"] = dirt_tiers_payload()
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        _print_report(
            report, impact, exemption_audit, priorities, exemption_impact
        )

    if args.fail_on_block and report.blocked_count:
        return 2
    return 0
