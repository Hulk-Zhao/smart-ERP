"""C3 工单状态机（唯一真源）。

12 个状态取自 PRD §5.0 / §6.2，不新增、不改名、不自定义。任何状态推进都必须经过
``assert_transition()``；DB 侧 ``ticket_event`` 触发器用同一份 ``TRANSITIONS``
生成 `(from_state, to_state)` 白名单，保证「不改代码无法产生非法边」。
"""

from __future__ import annotations

from enum import Enum


class TicketState(str, Enum):
    PLANNED = "计划中"
    NEEDS_REVIEW = "待确认"
    PENDING_APPROVAL = "待审批"
    EXECUTING = "执行中"
    VERIFYING = "校验中"
    COMPLETED = "已完成"
    COMPENSATING = "补偿中"
    ROLLED_BACK = "已回滚"
    MANUAL_REQUIRED = "需人工处置"
    LOCKED = "已锁定"
    TERMINATED = "已终止"
    REJECTED = "已驳回"


TRANSITIONS: dict[TicketState, frozenset[TicketState]] = {
    TicketState.PLANNED: frozenset(
        {TicketState.PENDING_APPROVAL, TicketState.TERMINATED, TicketState.NEEDS_REVIEW}
    ),
    TicketState.NEEDS_REVIEW: frozenset(
        {TicketState.PENDING_APPROVAL, TicketState.TERMINATED}
    ),
    TicketState.PENDING_APPROVAL: frozenset(
        {TicketState.EXECUTING, TicketState.REJECTED, TicketState.TERMINATED}
    ),
    TicketState.EXECUTING: frozenset(
        {TicketState.VERIFYING, TicketState.COMPENSATING, TicketState.LOCKED}
    ),
    TicketState.VERIFYING: frozenset({TicketState.COMPLETED, TicketState.COMPENSATING}),
    TicketState.COMPENSATING: frozenset(
        {TicketState.ROLLED_BACK, TicketState.MANUAL_REQUIRED}
    ),
    TicketState.COMPLETED: frozenset(),
    TicketState.ROLLED_BACK: frozenset(),
    TicketState.MANUAL_REQUIRED: frozenset(),
    TicketState.LOCKED: frozenset(),
    TicketState.TERMINATED: frozenset(),
    TicketState.REJECTED: frozenset(),
}

TICKET_STATES: tuple[str, ...] = tuple(state.value for state in TicketState)

TRANSITION_EDGES: frozenset[tuple[str, str]] = frozenset(
    (source.value, target.value)
    for source, targets in TRANSITIONS.items()
    for target in targets
)

# ticket_step 的步骤状态（§3.1.2）
STEP_STATES: tuple[str, ...] = (
    "intent",
    "running",
    "succeeded",
    "failed",
    "skipped",
    "compensating",
    "compensated",
    "unresolved",
)


class IllegalTransition(Exception):
    """非法状态迁移（E-STATE-ILLEGAL）。"""


def assert_transition(current: TicketState | str, target: TicketState | str) -> None:
    source = TicketState(current)
    nxt = TicketState(target)
    if nxt not in TRANSITIONS[source]:
        raise IllegalTransition(f"{source.value} -> {nxt.value} 不在迁移白名单内")


def state_contract() -> dict[str, object]:
    """生成 ``contracts/ticket/state_machine.v1.json`` 的内容。"""

    return {
        "contract": "ticket-state-machine",
        "version": "1.0",
        "states": list(TICKET_STATES),
        "terminal_states": [
            state.value for state, targets in TRANSITIONS.items() if not targets
        ],
        "transitions": [
            {"from": source, "to": target}
            for source, target in sorted(TRANSITION_EDGES)
        ],
        "step_states": list(STEP_STATES),
    }
