"""单据解析输出契约（Pydantic v2）。

对齐 PRD §4 M4（FR-401/402/404/405）：

- 每个字段带「值 + 置信度 + 原文坐标（页码 + 包围盒）」，供前端点击回链影像；
- 空缺字段必须是 ``None``，不得填 0；缺失字段的置信度固定为 0；
- 置信度低于 0.85 的字段进入 ``low_confidence_fields``，必须人工确认；
- 无法识别时返回 ``status=unrecognized`` 并列出缺失的关键字段，不允许编造值。

坐标约定：``bbox`` 为 ``(x0, y0, x1, y1)``，原点在页面左上角，单位与具体抽取器一致
（离线文本抽取器使用按字符估算的近似像素坐标，见 README）。
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from enum import Enum
from typing import Any, Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# 契约版本：字段结构变更时递增，便于解析结果落库后追溯
CONTRACT_VERSION = "0.1.0"

# FR-401/405：字段置信度低于 0.85 必须人工确认
LOW_CONFIDENCE_THRESHOLD = 0.85
# FR-401：分类置信度低于 0.8 要求人工选择单据类型
CLASSIFICATION_THRESHOLD = 0.80


class ContractModel(BaseModel):
    """契约模型公共配置：禁止额外字段、不可变，保证落库/回放稳定。"""

    model_config = ConfigDict(extra="forbid", frozen=True)


class DocType(str, Enum):
    INVOICE = "invoice"
    DELIVERY_NOTE = "delivery_note"
    STATEMENT = "statement"
    PICKING_SLIP = "picking_slip"
    UNKNOWN = "unknown"


DOC_TYPE_LABELS: dict[DocType, str] = {
    DocType.INVOICE: "发票",
    DocType.DELIVERY_NOTE: "送货单",
    DocType.STATEMENT: "对账单",
    DocType.PICKING_SLIP: "领料单",
    DocType.UNKNOWN: "未知类型",
}


class ParseStatus(str, Enum):
    OK = "ok"
    NEEDS_REVIEW = "needs_review"
    UNRECOGNIZED = "unrecognized"


PARSE_STATUS_LABELS: dict[ParseStatus, str] = {
    ParseStatus.OK: "解析成功",
    ParseStatus.NEEDS_REVIEW: "需人工确认",
    ParseStatus.UNRECOGNIZED: "无法识别",
}


class FieldSource(str, Enum):
    """字段来源。RULE/DERIVED 为当前离线实现；OCR/LLM 预留给接入真模型后使用。"""

    RULE = "rule"
    DERIVED = "derived"
    OCR = "ocr"
    LLM = "llm"


class IssueSeverity(str, Enum):
    WARNING = "warning"
    ERROR = "error"


class Coordinate(ContractModel):
    """原文证据坐标：页码从 1 开始，bbox 为左上/右下两点的归一化或像素坐标。"""

    page: int = Field(ge=1, description="页码，从 1 开始")
    bbox: tuple[float, float, float, float] = Field(
        description="(x0, y0, x1, y1)，原点在页面左上角"
    )

    @field_validator("bbox")
    @classmethod
    def _bbox_must_be_ordered(
        cls, value: tuple[float, float, float, float]
    ) -> tuple[float, float, float, float]:
        x0, y0, x1, y1 = value
        if x0 < 0 or y0 < 0:
            raise ValueError("bbox 坐标不能为负数")
        if x1 <= x0 or y1 <= y0:
            raise ValueError("bbox 必须满足 x1 > x0 且 y1 > y0")
        return value


T = TypeVar("T")


# 写法取舍：3.12 下可写成 class FieldValue[T]（PEP 695），ruff 的 UP046 也就不需要 noqa；
# 但本机默认解释器是 3.11，实测那种写法会让 tests/docparse 在 3.11 上 12 个模块收集失败。
# 仓库其它泛型类（apps/api/schemas.py、apps/connectors/canonical/mapping.py）同样是 Generic，
# 这里保持一致，用 3.11/3.12 都能解析的写法，仅在这一行压 UP046。
class FieldValue(ContractModel, Generic[T]):  # noqa: UP046
    """单个抽取字段：值、置信度、证据坐标与来源。

    缺失（``value is None``）时置信度必须为 0；有值时置信度必须大于 0。
    这条约束是 FR-402「空缺字段不得被填充为 0」的机器可校验版本。
    """

    value: T | None = None
    confidence: float = Field(ge=0.0, le=1.0)
    coords: list[Coordinate] = Field(default_factory=list)
    raw_text: str | None = None
    source: FieldSource = FieldSource.RULE

    @model_validator(mode="after")
    def _check_missing_consistency(self) -> FieldValue[T]:
        if self.value is None and self.confidence != 0.0:
            raise ValueError("缺失字段的置信度必须为 0.0，不得伪装成已识别")
        if self.value is not None and self.confidence <= 0.0:
            raise ValueError("有值字段的置信度必须大于 0")
        return self

    @property
    def is_missing(self) -> bool:
        return self.value is None

    def is_low_confidence(self, threshold: float = LOW_CONFIDENCE_THRESHOLD) -> bool:
        return self.value is not None and self.confidence < threshold


StrField = FieldValue[str]
DecimalField = FieldValue[Decimal]
DateField = FieldValue[date]


def _empty_str() -> FieldValue[str]:
    return StrField(value=None, confidence=0.0)


def _empty_decimal() -> FieldValue[Decimal]:
    return DecimalField(value=None, confidence=0.0)


def _empty_date() -> FieldValue[date]:
    return DateField(value=None, confidence=0.0)


# 字段中文名：用于缺失字段清单、低置信提示与前端表单标签
HEADER_FIELD_LABELS: dict[str, str] = {
    "doc_no": "单据号码",
    "doc_date": "单据日期",
    "supplier_name": "供应商名称",
    "supplier_tax_no": "供应商税号",
    "buyer_name": "购买方名称",
    "currency": "币种",
    "subtotal": "金额小计",
    "tax_amount": "税额合计",
    "total_amount": "价税合计",
    "tax_rate": "税率",
}

LINE_FIELD_LABELS: dict[str, str] = {
    "item_name": "物料名称",
    "item_code": "物料编码",
    "spec": "规格型号",
    "unit": "单位",
    "quantity": "数量",
    "unit_price": "单价",
    "amount": "金额",
    "tax_rate": "税率",
    "tax_amount": "税额",
}

# 各单据类型的头关键字段：缺失即无法自动进入下一步（FR-404 转待补录）
CRITICAL_HEADER_FIELDS: dict[DocType, tuple[str, ...]] = {
    DocType.INVOICE: ("doc_no", "doc_date", "supplier_name", "total_amount"),
    DocType.DELIVERY_NOTE: ("doc_no", "doc_date", "supplier_name"),
    DocType.STATEMENT: ("doc_no", "doc_date", "supplier_name", "total_amount"),
    DocType.PICKING_SLIP: ("doc_no", "doc_date"),
    DocType.UNKNOWN: (),
}

# 明细行必须能定位「是什么、多少个」，否则该行需人工补录
CRITICAL_LINE_FIELDS: tuple[str, ...] = ("item_name", "quantity")


class DocHeader(ContractModel):
    """单据头。所有字段常驻，未识别到的字段内部 value 为 None。"""

    doc_no: StrField = Field(default_factory=_empty_str)
    doc_date: DateField = Field(default_factory=_empty_date)
    supplier_name: StrField = Field(default_factory=_empty_str)
    supplier_tax_no: StrField = Field(default_factory=_empty_str)
    buyer_name: StrField = Field(default_factory=_empty_str)
    currency: StrField = Field(default_factory=_empty_str)
    subtotal: DecimalField = Field(default_factory=_empty_decimal)
    tax_amount: DecimalField = Field(default_factory=_empty_decimal)
    total_amount: DecimalField = Field(default_factory=_empty_decimal)
    tax_rate: DecimalField = Field(default_factory=_empty_decimal)

    def field(self, name: str) -> FieldValue[Any]:
        return getattr(self, name)


class DocLine(ContractModel):
    """明细行。page 记录行来自第几页；多页表格合并后仍保留原页码（FR-402）。"""

    page: int = Field(ge=1)
    line_no: int | None = None
    item_name: StrField = Field(default_factory=_empty_str)
    item_code: StrField = Field(default_factory=_empty_str)
    spec: StrField = Field(default_factory=_empty_str)
    unit: StrField = Field(default_factory=_empty_str)
    quantity: DecimalField = Field(default_factory=_empty_decimal)
    unit_price: DecimalField = Field(default_factory=_empty_decimal)
    amount: DecimalField = Field(default_factory=_empty_decimal)
    tax_rate: DecimalField = Field(default_factory=_empty_decimal)
    tax_amount: DecimalField = Field(default_factory=_empty_decimal)

    def field(self, name: str) -> FieldValue[Any]:
        return getattr(self, name)


class MissingField(ContractModel):
    """缺失字段条目。path 形如 ``header.supplier_name`` 或 ``lines[2].quantity``。"""

    path: str
    label: str
    critical: bool
    reason: str = "未在文档中识别到该字段"


class ValidationIssue(ContractModel):
    """校验/降级问题。code 供前端与统计使用，message 给人读。"""

    code: str
    severity: IssueSeverity
    message: str
    field_path: str | None = None
    expected: Decimal | None = None
    actual: Decimal | None = None


class ParseResult(ContractModel):
    """一次解析的完整输出。"""

    contract_version: str = CONTRACT_VERSION
    file_name: str
    file_kind: str
    doc_type: DocType
    doc_type_confidence: float = Field(ge=0.0, le=1.0)
    status: ParseStatus
    message: str | None = None
    header: DocHeader = Field(default_factory=DocHeader)
    lines: list[DocLine] = Field(default_factory=list)
    missing_fields: list[MissingField] = Field(default_factory=list)
    low_confidence_fields: list[str] = Field(default_factory=list)
    issues: list[ValidationIssue] = Field(default_factory=list)
    pages: int = Field(default=0, ge=0)
    parser: str

    @property
    def is_unrecognized(self) -> bool:
        return self.status is ParseStatus.UNRECOGNIZED
