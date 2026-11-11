"""U8 连接器动作声明（C2：等级、字段、补偿、校验、限流全部写在代码里）。

两个正向动作都是**草稿类**写入（W1）：U8 里可删除、不进总账、不需要审批凭证。
资金/过账类动作（付款单过账、凭证记账）不在 SDK 里声明，注册期就会被
``FORBIDDEN_ACTION_VERBS`` 与 ``CRITICAL_READONLY_FIELDS`` 拦掉。

同时声明两个**反向动作**（删除草稿，W0，不高于原动作）：回滚链路
（``RollbackService``）只经 ``WriteGateway`` 回写，因此「可回滚」必须表现为
一个真实可执行的动作，而不是连接器内部不可见的旁路。
"""

from __future__ import annotations

from apps.platform.policy.levels import (
    ActionSpec,
    CompensateSpec,
    RateLimit,
    VerifySpec,
    WriteLevel,
)

PO_DRAFT_KEY = "u8.po.create_draft"
ARRIVAL_DRAFT_KEY = "u8.arrival.create_draft"
PO_DELETE_KEY = "u8.po.delete_draft"
ARRIVAL_DELETE_KEY = "u8.arrival.delete_draft"

# 两个动作的载荷形状一致（工单侧字段，snake_case），由连接器映射成 U8 报文。
_DRAFT_FIELDS = frozenset({"source_doc_no", "supplier_code", "doc_date", "lines", "memo"})

PO_CREATE_DRAFT = ActionSpec(
    key=PO_DRAFT_KEY,
    level=WriteLevel.W1,
    writable_fields=_DRAFT_FIELDS,
    compensation=CompensateSpec(kind="delete_draft", action_key=PO_DELETE_KEY),
    verify=VerifySpec(
        kind="read_po_draft_by_source",
        fields=("source_doc_no", "supplier_code", "doc_date", "line_count"),
    ),
    rate_limit=RateLimit(qps=2.0, daily_quota=500, concurrency=2),
)

ARRIVAL_CREATE_DRAFT = ActionSpec(
    key=ARRIVAL_DRAFT_KEY,
    level=WriteLevel.W1,
    writable_fields=_DRAFT_FIELDS,
    compensation=CompensateSpec(kind="delete_draft", action_key=ARRIVAL_DELETE_KEY),
    verify=VerifySpec(
        kind="read_arrival_draft_by_source",
        fields=("source_doc_no", "supplier_code", "doc_date", "line_count"),
    ),
    rate_limit=RateLimit(qps=1.0, daily_quota=300, concurrency=1),
)

# 反向动作：按 U8 单号（cCode）删除草稿；W0 不触发审批，但同样过闸门与字段校验。
PO_DELETE_DRAFT = ActionSpec(
    key=PO_DELETE_KEY,
    level=WriteLevel.W0,
    writable_fields=frozenset({"cCode"}),
    verify=VerifySpec(kind="read_po_draft_by_code", fields=("cCode",)),
    rate_limit=RateLimit(qps=2.0, daily_quota=500, concurrency=2),
)

ARRIVAL_DELETE_DRAFT = ActionSpec(
    key=ARRIVAL_DELETE_KEY,
    level=WriteLevel.W0,
    writable_fields=frozenset({"cCode"}),
    verify=VerifySpec(kind="read_arrival_draft_by_code", fields=("cCode",)),
    rate_limit=RateLimit(qps=1.0, daily_quota=300, concurrency=1),
)

ACTIONS: tuple[ActionSpec, ...] = (
    PO_CREATE_DRAFT,
    ARRIVAL_CREATE_DRAFT,
    PO_DELETE_DRAFT,
    ARRIVAL_DELETE_DRAFT,
)
ACTIONS_BY_KEY = {action.key: action for action in ACTIONS}
DELETE_ACTIONS: dict[str, str] = {
    PO_DELETE_KEY: "purchase-order-drafts",
    ARRIVAL_DELETE_KEY: "arrival-drafts",
}

# 读接口的限流键（连接器级预算，不是动作级）。
READ_LIMIT_KEY = "u8.read"
