"""S2 对账引擎领域模型（纯 Python，无数据库依赖）。

对齐 PRD §4.0 / §5.0 / §7.1 与 C1 数据模型口径：
- 金额一律 ``Money{amount, currency, tax_included}``，禁止裸数字（G-12：4 位小数为存储精度）；
- 缺失 ≠ 零：数量/单价/金额缺失用 ``None`` 表示，不补 0；
- 结论必须带 ``Evidence{system, doc_no, field}``（G-06），置信度 < 0.70 视为不可用。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from enum import Enum
from typing import Any

# 金额存储精度：4 位小数（G-12）
MONEY_EXP = Decimal("0.0001")
# 置信度精度：2 位小数（agent_decision.confidence numeric(3,2)）
CONF_EXP = Decimal("0.01")
# 金额比较容差：0.01（G-12）
EQUALITY_TOLERANCE = Decimal("0.01")
# 低于该置信度不得作为结论输出（PRD §5.0）
MIN_CONFIDENCE = Decimal("0.70")
# 多候选置信度差小于该值 → 匹配歧义，不下结论（FR-103）
AMBIGUITY_GAP = Decimal("0.10")


def to_decimal(value: Any, *, places: Decimal | None = None) -> Decimal | None:
    """把外部输入（str/int/float/Decimal）转成 Decimal；空值返回 None，绝不补 0。"""
    if value is None:
        return None
    if isinstance(value, str) and not value.strip():
        return None
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:  # 明确报错，不静默当 0
        raise ValueError(f"无法解析为 Decimal: {value!r}") from exc
    if places is not None:
        result = result.quantize(places, rounding=ROUND_HALF_UP)
    return result


def quantize_confidence(value: Decimal) -> Decimal:
    return to_decimal(value, places=CONF_EXP)  # type: ignore[return-value]


@dataclass(frozen=True)
class Money:
    """金额：4 位小数存储、2 位展示由上层负责。"""

    amount: Decimal
    currency: str = "CNY"
    tax_included: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "amount", to_decimal(self.amount, places=MONEY_EXP)
        )

    @classmethod
    def of(
        cls,
        value: Any,
        currency: str = "CNY",
        tax_included: bool = True,
    ) -> Money | None:
        amount = to_decimal(value, places=MONEY_EXP)
        if amount is None:
            return None
        return cls(amount=amount, currency=currency, tax_included=tax_included)

    def _check_currency(self, other: Money) -> None:
        if self.currency != other.currency:
            raise ValueError(
                f"币种不一致，无法直接相减: {self.currency} vs {other.currency}"
            )

    def __sub__(self, other: Money) -> Money:
        self._check_currency(other)
        return Money(self.amount - other.amount, self.currency, self.tax_included)

    def __add__(self, other: Money) -> Money:
        self._check_currency(other)
        return Money(self.amount + other.amount, self.currency, self.tax_included)

    def __neg__(self) -> Money:
        return Money(-self.amount, self.currency, self.tax_included)

    def is_zero(self, tolerance: Decimal = EQUALITY_TOLERANCE) -> bool:
        return abs(self.amount) <= tolerance

    def nearly_equal(
        self, other: Money, tolerance: Decimal = EQUALITY_TOLERANCE
    ) -> bool:
        self._check_currency(other)
        return abs(self.amount - other.amount) <= tolerance

    def to_dict(self) -> dict:
        return {
            "amount": f"{self.amount:.4f}",
            "currency": self.currency,
            "tax_included": self.tax_included,
        }


@dataclass(frozen=True)
class Evidence:
    """证据：任何结论中的数字都必须能回到 系统 + 单据号 + 字段（G-06）。"""

    system: str
    doc_no: str
    field: str
    coord: Mapping[str, Any] | None = None

    def to_dict(self) -> dict:
        payload: dict = {
            "system": self.system,
            "doc_no": self.doc_no,
            "field": self.field,
        }
        if self.coord is not None:
            payload["coord"] = dict(self.coord)
        return payload


@dataclass(frozen=True)
class DocLine:
    """单据行（我方 ERP/MES/WMS 或供应商对账单）。"""

    system: str
    doc_no: str
    line_no: int
    supplier_code: str
    item_code: str | None = None
    item_name: str | None = None
    qty: Decimal | None = None
    unit: str | None = None
    unit_price: Decimal | None = None
    amount: Money | None = None
    tax_rate: Decimal | None = None
    scrap_qty: Decimal | None = None
    period: str | None = None
    coord: Mapping[str, Any] | None = None

    @property
    def ref(self) -> str:
        return f"{self.system}:{self.doc_no}#{self.line_no}"

    def evidence(self, field_name: str) -> Evidence:
        return Evidence(
            system=self.system,
            doc_no=self.doc_no,
            field=field_name,
            coord=self.coord,
        )


@dataclass(frozen=True)
class Scope:
    """G-05 / §5.0：任何任务必须带组织、期间、系统范围。"""

    org_id: str | None
    period: str | None
    system_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class SupplierMaster:
    code: str
    name: str
    aliases: tuple[str, ...] = ()
    tax_no: str | None = None


@dataclass(frozen=True)
class ItemMaster:
    code: str
    name: str
    base_unit: str | None = None
    spec: str | None = None


@dataclass(frozen=True)
class UnitConversion:
    """1 ``from_unit`` = ``factor`` ``to_unit``。"""

    item: str
    from_unit: str
    to_unit: str
    factor: Decimal


@dataclass(frozen=True)
class MasterData:
    suppliers: Mapping[str, SupplierMaster] = field(default_factory=dict)
    items: Mapping[str, ItemMaster] = field(default_factory=dict)
    unit_conversions: tuple[UnitConversion, ...] = ()

    def supplier(self, code: str | None) -> SupplierMaster | None:
        if not code:
            return None
        return self.suppliers.get(code)

    def item(self, code: str | None) -> ItemMaster | None:
        if not code:
            return None
        return self.items.get(code)


@dataclass(frozen=True)
class Tolerance:
    """差异比较容差，来自 options.tolerance（PRD AG-1 输入契约）。

    ``rounding``：金额「舍入容差」单行上限，默认 0.05 元。依据：供应商对账单
    普遍按角（0.1 元）取整，单行取整最大误差为 0.05 元；超过该值的差额不再是
    舍入噪声，必须按真实差异归因或转人工。
    ``rounding_batch_limit``：整批舍入差累计上限，默认 1.00 元。依据：单行都在
    容差内但行数多时可能系统性放大（例如整批按角向上取整），累计超过 1 元即不再
    视为噪声，转人工确认，避免「凑整」被静默放过。
    """

    amount: Decimal = EQUALITY_TOLERANCE
    price: Decimal = Decimal("0.0001")
    qty: Decimal = Decimal("0.0001")
    rounding: Decimal = Decimal("0.05")
    rounding_batch_limit: Decimal = Decimal("1.00")


class MatchType(str, Enum):
    EXACT = "exact"          # 单据号直配
    FUZZY = "fuzzy"          # 业务键模糊配
    AGGREGATED = "aggregated"  # 多行合计匹配（拆单/合并），需单独标注
    CANDIDATE = "candidate"  # 候选，不自动选
    UNMATCHED = "unmatched"


class LineStatus(str, Enum):
    MATCHED = "已匹配"
    NEEDS_REVIEW = "待人工"
    AMBIGUOUS = "匹配歧义"
    CANDIDATE_ONLY = "候选待人工"
    UNMATCHED_OURS = "我方未匹配"
    UNMATCHED_SUPPLIER = "供应商未匹配"
    DUPLICATE = "重复计费"
    # 金额/数量无法解析：该行不参与对账，整批转人工（不静默当 0，也不抛异常）
    UNPARSEABLE = "不可解析"


class DiffType(str, Enum):
    """七类归因口径（FR-104 / 01-技术架构 ag1.reconcile.output）。

    ``ROUNDING_TOLERANCE`` 是**容差调整类**，不属于七类业务归因：它只在差异明细
    里留痕（金额、阈值、证据齐全，可审计），不生成差异单、不计入归因准确率。
    """

    UNIT_PRICE = "单价不一致"
    QTY = "数量不符"
    SCRAP = "报废未扣"
    UOM = "单位换算"
    TAX = "税率口径"
    MISSING_DOC = "缺单"
    DUPLICATE = "重复计费"
    ROUNDING_TOLERANCE = "金额舍入容差"


class DiffOwner(str, Enum):
    OURS = "ours"
    SUPPLIER = "supplier"
    SYSTEM = "system"


class BatchStatus(str, Enum):
    NO_CONCLUSION = "无法对账"      # 降级：宁可不出结论
    NEEDS_REVIEW = "待人工复核"     # 存在歧义/低置信度，不得自动通过
    RECONCILED = "已对账"


class MissingKind(str, Enum):
    SCOPE = "范围缺失"
    SUPPLIER = "供应商未绑定"
    ITEM = "物料未绑定"
    BASE_UNIT = "基础单位缺失"
    UNIT_CONVERSION = "单位换算缺失"
    LINE_DATA = "行数据缺失"


@dataclass(frozen=True)
class MissingItem:
    kind: MissingKind
    code: str
    detail: str
    source_ref: str

    def to_dict(self) -> dict:
        return {
            "kind": self.kind.value,
            "code": self.code,
            "detail": self.detail,
            "source_ref": self.source_ref,
        }


@dataclass(frozen=True)
class Candidate:
    line: DocLine
    confidence: Decimal
    # 低分原因：用于区分「单位不可换算（降权）」与「业务键相似度低」，供人工判断
    reason: str = ""

    def to_dict(self) -> dict:
        return {
            "ref": self.line.ref,
            "confidence": f"{self.confidence:.2f}",
            "reason": self.reason,
        }


@dataclass(frozen=True)
class MatchPair:
    our: DocLine
    supplier: DocLine
    match_type: MatchType
    confidence: Decimal


@dataclass(frozen=True)
class DiffItem:
    type: DiffType
    amount: Money
    owner: DiffOwner
    suggestion: str
    confidence: Decimal
    evidence: tuple[Evidence, ...]
    ref: str
    supplier_code: str = ""

    @property
    def needs_manual(self) -> bool:
        """置信度 < 0.70 一律待人工（PRD §5.0），不得自动通过。"""
        return self.confidence < MIN_CONFIDENCE

    def to_dict(self) -> dict:
        return {
            "type": self.type.value,
            "amount": self.amount.to_dict(),
            "owner": self.owner.value,
            "suggestion": self.suggestion,
            "confidence": f"{self.confidence:.2f}",
            "needs_manual": self.needs_manual,
            "ref": self.ref,
            "supplier_code": self.supplier_code,
            "evidence": [item.to_dict() for item in self.evidence],
        }


@dataclass(frozen=True)
class DiscrepancyDraft:
    """差异单草稿（FR-106）：按「供应商 × 差异类型」聚合，仅草稿，不写入 ERP。"""

    draft_no: str
    supplier_code: str
    period: str
    diff_type: DiffType
    amount: Money
    owner: DiffOwner
    suggestion: str
    confidence: Decimal
    needs_manual: bool
    refs: tuple[str, ...]
    evidence: tuple[Evidence, ...]
    status: str = "草稿"

    def to_dict(self) -> dict:
        return {
            "draft_no": self.draft_no,
            "supplier_code": self.supplier_code,
            "period": self.period,
            "diff_type": self.diff_type.value,
            "amount": self.amount.to_dict(),
            "owner": self.owner.value,
            "suggestion": self.suggestion,
            "confidence": f"{self.confidence:.2f}",
            "needs_manual": self.needs_manual,
            "refs": list(self.refs),
            "evidence": [item.to_dict() for item in self.evidence],
            "status": self.status,
        }


@dataclass(frozen=True)
class UnattributedItem:
    """金额有差但七类规则均无法解释：不出归因结论，交人工。"""

    ref: str
    amount_diff: Money
    reason: str
    evidence: tuple[Evidence, ...]

    def to_dict(self) -> dict:
        return {
            "ref": self.ref,
            "amount_diff": self.amount_diff.to_dict(),
            "reason": self.reason,
            "needs_manual": True,
            "evidence": [item.to_dict() for item in self.evidence],
        }


@dataclass(frozen=True)
class MatchResultLine:
    item: str | None
    qty_mine: Decimal | None
    qty_supplier: Decimal | None
    amount: Money | None
    match_type: MatchType
    confidence: Decimal
    status: LineStatus
    our_ref: str | None = None
    supplier_ref: str | None = None
    candidates: tuple[Candidate, ...] = ()
    diffs: tuple[DiffItem, ...] = ()
    note: str = ""
    # 聚合匹配时覆盖的我方行（拆单为 1 个、合并为多个），便于人工复核区分
    aggregate_refs: tuple[str, ...] = ()

    @property
    def needs_manual(self) -> bool:
        return self.status in {
            LineStatus.AMBIGUOUS,
            LineStatus.CANDIDATE_ONLY,
            LineStatus.NEEDS_REVIEW,
        }

    def to_dict(self) -> dict:
        return {
            "item": self.item,
            "qty_mine": None if self.qty_mine is None else f"{self.qty_mine:.4f}",
            "qty_supplier": (
                None if self.qty_supplier is None else f"{self.qty_supplier:.4f}"
            ),
            "amount": None if self.amount is None else self.amount.to_dict(),
            "match_type": self.match_type.value,
            "confidence": f"{self.confidence:.2f}",
            "status": self.status.value,
            "our_ref": self.our_ref,
            "supplier_ref": self.supplier_ref,
            "candidates": [c.to_dict() for c in self.candidates],
            "diffs": [d.to_dict() for d in self.diffs],
            "aggregate_refs": list(self.aggregate_refs),
            "needs_manual": self.needs_manual,
            "note": self.note,
        }


@dataclass(frozen=True)
class ReconMetrics:
    supplier_lines: int
    matched: int
    matched_rate: Decimal
    ambiguous: int
    candidate_only: int
    unmatched: int
    diff_lines: int
    diff_rate: Decimal
    diff_amount: Decimal
    suspected_anomaly: bool
    auto_disposal_allowed: bool
    # 聚合匹配行数（拆单/合并）
    aggregated: int = 0
    # 舍入容差行数与金额（仅在明细里留痕，不计入七类归因/差异单）
    rounding_lines: int = 0
    rounding_amount: Decimal = Decimal(0)
    # 舍入差累计是否超限，超限时整批转人工
    rounding_escalated: bool = False

    def to_dict(self) -> dict:
        return {
            "supplier_lines": self.supplier_lines,
            "matched": self.matched,
            "matched_rate": f"{self.matched_rate:.4f}",
            "ambiguous": self.ambiguous,
            "candidate_only": self.candidate_only,
            "unmatched": self.unmatched,
            "diff_lines": self.diff_lines,
            "diff_rate": f"{self.diff_rate:.4f}",
            "diff_amount": f"{self.diff_amount:.4f}",
            "suspected_anomaly": self.suspected_anomaly,
            "auto_disposal_allowed": self.auto_disposal_allowed,
            "aggregated": self.aggregated,
            "rounding_lines": self.rounding_lines,
            "rounding_amount": f"{self.rounding_amount:.4f}",
            "rounding_escalated": self.rounding_escalated,
        }


@dataclass(frozen=True)
class ParseIssue:
    """行内金额/数量无法解析：留下行号与原值，供人工核对，绝不静默当 0。"""

    ref: str
    side: str  # "our" | "supplier"
    field: str
    raw: str
    supplier_code: str = ""
    doc_no: str = ""
    line_no: int = 0

    def to_dict(self) -> dict:
        return {
            "ref": self.ref,
            "side": self.side,
            "field": self.field,
            "raw": self.raw,
            "supplier_code": self.supplier_code,
            "doc_no": self.doc_no,
            "line_no": self.line_no,
        }


@dataclass(frozen=True)
class ReconRequest:
    scope: Scope
    suppliers: tuple[str, ...]
    our_lines: tuple[DocLine, ...]
    supplier_lines: tuple[DocLine, ...]
    master_data: MasterData
    tolerance: Tolerance = field(default_factory=Tolerance)
    # 解析失败的行（loader 采集，引擎据此做行级降级，不向外抛异常）
    parse_issues: tuple[ParseIssue, ...] = ()


@dataclass(frozen=True)
class ReconResult:
    batch_status: BatchStatus
    conclusive: bool
    status_reason: str
    missing_items: tuple[MissingItem, ...] = ()
    lines: tuple[MatchResultLine, ...] = ()
    diffs: tuple[DiffItem, ...] = ()
    unattributed: tuple[UnattributedItem, ...] = ()
    discrepancy_drafts: tuple[DiscrepancyDraft, ...] = ()
    metrics: ReconMetrics | None = None
    evidence: tuple[Evidence, ...] = ()

    def to_dict(self) -> dict:
        return {
            "batch_status": self.batch_status.value,
            "conclusive": self.conclusive,
            "status_reason": self.status_reason,
            "missing_items": [m.to_dict() for m in self.missing_items],
            "lines": [line.to_dict() for line in self.lines],
            "diffs": [d.to_dict() for d in self.diffs],
            "unattributed": [u.to_dict() for u in self.unattributed],
            "discrepancy_drafts": [d.to_dict() for d in self.discrepancy_drafts],
            "metrics": None if self.metrics is None else self.metrics.to_dict(),
            "evidence": [e.to_dict() for e in self.evidence],
        }


def dedupe_missing(items: Iterable[MissingItem]) -> tuple[MissingItem, ...]:
    """缺失清单去重（同 kind + code 只报一次），保持出现顺序。"""
    seen: set[tuple[str, str]] = set()
    result: list[MissingItem] = []
    for item in items:
        key = (item.kind.value, item.code)
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
    return tuple(result)
