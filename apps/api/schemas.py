"""``/erp/api/v1`` 的请求/响应模型（C16 的唯一来源）。

口径：

- 金额一律十进制文本（``str``），与 ``contracts/*.v1.json`` 的「金额禁止 number」一致；
- 分页统一 ``{items, next_cursor}``，游标不透明；
- 入参 ``extra="forbid"``：多传字段直接 422，不静默忽略。
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Any, Generic, Literal, TypeVar
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from apps.platform.contracts.ticket import TicketState

T = TypeVar("T")

ApprovalRole = Literal["reviewer", "approver", "cfo"]
TicketAction = Literal["approve", "reject", "cancel", "compensate"]

#: 工单状态枚举从状态机派生（值即契约里的中文状态名），不手写 12 态清单。
TicketStateCode = Enum(
    "TicketStateCode",
    {state.name: state.value for state in TicketState},
    type=str,
    module=__name__,
)


class BillingBucket(str, Enum):
    """行/差异的计费桶：与计量表四类计数共用同一分类函数，禁止第二处重算。"""

    BILLABLE = "billable"
    AUTO_RELEASED = "auto_released"
    DISPUTE_HOLD = "dispute_hold"
    OTHER_UNBILLABLE = "other_unbillable"


class ScopeIn(BaseModel):
    """G-05：三个字段缺一不可，只允许在授权范围内收窄。"""

    model_config = ConfigDict(extra="forbid")

    org_id: str = Field(min_length=1, max_length=64)
    period: str = Field(min_length=1, max_length=32)
    system_ids: list[str] = Field(min_length=1, max_length=16)

    def as_tuple(self) -> tuple[str, ...]:
        return tuple(self.system_ids)


class Page(BaseModel, Generic[T]):  # noqa: UP046 - 运行环境 Python 3.11 不支持下述 PEP 695 语法
    items: list[T]
    next_cursor: str | None = None


# --------------------------------------------------------------------------- 会话


class SessionScopeOut(BaseModel):
    """服务端从会话授权范围派生出的默认 scope（只读，不来自请求）。

    ``org_id`` 多组织授权时为 ``None``，由前端让用户选择，服务端不猜；
    ``period`` 仅在授权期内存在合法 ``YYYY-MM`` 时给出最新一个，否则 ``None``。
    """

    org_id: str | None = None
    period: str | None = None
    system_ids: list[str]


class SessionOut(BaseModel):
    """当前会话身份：前端只读这里，不再从构建期变量猜角色/用户。"""

    user_id: UUID
    tenant_id: UUID
    roles: list[str]
    default_scope: SessionScopeOut | None = None


# --------------------------------------------------------------------------- 工单


class TicketStepOut(BaseModel):
    step_no: int
    step_id: UUID
    action_key: str
    system: str
    object_type: str
    object_id: str
    state: str
    level: str
    idem_key: str
    executed_at: datetime | None = None


class ApprovalOut(BaseModel):
    role: str
    approver_id: UUID
    decision: Literal["approved", "rejected"]
    comment: str
    decided_at: datetime


class TicketOut(BaseModel):
    ticket_id: UUID
    ticket_no: str
    scene_code: str
    state: TicketStateCode
    version: int
    initiator_id: UUID
    business_key: str
    org_id: str
    period: str
    systems: list[str]
    write_level: str
    amount: str
    touches_fund: bool
    created_at: datetime
    updated_at: datetime


class TicketDetailOut(TicketOut):
    steps: list[TicketStepOut]
    approvals: list[ApprovalOut]
    required_roles: list[str]
    missing_roles: list[str]
    audit_image_count: int
    #: 同步回滚入口是否开放（``ERP_ROLLBACK_SYNC_ENABLED``）。
    #: false 时前端必须禁用按钮并说明原因，不能靠「点了报错」提示。
    rollback_available: bool


class ActionIn(BaseModel):
    """工单动作入参；``comment`` 同时承担审批意见 / 终止原因（FR-107）。

    ``reason`` 单列：回滚他人工单必须填写理由并留审计（FR-1303 / 权限矩阵 §4.5），
    不能复用 ``comment``——否则「没填理由」与「填了意见」在审计里无法区分。
    """

    model_config = ConfigDict(extra="forbid")

    scope: ScopeIn
    role: ApprovalRole | None = None
    comment: str = Field(default="", max_length=2000)
    reason: str | None = Field(default=None, max_length=2000)
    confirmed_manual: bool = False
    fail_code: Literal["F1", "F2", "F3", "F4", "F5", "F6", "F7"] | None = None


class ActionOut(BaseModel):
    ticket_id: UUID
    action: TicketAction
    state: str
    version: int
    required_roles: list[str] = Field(default_factory=list)
    missing_roles: list[str] = Field(default_factory=list)
    executed_steps: list[TicketStepOut] = Field(default_factory=list)
    manual_tasks: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------------- 审批


class ApprovalTaskOut(BaseModel):
    ticket_id: UUID
    ticket_no: str
    scene_code: str
    business_key: str
    state: str
    write_level: str
    amount: str
    touches_fund: bool
    initiator_id: UUID
    required_roles: list[str]
    missing_roles: list[str]
    created_at: datetime


class ApprovalDecisionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scope: ScopeIn
    role: ApprovalRole
    comment: str = Field(default="", max_length=2000)


class ApprovalDecisionOut(BaseModel):
    ticket_id: UUID
    role: str
    decision: Literal["approved", "rejected"]
    decided_at: datetime
    satisfied: bool
    required_roles: list[str]
    missing_roles: list[str]
    ticket_state: str


# --------------------------------------------------------------------------- 对账


class MoneyOut(BaseModel):
    amount: str
    currency: str
    tax_included: bool


class EvidenceOut(BaseModel):
    system: str
    doc_no: str
    field: str
    coord: dict[str, Any] | None = None


class MissingItemOut(BaseModel):
    kind: str
    code: str
    detail: str
    source_ref: str


class ReconLineIn(BaseModel):
    """对账输入行；字段与 ``apps.recon.loader._build_line`` 一一对应。"""

    model_config = ConfigDict(extra="forbid")

    system: str = Field(min_length=1, max_length=32)
    doc_no: str = Field(min_length=1, max_length=128)
    line_no: int = Field(default=1, ge=1)
    supplier_code: str = Field(min_length=1, max_length=64)
    item_code: str | None = Field(default=None, max_length=64)
    item_name: str | None = Field(default=None, max_length=256)
    qty: Decimal | None = None
    unit: str | None = Field(default=None, max_length=32)
    unit_price: Decimal | None = None
    amount: Decimal | None = None
    currency: str = Field(default="CNY", min_length=3, max_length=8)
    tax_included: bool = True
    tax_rate: Decimal | None = None
    scrap_qty: Decimal | None = None
    period: str | None = Field(default=None, max_length=32)
    coord: dict[str, Any] | None = None


class SupplierMasterIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str = Field(min_length=1, max_length=64)
    name: str | None = Field(default=None, max_length=256)
    aliases: list[str] = Field(default_factory=list)
    tax_no: str | None = Field(default=None, max_length=64)


class ItemMasterIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str = Field(min_length=1, max_length=64)
    name: str | None = Field(default=None, max_length=256)
    base_unit: str | None = Field(default=None, max_length=32)
    spec: str | None = Field(default=None, max_length=128)


class UnitConversionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    item: str = Field(min_length=1, max_length=64)
    from_unit: str = Field(min_length=1, max_length=32)
    to_unit: str = Field(min_length=1, max_length=32)
    factor: Decimal = Field(gt=0)


class MasterDataIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    suppliers: list[SupplierMasterIn] = Field(default_factory=list)
    items: list[ItemMasterIn] = Field(default_factory=list)
    unit_conversions: list[UnitConversionIn] = Field(default_factory=list)


class ToleranceIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    amount: Decimal | None = None
    price: Decimal | None = None
    qty: Decimal | None = None


class ReconDatasetIn(BaseModel):
    """本次对账的取数结果；缺省时按 ``sources.statement_files`` 找已登记数据集。"""

    model_config = ConfigDict(extra="forbid")

    our_lines: list[ReconLineIn] = Field(default_factory=list)
    supplier_lines: list[ReconLineIn] = Field(default_factory=list)
    master_data: MasterDataIn = Field(default_factory=MasterDataIn)
    tolerance: ToleranceIn | None = None


class ReconSourcesIn(BaseModel):
    """取数来源声明（与 AG-1 入参 C7 的 ``sources`` 对齐）。"""

    model_config = ConfigDict(extra="forbid")

    erp: bool = True
    mes: bool = False
    wms: bool = False
    statement_files: list[str] = Field(default_factory=list)


class ReconRunIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scope: ScopeIn
    suppliers: list[str] = Field(min_length=1, max_length=200)
    sources: ReconSourcesIn = Field(default_factory=ReconSourcesIn)
    dataset: ReconDatasetIn | None = None
    #: 显式重跑：必须指向同租户/同组织/同期间的既有批次，并给出原因。
    rerun_of: UUID | None = None
    rerun_reason: str | None = Field(default=None, min_length=1, max_length=500)

    @model_validator(mode="after")
    def _rerun_needs_reason(self) -> ReconRunIn:
        if self.rerun_of is not None and not self.rerun_reason:
            raise ValueError("显式重跑（rerun_of）必须同时提供 rerun_reason")
        return self


class ReconMetricsOut(BaseModel):
    supplier_lines: int
    matched: int
    matched_rate: str
    ambiguous: int
    candidate_only: int
    unmatched: int
    diff_lines: int
    diff_rate: str
    diff_amount: str
    suspected_anomaly: bool
    auto_disposal_allowed: bool


class ReconBatchOut(BaseModel):
    batch_id: UUID
    status: str
    conclusive: bool
    status_reason: str
    org_id: str
    period: str
    system_ids: list[str]
    suppliers: list[str]
    metrics: ReconMetricsOut | None = None
    missing_items: list[MissingItemOut] = Field(default_factory=list)
    diff_count: int = 0
    created_by: UUID
    created_at: datetime


class ReconBatchSummaryOut(BaseModel):
    """批次列表项。计数与详情同源；``None`` = 旧批次无可读投影，不伪造 0。"""

    batch_id: UUID
    status: str
    line_count: int | None = None
    diff_count: int | None = None
    diff_amount: str | None = None
    created_at: datetime


class ReconLineResultOut(BaseModel):
    item: str | None = None
    qty_mine: str | None = None
    qty_supplier: str | None = None
    amount: MoneyOut | None = None
    match_type: str
    confidence: str
    status: str
    our_ref: str | None = None
    supplier_ref: str | None = None
    needs_manual: bool
    note: str = ""


class ReconDiffOut(BaseModel):
    diff_id: str
    type: str
    amount: MoneyOut
    owner: str
    suggestion: str
    confidence: str
    needs_manual: bool
    billing_bucket: BillingBucket
    ref: str
    supplier_code: str
    evidence: list[EvidenceOut] = Field(default_factory=list)


class DiscrepancyDraftOut(BaseModel):
    draft_no: str
    supplier_code: str
    period: str
    diff_type: str
    amount: MoneyOut
    owner: str
    suggestion: str
    confidence: str
    needs_manual: bool
    refs: list[str]
    status: str


class UnattributedOut(BaseModel):
    ref: str
    amount_diff: MoneyOut
    reason: str
    evidence: list[EvidenceOut] = Field(default_factory=list)


class ReconDiffDetailOut(ReconDiffOut):
    batch_id: UUID
    related_lines: list[ReconLineResultOut] = Field(default_factory=list)
    draft: DiscrepancyDraftOut | None = None
    unattributed: UnattributedOut | None = None


# --------------------------------------------------------------------------- 计量（结果计费）


class MeteringDataGapOut(BaseModel):
    """计量所需但当前数据模型提供不了的字段：如实报缺，不用默认值冒充。"""

    field: str
    reason: str
    #: 谁负责提供（参考取值：对账运营/复核人/客户成功/采购法务…），不是硬编码清单
    owner_role: str
    #: 需要提供什么（可直接转成待办），新增缺口时必须一起给出
    action: str


class MeteringExcludedOut(BaseModel):
    total: int
    misrelease: int | None = None
    dispute_hold: int
    customer_reason: int | None = None
    other_unbillable: int
    other_reasons: dict[str, int] = Field(default_factory=dict)


class MeteringLineOut(BaseModel):
    """按明细行的程序性事实；不判断「结论是否正确」。"""

    line_no: int
    side: Literal["supplier", "our"]
    supplier_code: str | None = None
    our_ref: str | None = None
    supplier_ref: str | None = None
    status: str
    auto_released: bool
    needs_manual: bool
    billing_bucket: BillingBucket
    human_modified: bool | None = None
    ticket_no: str | None = None
    before_ref: str | None = None
    after_ref: str | None = None
    excluded_reason: str = ""
    diff_types: list[str] = Field(default_factory=list)
    owner: str | None = None
    confidence: str
    source_refs: list[str] = Field(default_factory=list)
    evidence: list[EvidenceOut] = Field(default_factory=list)


class MeteringStatementOut(BaseModel):
    """产品文档 07 §2.2.1 计量表字段；金额一律十进制文本。"""

    batch_id: UUID
    batch_status: str
    conclusive: bool
    period: str
    supplier_scope: list[str]
    expected_lines: int
    auto_released_lines: int
    human_reviewed_lines: int | None = None
    confirmed_misrelease_lines: int | None = None
    customer_reason_lines: int | None = None
    billable_lines: int
    billable_basis: str
    excluded_lines: MeteringExcludedOut
    unit_price: str
    unit_price_source: str
    currency: str
    period_amount: str
    prior_cumulative_amount: str
    cumulative_amount: str
    cap_amount: str | None = None
    cap_remaining: str | None = None
    cap_exceeded: bool
    line_accounting: str
    calculation: list[str] = Field(default_factory=list)
    data_gaps: list[MeteringDataGapOut] = Field(default_factory=list)
    computed_at: datetime
    line_count: int
    lines: list[MeteringLineOut] = Field(default_factory=list)


# --------------------------------------------------------------------------- 审计


class AuditImageOut(BaseModel):
    image_id: UUID
    ticket_id: UUID
    step_id: UUID
    system: str
    object_type: str
    object_id: str
    before_ref: str | None = None
    after_ref: str | None = None
    field_diff: list[dict[str, Any]] = Field(default_factory=list)
    diff_missing: bool
    prev_hash: str
    hash: str
    created_at: datetime


class AuditChainOut(BaseModel):
    ticket_id: UUID
    tenant_id: UUID
    ok: bool
    checked: int
    broken_at: int | None = None
    detail: str = ""
    image_count: int
    head_hash: str


__all__ = [
    "ActionIn",
    "ActionOut",
    "ApprovalDecisionIn",
    "ApprovalDecisionOut",
    "ApprovalOut",
    "ApprovalTaskOut",
    "AuditChainOut",
    "AuditImageOut",
    "DiscrepancyDraftOut",
    "ItemMasterIn",
    "MasterDataIn",
    "MeteringDataGapOut",
    "MeteringExcludedOut",
    "MeteringLineOut",
    "MeteringStatementOut",
    "MissingItemOut",
    "MoneyOut",
    "Page",
    "ReconBatchOut",
    "ReconDatasetIn",
    "ReconDiffDetailOut",
    "ReconDiffOut",
    "ReconLineIn",
    "ReconLineResultOut",
    "ReconMetricsOut",
    "ReconRunIn",
    "ReconSourcesIn",
    "ScopeIn",
    "SupplierMasterIn",
    "TicketAction",
    "TicketDetailOut",
    "TicketOut",
    "TicketStepOut",
    "ToleranceIn",
    "UnattributedOut",
    "UnitConversionIn",
]
