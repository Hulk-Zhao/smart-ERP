"""C7 Agent 输入输出契约（Pydantic v2，Schema 与代码同源）。

硬约束（PRD §5.0 契约约定）：
- 入参必含 ``scope{org_id, period, system_ids}``，缺任一字段直接拒绝（G-05）；
- 金额一律对象化 ``Money{amount, currency, tax_included}``，禁止裸数字；
- 置信度 ``0.00–1.00``，**低于 0.70 视为不可用**，不得作为结论输出；
- 出参必含 ``ticket_id``、``status``、``evidence[]``（至少一条）。

输入侧 ``additionalProperties: false``（防注入），输出侧宽容新增字段。
"""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated, Any, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
)

from apps.platform.contracts.canonical import Evidence, Money

USABLE_CONFIDENCE_MIN = 0.70

ORG_ID = Annotated[str, StringConstraints(min_length=1)]
PERIOD = Annotated[str, StringConstraints(pattern=r"^[0-9]{4}-(0[1-9]|1[0-2])$")]
TICKET_ID = Annotated[str, StringConstraints(pattern=r"^AT-[0-9]{8}-[0-9]{4}$")]
NON_EMPTY = Annotated[str, StringConstraints(min_length=1)]


def _require_usable(value: float) -> float:
    if value < USABLE_CONFIDENCE_MIN:
        raise ValueError(
            f"confidence {value} < {USABLE_CONFIDENCE_MIN}：视为不可用，不得作为结论输出"
        )
    return value


Confidence = Annotated[float, Field(ge=0.0, le=1.0), AfterValidator(_require_usable)]

TicketStatus = Literal[
    "计划中",
    "待确认",
    "待审批",
    "执行中",
    "校验中",
    "已完成",
    "补偿中",
    "已回滚",
    "需人工处置",
    "已锁定",
    "已终止",
    "已驳回",
]


class Scope(BaseModel):
    """执行范围（G-05）。三个字段都必填，没有默认值。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    org_id: ORG_ID
    period: PERIOD
    system_ids: list[NON_EMPTY] = Field(min_length=1)


class AgentInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    scope: Scope


class AgentOutput(BaseModel):
    """所有 Agent 共用的外层信封。"""

    model_config = ConfigDict(extra="allow", frozen=True)

    ticket_id: TICKET_ID
    status: TicketStatus
    evidence: list[Evidence] = Field(min_length=1)


class ReconcileSources(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    erp: NON_EMPTY
    mes: NON_EMPTY | None = None
    wms: NON_EMPTY | None = None
    statement_files: list[str] = Field(default_factory=list)


class Tolerance(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    amount: Annotated[str, StringConstraints(pattern=r"^\d+(\.\d{1,4})?$")] | None = None
    price: Annotated[str, StringConstraints(pattern=r"^\d+(\.\d{1,4})?$")] | None = None
    qty: Annotated[float, Field(ge=0)] | None = None


class ReconcileOptions(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tolerance: Tolerance | None = None
    auto_write: bool = False
    batch_size: int = Field(default=100, ge=1, le=100)


class AG1ReconcileInput(AgentInput):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        json_schema_extra={"$id": "https://contracts/agent/ag1.reconcile.input/1.0"},
    )

    suppliers: list[NON_EMPTY] = Field(min_length=1)
    sources: ReconcileSources
    options: ReconcileOptions = Field(default_factory=ReconcileOptions)


MatchType = Literal["exact", "fuzzy", "candidate", "unmatched"]
DiffType = Literal[
    "单价不一致", "数量不符", "报废未扣", "单位换算", "税率口径", "缺单", "重复计费"
]
DiffOwner = Literal["ours", "supplier", "system"]


class ReconcileLine(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    item: NON_EMPTY
    qty_mine: Decimal | None = None
    qty_supplier: Decimal | None = None
    amount: Money
    match_type: MatchType
    confidence: Confidence | None = None


class ReconcileDiff(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    type: DiffType
    amount: Money
    owner: DiffOwner
    suggestion: str
    confidence: Confidence


class PlannedAction(BaseModel):
    """Agent 只能提出 W0/W1；更高等级必须由人走审批，不在输出契约里。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    system: NON_EMPTY
    action: NON_EMPTY
    level: Literal["W0", "W1"]


class AG1ReconcileOutput(AgentOutput):
    model_config = ConfigDict(
        extra="allow",
        frozen=True,
        json_schema_extra={"$id": "https://contracts/agent/ag1.reconcile.output/1.0"},
    )

    batch_id: NON_EMPTY
    lines: list[ReconcileLine]
    diffs: list[ReconcileDiff]
    actions: list[PlannedAction]
    metrics: dict[str, Any]


AGENT_CONTRACTS: dict[str, type[BaseModel]] = {
    "ag1.reconcile.input.v1": AG1ReconcileInput,
    "ag1.reconcile.output.v1": AG1ReconcileOutput,
}
