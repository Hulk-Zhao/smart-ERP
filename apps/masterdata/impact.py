"""阻断影响面评估与「降级清单」对外导出（只读，不接 API、不写回）。

阻断语义固定为「该主体涉及的对账行强制降级为待人工」，所以只给差异条数不够，
必须先回答两个问题：

1. 这一期到底有多少行会被降级——按主体（供应商/客户/物料）聚合阻断差异，
   再用行级数据算出每主体影响行数与整体降级行占比；超过阈值时输出告警项。
2. 对账引擎按什么清单降级——导出稳定的 JSON「降级清单」，并提供读取函数；
   读不到或格式非法必须 fail-closed（整批转人工），不能把空清单当成无降级。

本模块只做只读计算与本地 JSON 导出：不连接 ERP、不写回、不在本次接进 API。
API 侧接入方式见 ``apps/masterdata/DEGRADATION_API.md``。
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

from .models import (
    BLOCK_SEMANTICS,
    Finding,
    InspectionReport,
    MasterDataSnapshot,
    ObjectType,
    Severity,
    format_ratio,
)
from .normalize import normalize_tax_code

SCHEMA_VERSION = "1.0"
KIND = "masterdata_downgrade_list"
_SUBJECT_TYPES = (ObjectType.SUPPLIER, ObjectType.CUSTOMER, ObjectType.ITEM)


class DowngradeListUnavailable(RuntimeError):
    """降级清单不可读、格式非法或 schema 不兼容。

    对账引擎必须按 fail-closed 处理：捕获后把整批转人工。
    绝不能把「读不到」当成「没有需要降级的主体」。
    """


@dataclass(frozen=True)
class ReconLine:
    """一条对账行，只保留定位主体与期间所需的字段（只读视图）。"""

    line_id: str
    supplier_code: str | None = None
    customer_code: str | None = None
    item_code: str | None = None
    period: str | None = None
    # 必须显式标注是否计费：少算一行可以补，多算一行会伤客户信任。
    # 不提供默认值，调用方漏传直接 TypeError。
    billable: bool = field(kw_only=True)

    def to_dict(self) -> dict[str, Any]:
        return {
            "line_id": self.line_id,
            "supplier_code": self.supplier_code,
            "customer_code": self.customer_code,
            "item_code": self.item_code,
            "period": self.period,
            "billable": self.billable,
        }


def recon_lines_from_dict(raw: Any) -> tuple[ReconLine, ...]:
    """把 ``{"lines": [...]}`` 或裸数组转成行级只读视图。"""
    entries = raw.get("lines") if isinstance(raw, Mapping) else raw
    if entries is None:
        entries = []
    if not isinstance(entries, list):
        raise TypeError("对账行数据必须是数组，或 {\"lines\": [...]}")
    lines: list[ReconLine] = []
    for index, entry in enumerate(entries):
        if not isinstance(entry, Mapping):
            raise TypeError(f"对账行第 {index + 1} 项不是对象")
        line_id = entry.get("line_id") or entry.get("id")
        if not line_id:
            raise ValueError(f"对账行第 {index + 1} 项缺少 line_id")
        if "billable" not in entry:
            raise TypeError(
                f"对账行 {line_id} 缺少 billable：必须显式标注是否计费，"
                "不得默认计入"
            )
        billable = entry["billable"]
        if not isinstance(billable, bool):
            raise TypeError(
                f"对账行 {line_id} 的 billable 必须是布尔值，收到 {billable!r}"
            )
        lines.append(
            ReconLine(
                line_id=str(line_id),
                supplier_code=entry.get("supplier_code"),
                customer_code=entry.get("customer_code"),
                item_code=entry.get("item_code"),
                period=entry.get("period"),
                billable=billable,
            )
        )
    return tuple(lines)


def load_recon_lines(path: str | Path) -> tuple[ReconLine, ...]:
    """从 JSON 夹具读取本期对账行；读取失败直接抛 ``ValueError``。"""
    target = Path(path)
    raw = json.loads(target.read_text(encoding="utf-8"))
    return recon_lines_from_dict(raw)


@dataclass(frozen=True)
class SubjectImpact:
    """一个主体被哪些阻断规则命中、以及本期影响多少条对账行。"""

    object_type: ObjectType
    code: str
    name: str
    blocked_rules: tuple[str, ...]
    reasons: tuple[str, ...]
    affected_lines: int
    affected_line_ids: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "subject_type": self.object_type.value,
            "subject_code": self.code,
            "subject_name": self.name,
            "blocked_rules": list(self.blocked_rules),
            "reasons": list(self.reasons),
            "affected_lines": self.affected_lines,
            "affected_line_ids": list(self.affected_line_ids),
        }


@dataclass(frozen=True)
class ImpactAlert:
    """超过阈值或存在无法归属主体的阻断时输出的告警项（不是裸数字）。"""

    code: str
    level: str
    message: str
    threshold: str
    actual: str

    def to_dict(self) -> dict[str, str]:
        return {
            "code": self.code,
            "level": self.level,
            "message": self.message,
            "threshold": self.threshold,
            "actual": self.actual,
        }


@dataclass(frozen=True)
class BlockImpact:
    """阻断影响面评估结果：结论句 + 按主体明细 + 告警项。"""

    as_of_period: str
    total_lines: int
    out_of_period_lines: int
    affected_lines: int
    affected_ratio: Decimal
    threshold: Decimal
    threshold_exceeded: bool
    global_downgrade: bool
    subjects: tuple[SubjectImpact, ...]
    alerts: tuple[ImpactAlert, ...]
    unattributed_rule_ids: tuple[str, ...]
    conclusion: str

    @property
    def triggered(self) -> bool:
        """是否至少有一条告警项被触发。"""
        return bool(self.alerts)

    def by_subject_type(self) -> dict[str, int]:
        counts = {object_type.value: 0 for object_type in _SUBJECT_TYPES}
        for subject in self.subjects:
            counts[subject.object_type.value] = (
                counts.get(subject.object_type.value, 0) + 1
            )
        return counts

    def to_dict(self) -> dict[str, Any]:
        return {
            "as_of_period": self.as_of_period,
            "total_lines": self.total_lines,
            "out_of_period_lines": self.out_of_period_lines,
            "affected_lines": self.affected_lines,
            "affected_ratio": _ratio_value(self.affected_ratio),
            "affected_ratio_pct": format_ratio(self.affected_ratio),
            "threshold": str(self.threshold),
            "threshold_pct": format_ratio(self.threshold),
            "threshold_exceeded": self.threshold_exceeded,
            "triggered": self.triggered,
            "global_downgrade": self.global_downgrade,
            "conclusion": self.conclusion,
            "subject_count": len(self.subjects),
            "by_subject_type": self.by_subject_type(),
            "subjects": [subject.to_dict() for subject in self.subjects],
            "alerts": [alert.to_dict() for alert in self.alerts],
            "unattributed_rule_ids": list(self.unattributed_rule_ids),
        }


def _ratio_value(value: Decimal) -> str:
    text = format(value.quantize(Decimal("0.0001")), "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def _evidence_text(finding: Finding, field: str) -> str:
    values = [
        getattr(item, field) for item in finding.evidence if getattr(item, field)
    ]
    if values:
        return "；".join(values)
    return finding.suggestion or finding.rule_name


def _subject_name(key: tuple[ObjectType, str], snapshot: MasterDataSnapshot | None) -> str:
    if snapshot is None:
        return ""
    object_type, code = key
    if object_type is ObjectType.SUPPLIER:
        entity = snapshot.supplier(code)
    elif object_type is ObjectType.CUSTOMER:
        entity = snapshot.customer(code)
    else:
        entity = snapshot.item(code)
    return entity.name if entity else ""


def resolve_subject_keys(
    finding: Finding, snapshot: MasterDataSnapshot | None
) -> tuple[tuple[ObjectType, str], ...]:
    """把一条阻断差异映射到它会影响的主体。

    - 供应商/客户/物料：对象本身就是主体；
    - 单据：按单据上的供应商、物料分别归属；
    - 税码：需要快照反查引用了该税码的供应商/客户/物料；
    - 单位换算等：按差异上携带的 ``item_code``/``supplier_code`` 归属；
    - 其余（期间、账套主数据级等）：无法归属，交由调用方按整批处理。
    """
    if finding.object_type in _SUBJECT_TYPES:
        return ((finding.object_type, finding.object_code),)
    if finding.object_type is ObjectType.DOCUMENT:
        keys: list[tuple[ObjectType, str]] = []
        if finding.supplier_code:
            keys.append((ObjectType.SUPPLIER, finding.supplier_code))
        if finding.item_code:
            keys.append((ObjectType.ITEM, finding.item_code))
        return tuple(keys)
    if finding.object_type is ObjectType.TAX_CODE and snapshot is not None:
        target = normalize_tax_code(finding.object_code)
        keys = []
        for supplier in snapshot.suppliers:
            if normalize_tax_code(supplier.tax_code) == target:
                keys.append((ObjectType.SUPPLIER, supplier.code))
        for customer in snapshot.customers:
            if normalize_tax_code(customer.tax_code) == target:
                keys.append((ObjectType.CUSTOMER, customer.code))
        for item in snapshot.items:
            if normalize_tax_code(item.tax_code) == target:
                keys.append((ObjectType.ITEM, item.code))
        return tuple(keys)
    keys = []
    if finding.supplier_code:
        keys.append((ObjectType.SUPPLIER, finding.supplier_code))
    if finding.item_code:
        keys.append((ObjectType.ITEM, finding.item_code))
    return tuple(keys)


def _line_subject_keys(line: ReconLine) -> tuple[tuple[ObjectType, str], ...]:
    keys: list[tuple[ObjectType, str]] = []
    if line.supplier_code:
        keys.append((ObjectType.SUPPLIER, line.supplier_code))
    if line.customer_code:
        keys.append((ObjectType.CUSTOMER, line.customer_code))
    if line.item_code:
        keys.append((ObjectType.ITEM, line.item_code))
    return tuple(keys)


def evaluate_block_impact(
    report: InspectionReport,
    lines: tuple[ReconLine, ...] | list[ReconLine],
    *,
    threshold: Decimal | None = None,
    snapshot: MasterDataSnapshot | None = None,
) -> BlockImpact:
    """给规则结果 + 行级数据，算出本期降级行占比与按主体影响面（只读）。

    只统计本期行：``line.period`` 为空视为本期，有值则必须等于
    ``report.as_of_period``。无法归属到主体的阻断按 fail-closed 保守处理，
    视为整批降级（``global_downgrade=True``），并单独给出告警项。
    """
    effective_threshold = (
        threshold
        if threshold is not None
        else report.options.block_line_ratio_threshold
    )
    if not Decimal(0) <= effective_threshold <= Decimal(1):
        raise ValueError(f"降级行阈值必须在 0~1 之间，收到 {effective_threshold}")

    blocked = [item for item in report.findings if item.severity is Severity.BLOCK]
    registry: dict[tuple[ObjectType, str], dict[str, Any]] = {}
    unattributed_rule_ids: set[str] = set()
    for finding in blocked:
        keys = resolve_subject_keys(finding, snapshot)
        if not keys:
            unattributed_rule_ids.add(finding.rule_id)
            continue
        reason = _evidence_text(finding, "actual")
        for key in keys:
            entry = registry.setdefault(
                key,
                {
                    "name": _subject_name(key, snapshot),
                    "rules": set(),
                    "reasons": [],
                },
            )
            entry["rules"].add(finding.rule_id)
            if reason and reason not in entry["reasons"]:
                entry["reasons"].append(reason)

    in_period = [
        line
        for line in lines
        if line.period is None or line.period == report.as_of_period
    ]
    global_downgrade = bool(unattributed_rule_ids)
    affected_ids: list[str] = []
    subject_line_ids: dict[tuple[ObjectType, str], list[str]] = {
        key: [] for key in registry
    }
    for line in in_period:
        matched = [
            key for key in _line_subject_keys(line) if key in registry
        ]
        for key in matched:
            subject_line_ids[key].append(line.line_id)
        if global_downgrade or matched:
            affected_ids.append(line.line_id)

    total_lines = len(in_period)
    affected_lines = len(affected_ids)
    ratio = (
        Decimal(affected_lines) / Decimal(total_lines)
        if total_lines
        else Decimal(0)
    )

    subjects = tuple(
        sorted(
            (
                SubjectImpact(
                    object_type=key[0],
                    code=key[1],
                    name=value["name"],
                    blocked_rules=tuple(sorted(value["rules"])),
                    reasons=tuple(value["reasons"]),
                    affected_lines=len(subject_line_ids[key]),
                    affected_line_ids=tuple(sorted(subject_line_ids[key])),
                )
                for key, value in registry.items()
            ),
            key=lambda item: (
                item.object_type.value,
                item.code,
            ),
        )
    )

    alerts: list[ImpactAlert] = []
    threshold_exceeded = total_lines > 0 and ratio > effective_threshold
    if threshold_exceeded:
        alerts.append(
            ImpactAlert(
                code="BLOCK_LINE_RATIO_EXCEEDED",
                level="告警",
                message=(
                    f"当前规则下降级行占比 {format_ratio(ratio)} 超过阈值 "
                    f"{format_ratio(effective_threshold)}："
                    "阻断规则可能定得过严，需结合阻断命中率复核阻断范围，"
                    "不得直接放宽阻断"
                ),
                threshold=_ratio_value(effective_threshold),
                actual=_ratio_value(ratio),
            )
        )
    if global_downgrade:
        alerts.append(
            ImpactAlert(
                code="UNATTRIBUTED_BLOCK",
                level="告警",
                message=(
                    "存在无法归属到供应商/客户/物料的阻断差异（规则 "
                    f"{', '.join(sorted(unattributed_rule_ids))}），"
                    "按 fail-closed 整批转人工；请补齐主体映射后再恢复自动对账"
                ),
                threshold="—",
                actual=str(len(unattributed_rule_ids)),
            )
        )

    if total_lines == 0:
        conclusion = (
            "本期未提供对账行，降级行占比无法评估"
            f"（阻断主体 {len(subjects)} 个）"
        )
    else:
        conclusion = (
            f"当前规则下本期 {format_ratio(ratio)} 的行会被降级"
            f"（{affected_lines}/{total_lines} 行）"
        )
        if threshold_exceeded:
            conclusion += (
                f"；已达到告警条件（阈值 {format_ratio(effective_threshold)}）"
            )

    return BlockImpact(
        as_of_period=report.as_of_period,
        total_lines=total_lines,
        out_of_period_lines=len(lines) - total_lines,
        affected_lines=affected_lines,
        affected_ratio=ratio,
        threshold=effective_threshold,
        threshold_exceeded=threshold_exceeded,
        global_downgrade=global_downgrade,
        subjects=subjects,
        alerts=tuple(alerts),
        unattributed_rule_ids=tuple(sorted(unattributed_rule_ids)),
        conclusion=conclusion,
    )


@dataclass(frozen=True)
class DowngradeEntry:
    """降级清单的一行：一个主体被一条规则命中的可消费结果。"""

    subject_type: ObjectType
    subject_code: str
    subject_name: str
    rule_id: str
    rule_name: str
    severity: Severity
    category: str
    reason: str
    source: str
    suggestion: str
    period: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "subject_type": self.subject_type.value,
            "subject_code": self.subject_code,
            "subject_name": self.subject_name,
            "rule_id": self.rule_id,
            "rule_name": self.rule_name,
            "severity": self.severity.value,
            "category": self.category,
            "reason": self.reason,
            "source": self.source,
            "suggestion": self.suggestion,
            "period": self.period,
        }


@dataclass(frozen=True)
class DowngradeList:
    """对外导出的降级清单（只读数据，不含任何写回指令）。"""

    source: str
    org_id: str | None
    as_of_period: str
    entries: tuple[DowngradeEntry, ...]
    downgrade_all: bool = False
    unattributed_rule_ids: tuple[str, ...] = ()

    def subject_keys(self) -> tuple[tuple[ObjectType, str], ...]:
        return tuple(
            sorted(
                {(entry.subject_type, entry.subject_code) for entry in self.entries},
                key=lambda item: (item[0].value, item[1]),
            )
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "kind": KIND,
            "generated_from": {
                "source": self.source,
                "org_id": self.org_id,
                "as_of_period": self.as_of_period,
            },
            "block_semantics": BLOCK_SEMANTICS,
            "policy": {
                "on_unavailable": "fail_closed",
                "on_unavailable_detail": (
                    "读不到、解析失败或 schema 不兼容时整批转人工；"
                    "不得把空清单或上一次缓存当成无降级"
                ),
                "on_unattributed": "downgrade_all",
                "on_unattributed_detail": (
                    "存在无法归属主体的阻断时整批转人工"
                ),
            },
            "downgrade_all": self.downgrade_all,
            "unattributed_rule_ids": list(self.unattributed_rule_ids),
            "summary": {
                "subject_count": len(self.subject_keys()),
                "entry_count": len(self.entries),
            },
            "entries": [entry.to_dict() for entry in self.entries],
        }


def build_downgrade_list(
    report: InspectionReport,
    snapshot: MasterDataSnapshot | None = None,
) -> DowngradeList:
    """把阻断差异整理成稳定排序、可被对账引擎直接消费的降级清单。

    ``snapshot`` 用于把税码类阻断反查到引用它的供应商/客户/物料；不传也能导出，
    但税码类阻断会进入 ``unattributed_rule_ids`` 并置 ``downgrade_all=True``。
    """
    entries: list[DowngradeEntry] = []
    unattributed: set[str] = set()
    for finding in report.findings:
        if finding.severity is not Severity.BLOCK:
            continue
        keys = resolve_subject_keys(finding, snapshot)
        if not keys:
            unattributed.add(finding.rule_id)
            continue
        reason = _evidence_text(finding, "actual")
        source = _evidence_text(finding, "source")
        for object_type, code in keys:
            entries.append(
                DowngradeEntry(
                    subject_type=object_type,
                    subject_code=code,
                    subject_name=_subject_name((object_type, code), snapshot),
                    rule_id=finding.rule_id,
                    rule_name=finding.rule_name,
                    severity=finding.severity,
                    category=finding.category,
                    reason=reason,
                    source=source,
                    suggestion=finding.suggestion,
                    period=finding.period,
                )
            )
    entries.sort(
        key=lambda item: (
            item.subject_type.value,
            item.subject_code,
            item.rule_id,
            item.period or "",
            item.reason,
        )
    )
    return DowngradeList(
        source=report.source,
        org_id=report.org_id,
        as_of_period=report.as_of_period,
        entries=tuple(entries),
        downgrade_all=bool(unattributed),
        unattributed_rule_ids=tuple(sorted(unattributed)),
    )


def export_downgrade_list(
    report: InspectionReport,
    path: str | Path,
    snapshot: MasterDataSnapshot | None = None,
) -> Path:
    """导出降级清单 JSON（UTF-8，无 BOM，缩进 2，换行 ``\\n``）。"""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = build_downgrade_list(report, snapshot).to_dict()
    with target.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, indent=2))
        handle.write("\n")
    return target


_ENTRY_REQUIRED = (
    "subject_type",
    "subject_code",
    "rule_id",
    "severity",
    "reason",
)


def parse_downgrade_list(raw: Any) -> DowngradeList:
    """解析并校验降级清单；任何结构问题都抛 ``DowngradeListUnavailable``。"""
    if not isinstance(raw, Mapping):
        raise DowngradeListUnavailable("降级清单必须是 JSON 对象")
    if raw.get("schema_version") != SCHEMA_VERSION:
        raise DowngradeListUnavailable(
            "降级清单 schema_version 不兼容："
            f"期望 {SCHEMA_VERSION}，收到 {raw.get('schema_version')!r}"
        )
    if raw.get("kind") != KIND:
        raise DowngradeListUnavailable(
            f"降级清单 kind 不匹配：期望 {KIND}，收到 {raw.get('kind')!r}"
        )
    generated = raw.get("generated_from") or {}
    entries_raw = raw.get("entries")
    if not isinstance(entries_raw, list):
        raise DowngradeListUnavailable("降级清单缺少 entries 数组")

    entries: list[DowngradeEntry] = []
    for index, item in enumerate(entries_raw):
        if not isinstance(item, Mapping):
            raise DowngradeListUnavailable(f"entries[{index}] 不是对象")
        missing = [key for key in _ENTRY_REQUIRED if not item.get(key)]
        if missing:
            raise DowngradeListUnavailable(
                f"entries[{index}] 缺少字段: {', '.join(missing)}"
            )
        try:
            object_type = ObjectType(item["subject_type"])
            severity = Severity(item["severity"])
        except ValueError as exc:
            raise DowngradeListUnavailable(
                f"entries[{index}] 枚举值非法: {exc}"
            ) from exc
        if object_type not in _SUBJECT_TYPES:
            raise DowngradeListUnavailable(
                f"entries[{index}] 主体类型必须是供应商/客户/物料，"
                f"收到 {object_type.value}"
            )
        if severity is not Severity.BLOCK:
            raise DowngradeListUnavailable(
                f"entries[{index}] 严重度必须是阻断，收到 {severity.value}"
            )
        entries.append(
            DowngradeEntry(
                subject_type=object_type,
                subject_code=str(item["subject_code"]),
                subject_name=str(item.get("subject_name") or ""),
                rule_id=str(item["rule_id"]),
                rule_name=str(item.get("rule_name") or ""),
                severity=severity,
                category=str(item.get("category") or ""),
                reason=str(item["reason"]),
                source=str(item.get("source") or ""),
                suggestion=str(item.get("suggestion") or ""),
                period=item.get("period"),
            )
        )
    return DowngradeList(
        source=str(generated.get("source") or ""),
        org_id=generated.get("org_id"),
        as_of_period=str(generated.get("as_of_period") or ""),
        entries=tuple(entries),
        downgrade_all=bool(raw.get("downgrade_all")),
        unattributed_rule_ids=tuple(raw.get("unattributed_rule_ids") or ()),
    )


def load_downgrade_list(path: str | Path) -> DowngradeList:
    """读取降级清单；文件缺失、JSON 损坏或 schema 不符一律抛异常。

    调用方（对账引擎 / API）必须捕获 ``DowngradeListUnavailable`` 并整批转人工，
    这就是本模块约定的 fail-closed 行为。
    """
    target = Path(path)
    try:
        text = target.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise DowngradeListUnavailable(
            f"无法读取降级清单 {target}: {exc}"
        ) from exc
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        raise DowngradeListUnavailable(
            f"降级清单不是合法 JSON {target}: {exc}"
        ) from exc
    return parse_downgrade_list(raw)
