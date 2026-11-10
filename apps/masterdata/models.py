"""S9 主数据体检领域模型（纯 Python，只读，无数据库/连接器依赖）。

设计约束对齐 PRD §7.1 / §7.2 与 03-AI能力清单：
- 体检只读：模型里不存在任何写回字段或写回方法（W0）；
- 缺失不等于零：空值保持 ``None``/空串，规则据此报缺，不做填充；
- 每条差异必须能回到「原值 / 期望 / 来源」，来源精确到 JSON 路径或单据号，
  便于人工复核，也便于后续接入只读连接器时替换来源标识。
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any

# 「阻断」的唯一定义，所有输出（报告、CLI、规则目录）必须引用这里，禁止各写一套。
BLOCK_SEMANTICS = (
    "该主体涉及的对账行强制降级为待人工；数据照常加载、差异照常出清单，"
    "不拒绝加载，也不拒绝出清单"
)


def format_ratio(value: Decimal) -> str:
    """把 0~1 的比例格式化成百分比文本，例如 ``0.4`` → ``40%``。"""
    percent = (value * Decimal(100)).quantize(Decimal("0.1"))
    text = format(percent, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return f"{text or '0'}%"


def _ratio_value(value: Decimal) -> str:
    """比例的机器可读形式：保留 4 位小数并去掉尾零，例如 ``0.525``。"""
    text = format(value.quantize(Decimal("0.0001")), "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


class Severity(str, Enum):
    """严重度只有两档，避免出现「中/低」这种没人真正处理的档位。

    两档都是「数据照常加载、差异照常输出」的体检结论，区别只在后续处置：

    ``BLOCK``：该主体涉及的对账行强制降级为待人工（见 ``BLOCK_SEMANTICS``），
    不是拒绝加载、也不是拒绝出清单。
    ``WARN``：可能合理，也可能只是历史遗留；人工确认后可以继续，本工具不降级。
    """

    BLOCK = "阻断"
    WARN = "提示"

    def __str__(self) -> str:
        return self.value


class ObjectType(str, Enum):
    SUPPLIER = "供应商"
    CUSTOMER = "客户"
    ITEM = "物料"
    CONVERSION = "单位换算"
    DOCUMENT = "单据"
    PERIOD = "会计期间"
    LEDGER = "账套"
    CURRENCY = "币种汇率"
    TAX_CODE = "税码"
    PAYMENT_TERM = "付款条件"

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True)
class Evidence:
    """一条差异证据：字段 + 原值 + 期望 + 来源。"""

    field: str
    actual: str
    expected: str
    source: str

    def to_dict(self) -> dict[str, str]:
        return {
            "field": self.field,
            "actual": self.actual,
            "expected": self.expected,
            "source": self.source,
        }


@dataclass(frozen=True)
class Supplier:
    code: str
    name: str
    tax_no: str | None = None
    bank_account: str | None = None
    org_id: str | None = None
    ledger_id: str | None = None
    currency: str | None = None
    payment_term: str | None = None
    tax_code: str | None = None


@dataclass(frozen=True)
class Customer:
    code: str
    name: str
    tax_no: str | None = None
    org_id: str | None = None
    ledger_id: str | None = None
    currency: str | None = None
    payment_term: str | None = None
    tax_code: str | None = None


@dataclass(frozen=True)
class Item:
    code: str
    name: str
    base_unit: str | None = None
    spec: str | None = None
    # 同类物料分组用；为空时不参与「换算比例偏离同类」判断，避免乱分组
    category: str | None = None
    org_id: str | None = None
    ledger_id: str | None = None
    tax_code: str | None = None


@dataclass(frozen=True)
class UnitConversion:
    """1 ``from_unit`` = ``factor`` ``to_unit``。

    ``last_used_period`` 与 ``usage_count`` 由只读取数方提供；缺失表示「无使用记录」，
    不能反推为「从未使用」，因此未使用规则会结合物料最早单据期间判断。
    """

    item_code: str
    from_unit: str
    to_unit: str
    factor: Decimal
    last_used_period: str | None = None
    usage_count: int = 0
    org_id: str | None = None


@dataclass(frozen=True)
class Document:
    """单据只保留体检需要的字段，不复制业务明细。"""

    doc_no: str
    doc_type: str
    doc_period: str
    posting_period: str
    org_id: str
    ledger_id: str
    supplier_code: str | None = None
    item_code: str | None = None
    unit: str | None = None
    line_no: int = 1
    currency: str | None = None
    exchange_rate: Decimal | None = None


@dataclass(frozen=True)
class CurrencyRate:
    """某一币种在指定期间/账套对本位币的汇率，只读配置。"""

    currency: str
    period: str
    rate: Decimal
    ledger_id: str | None = None
    # 汇率最近一次维护期间；为空时退回 period，避免把「没记录」当成「很旧」
    updated_period: str | None = None


@dataclass(frozen=True)
class TaxCode:
    """税码目录：``rate`` 是开票税率，``filing_rate`` 是申报口径税率。"""

    code: str
    rate: Decimal
    filing_rate: Decimal
    name: str = ""


@dataclass(frozen=True)
class PaymentTermContract:
    """合同约定的付款条件，用来和主数据当前值比对。"""

    party_type: str  # 供应商 / 客户
    party_code: str
    payment_term: str
    contract_no: str = ""


@dataclass(frozen=True)
class Period:
    period: str
    closed: bool
    org_id: str | None = None


@dataclass(frozen=True)
class Ledger:
    ledger_id: str
    org_id: str
    name: str = ""


@dataclass(frozen=True)
class MasterDataSnapshot:
    """一次体检的只读快照。

    ``source`` 写入每条证据，便于回答「这个结论是从哪份数据看出来的」。
    """

    org_id: str | None
    as_of_period: str
    source: str = ""
    base_currency: str = "CNY"
    suppliers: tuple[Supplier, ...] = ()
    customers: tuple[Customer, ...] = ()
    items: tuple[Item, ...] = ()
    unit_conversions: tuple[UnitConversion, ...] = ()
    documents: tuple[Document, ...] = ()
    periods: tuple[Period, ...] = ()
    ledgers: tuple[Ledger, ...] = ()
    currency_rates: tuple[CurrencyRate, ...] = ()
    tax_codes: tuple[TaxCode, ...] = ()
    payment_contracts: tuple[PaymentTermContract, ...] = ()

    def supplier(self, code: str | None) -> Supplier | None:
        if not code:
            return None
        return next((item for item in self.suppliers if item.code == code), None)

    def customer(self, code: str | None) -> Customer | None:
        if not code:
            return None
        return next((item for item in self.customers if item.code == code), None)

    def item(self, code: str | None) -> Item | None:
        if not code:
            return None
        return next((item for item in self.items if item.code == code), None)

    def ledger(self, ledger_id: str | None) -> Ledger | None:
        if not ledger_id:
            return None
        return next(
            (item for item in self.ledgers if item.ledger_id == ledger_id), None
        )


@dataclass(frozen=True)
class InspectionOptions:
    """阈值集中在这里，默认值都写明了依据，避免规则里藏魔法数字。

    ``factor_outlier_ratio=1.5``：同类别包装换算通常不会相差 50% 以上，超过先提示。
    ``factor_outlier_block_ratio=3.0``：差到 3 倍以上基本是「100 写成 10」这类
    数量级错误，直接阻断自动对账。
    ``unused_conversion_months=12``：一个完整会计年度都没用过，才视为历史遗留。
    ``duplicate_name_threshold=0.92``：名称归一后仍高度相似才提示；低于该值不猜。
    ``duplicate_name_min_length=6``：短于该长度的名称不参与相似度猜测，两个字的
    简称几乎必然彼此相似，报了也没法处理。
    ``duplicate_name_window=128``：形近名称先按归一化名称排序（正序 + 倒序），
    只在左右各 128 个邻居内比对，另外补一遍「短名是长名前缀」的二分候选。
    窗口越宽召回越高、耗时越高（约 O(n·窗口)）。真实数据里若发现漏报，优先
    调大该值；1 万条供应商实测 128 可召回 99% 以上。
    ``duplicate_name_length_ratio=0.20``：长度差过滤的相对阈值。由相似度公式可
    推出必要条件 ``长度差 ≤ 短名长度 × 2(1-阈值)/阈值``（0.92 时约 0.174），默认
    0.20 留出余量，先做整数比较即可淘汰绝大多数候选，不进入 difflib。
    ``min_peer_items=3``：同类样本少于 3 个不算中位数，避免两条数据互相「证明」。
    ``stale_rate_months=6``：汇率连续两个季度未维护就提示；不直接阻断，避免正常
    稳定币种被频繁打扰。
    ``block_line_ratio_threshold=0.30``：本期被阻断主体覆盖的对账行占比超过该阈值
    时输出告警。默认 30% 只是起点——超过说明阻断规则可能把整批都吃掉了，
    应结合「阻断命中率」一起复核规则是否过严，而不是直接放宽阻断。

    ``severity_overrides``：严重度是「规则目录声明 + 配置可覆盖」的数据，不是写死的
    代码常量。每一项是 ``(规则ID, 严重度)``；未列出的规则沿用规则目录声明。
    覆盖只改变处置档位，不改变规则是否执行，也不影响数据加载与清单输出。
    """

    factor_outlier_ratio: Decimal = Decimal("1.5")
    factor_outlier_block_ratio: Decimal = Decimal("3.0")
    unused_conversion_months: int = 12
    duplicate_name_threshold: Decimal = Decimal("0.92")
    duplicate_name_min_length: int = 6
    duplicate_name_window: int = 128
    duplicate_name_length_ratio: Decimal = Decimal("0.20")
    min_peer_items: int = 3
    stale_rate_months: int = 6
    # 本期降级行占比超过该阈值时输出告警（0~1，默认 30%）
    block_line_ratio_threshold: Decimal = Decimal("0.30")
    # 主数据级账套归属不符默认只提示；单据级不符在规则里固定阻断
    ledger_master_severity: Severity = Severity.WARN
    severity_overrides: tuple[tuple[str, Severity], ...] = ()

    def severity_of(self, rule_id: str, default: Severity) -> Severity:
        """取该规则生效的严重度：配置覆盖优先，否则用规则目录声明。"""
        for key, severity in self.severity_overrides:
            if key == rule_id:
                return severity
        return default

    def to_dict(self) -> dict[str, Any]:
        return {
            "factor_outlier_ratio": str(self.factor_outlier_ratio),
            "factor_outlier_block_ratio": str(self.factor_outlier_block_ratio),
            "unused_conversion_months": self.unused_conversion_months,
            "duplicate_name_threshold": str(self.duplicate_name_threshold),
            "duplicate_name_min_length": self.duplicate_name_min_length,
            "duplicate_name_window": self.duplicate_name_window,
            "duplicate_name_length_ratio": str(
                self.duplicate_name_length_ratio
            ),
            "min_peer_items": self.min_peer_items,
            "stale_rate_months": self.stale_rate_months,
            "block_line_ratio_threshold": str(self.block_line_ratio_threshold),
            "ledger_master_severity": self.ledger_master_severity.value,
            "severity_overrides": {
                rule_id: severity.value
                for rule_id, severity in self.severity_overrides
            },
        }


@dataclass(frozen=True)
class Finding:
    """差异清单的一行。规则元数据随行携带，导出 CSV 后无需再查规则表。"""

    rule_id: str
    rule_name: str
    category: str
    severity: Severity
    object_type: ObjectType
    object_code: str
    object_name: str = ""
    evidence: tuple[Evidence, ...] = ()
    suggestion: str = ""
    why: str = ""
    false_positive: str = ""
    supplier_code: str | None = None
    item_code: str | None = None
    period: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "rule_name": self.rule_name,
            "category": self.category,
            "severity": self.severity.value,
            "object_type": self.object_type.value,
            "object_code": self.object_code,
            "object_name": self.object_name,
            "evidence": [item.to_dict() for item in self.evidence],
            "suggestion": self.suggestion,
            "why": self.why,
            "false_positive": self.false_positive,
            "supplier_code": self.supplier_code,
            "item_code": self.item_code,
            "period": self.period,
        }


@dataclass(frozen=True)
class RuleSpec:
    """规则目录：每条规则必须带「为什么值得修」和「误报怎么处理」。"""

    rule_id: str
    name: str
    category: str
    default_severity: Severity
    why: str
    false_positive: str
    check: Callable[..., Iterable[Finding]] = field(repr=False, compare=False)

    def to_dict(self, effective_severity: Severity | None = None) -> dict[str, str]:
        """同时给出「目录声明」与「本次生效」两档，便于审计配置是否覆盖。"""
        effective = effective_severity or self.default_severity
        return {
            "rule_id": self.rule_id,
            "name": self.name,
            "category": self.category,
            "default_severity": self.default_severity.value,
            "effective_severity": effective.value,
            "severity_source": (
                "规则默认" if effective is self.default_severity else "配置覆盖"
            ),
            "block_semantics": BLOCK_SEMANTICS,
            "why": self.why,
            "false_positive": self.false_positive,
        }


@dataclass(frozen=True)
class FindingFilter:
    """差异清单过滤器。

    ``period`` 只筛「带期间的事实」；主数据级问题（``period is None``）始终保留，
    否则按期间导出时会把根因藏掉，只剩表面差异。
    """

    supplier: str | None = None
    item: str | None = None
    period: str | None = None

    def is_empty(self) -> bool:
        return not (self.supplier or self.item or self.period)

    def matches(self, finding: Finding) -> bool:
        if self.supplier:
            hit_supplier = finding.supplier_code == self.supplier or (
                finding.object_type is ObjectType.SUPPLIER
                and finding.object_code == self.supplier
            )
            if not hit_supplier:
                return False
        if self.item:
            hit_item = finding.item_code == self.item or (
                finding.object_type is ObjectType.ITEM
                and finding.object_code == self.item
            )
            if not hit_item:
                return False
        period_conflict = bool(
            self.period
            and finding.period is not None
            and finding.period != self.period
        )
        return not period_conflict


@dataclass(frozen=True)
class AppliedExemption:
    """报告里实际生效的豁免审计记录（只记录，不改变规则本身）。"""

    exemption_id: str
    subject_type: ObjectType
    subject_code: str
    rule_id: str
    submitted_by: str
    submitted_role: str
    submitted_at: str
    approved_by: str
    approved_role: str
    basis_type: str
    basis_ref: str
    basis_valid_from: str
    basis_valid_to: str
    reason: str
    granted_at: str
    expires_at: str
    second_reviewer: str = ""
    second_reviewed_role: str = ""
    escalated_review_by: str = ""
    cumulative_count: int = 1
    findings: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "exemption_id": self.exemption_id,
            "subject_type": self.subject_type.value,
            "subject_code": self.subject_code,
            "rule_id": self.rule_id,
            "submitted_by": self.submitted_by,
            "submitted_role": self.submitted_role,
            "submitted_at": self.submitted_at,
            "approved_by": self.approved_by,
            "approved_role": self.approved_role,
            "basis_type": self.basis_type,
            "basis_ref": self.basis_ref,
            "basis_valid_from": self.basis_valid_from,
            "basis_valid_to": self.basis_valid_to,
            "reason": self.reason,
            "granted_at": self.granted_at,
            "expires_at": self.expires_at,
            "second_reviewer": self.second_reviewer,
            "second_reviewed_role": self.second_reviewed_role,
            "escalated_review_by": self.escalated_review_by,
            "cumulative_count": self.cumulative_count,
            "findings_count": len(self.findings),
            "findings": list(self.findings),
        }


@dataclass(frozen=True)
class InspectionReport:
    source: str
    org_id: str | None
    as_of_period: str
    findings: tuple[Finding, ...]
    rules: tuple[RuleSpec, ...]
    options: InspectionOptions = field(default_factory=InspectionOptions)
    filters: FindingFilter = field(default_factory=FindingFilter)
    exemptions: tuple[AppliedExemption, ...] = ()
    # 豁免影响面上限（15% 二次复核 / 整批豁免）是否已校验；无行级数据时为 False
    exemption_cap_checked: bool = False
    exemption_cap_note: str = ""

    @property
    def blocked_count(self) -> int:
        return sum(1 for item in self.findings if item.severity is Severity.BLOCK)

    @property
    def warning_count(self) -> int:
        return sum(1 for item in self.findings if item.severity is Severity.WARN)

    def summary(self) -> dict[str, Any]:
        by_rule: dict[str, int] = {}
        by_category: dict[str, int] = {}
        for finding in self.findings:
            by_rule[finding.rule_id] = by_rule.get(finding.rule_id, 0) + 1
            by_category[finding.category] = by_category.get(finding.category, 0) + 1
        return {
            "total": len(self.findings),
            "blocked": self.blocked_count,
            "warnings": self.warning_count,
            "by_rule": by_rule,
            "by_category": by_category,
        }

    def metrics(self) -> dict[str, Any]:
        """体检质量指标；其中「阻断命中率」是判断阻断规则是否过严的唯一依据。

        ``block_hit_rate`` = 阻断差异数 / 差异总数，按「条数」统计，回答
        「规则一开，多少条结论直接转人工」；降级行占比（按对账行）是另一个指标，
        由 :func:`apps.masterdata.impact.evaluate_block_impact` 计算，两者不能混用。
        """
        total = len(self.findings)
        ratio = (
            Decimal(self.blocked_count) / Decimal(total) if total else Decimal(0)
        )
        blocked_rules = sorted(
            {
                item.rule_id
                for item in self.findings
                if item.severity is Severity.BLOCK
            }
        )
        rules_hit = len({item.rule_id for item in self.findings})
        return {
            "total_findings": total,
            "blocked_findings": self.blocked_count,
            "warning_findings": self.warning_count,
            "block_hit_rate": _ratio_value(ratio),
            "block_hit_rate_pct": format_ratio(ratio),
            "block_hit_rate_definition": (
                "阻断命中率 = 阻断差异数 / 差异总数（按条数，不按行数）；"
                "只用于判断阻断规则是否定得过严"
            ),
            "blocked_rule_count": len(blocked_rules),
            "blocked_rules": blocked_rules,
            "rules_hit": rules_hit,
            "total_rules": len(self.rules),
            "rules_hit_rate": _ratio_value(
                Decimal(rules_hit) / Decimal(len(self.rules))
                if self.rules
                else Decimal(0)
            ),
        }

    def apply_filter(self, filters: FindingFilter) -> InspectionReport:
        if filters.is_empty():
            return InspectionReport(
                source=self.source,
                org_id=self.org_id,
                as_of_period=self.as_of_period,
                findings=self.findings,
                rules=self.rules,
                options=self.options,
                filters=filters,
                exemptions=self.exemptions,
                exemption_cap_checked=self.exemption_cap_checked,
                exemption_cap_note=self.exemption_cap_note,
            )
        kept = tuple(item for item in self.findings if filters.matches(item))
        return InspectionReport(
            source=self.source,
            org_id=self.org_id,
            as_of_period=self.as_of_period,
            findings=kept,
            rules=self.rules,
            options=self.options,
            filters=filters,
            exemptions=self.exemptions,
            exemption_cap_checked=self.exemption_cap_checked,
            exemption_cap_note=self.exemption_cap_note,
        )

    def filter(
        self,
        *,
        supplier: str | None = None,
        item: str | None = None,
        period: str | None = None,
    ) -> InspectionReport:
        return self.apply_filter(
            FindingFilter(supplier=supplier, item=item, period=period)
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "org_id": self.org_id,
            "as_of_period": self.as_of_period,
            "block_semantics": BLOCK_SEMANTICS,
            "summary": self.summary(),
            "metrics": self.metrics(),
            "exemption_count": len(self.exemptions),
            "exemption_cap_checked": self.exemption_cap_checked,
            "exemption_cap_note": self.exemption_cap_note,
            "exemptions": [item.to_dict() for item in self.exemptions],
            "filters": {
                "supplier": self.filters.supplier,
                "item": self.filters.item,
                "period": self.filters.period,
            },
            "options": self.options.to_dict(),
            "rules": [
                rule.to_dict(
                    self.options.severity_of(rule.rule_id, rule.default_severity)
                )
                for rule in self.rules
            ],
            "findings": [item.to_dict() for item in self.findings],
        }

    def to_csv(self) -> str:
        from .export import render_csv

        return render_csv(self)

    def write_csv(self, path: str | Path) -> Path:
        from .export import write_csv

        return write_csv(self, path)
