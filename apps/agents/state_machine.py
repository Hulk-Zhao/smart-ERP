"""12 态工单状态机：显式转移表 + 守卫条件（§3.1.1）。

契约（``apps.platform.contracts.ticket``）只给出「边」的白名单；运行时在其上补齐
**触发名与校验条件**，任何推进都必须同时满足：

1. 边存在于 :data:`TRANSITION_RULES`（与 C3 的 ``TRANSITION_EDGES`` 完全一致，单测比对）；
2. 触发名在该边上登记（防止「同一条边被不同语义复用」）；
3. 守卫条件全部通过（例如「已终止必须零写入」「执行中→校验中必须所有步骤成功」）。

失败一律抛 :class:`TransitionError`，对外错误码固定 ``E-STATE-ILLEGAL``。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Callable, Mapping

from apps.platform.contracts.errors import ErrorCode
from apps.platform.contracts.ticket import TRANSITION_EDGES, TicketState

# 偏差阈值（PRD §5.0：偏差 > 5% 转人工确认）
DEVIATION_THRESHOLD = Decimal("0.05")

SUCCEEDED_STEP_STATES = frozenset({"succeeded", "skipped"})
# 已经产生过外部写入时，任何一步失败都必须补偿（F1–F7）；F8 走「已锁定」而非补偿。
COMPENSATE_FAIL_CODES = frozenset({"F1", "F2", "F3", "F4", "F5", "F6", "F7"})


class TransitionError(Exception):
    """状态机拒绝推进。``code`` 固定 E-STATE-ILLEGAL（C10）。"""

    code: ErrorCode = ErrorCode.STATE_ILLEGAL

    def __init__(self, message: str, *, current: str, target: str, trigger: str) -> None:
        super().__init__(message)
        self.current = current
        self.target = target
        self.trigger = trigger


class IllegalTransitionError(TransitionError):
    """当前态 × 目标态不在白名单内。"""


class GuardViolationError(TransitionError):
    """边合法但校验条件不满足。"""


@dataclass(frozen=True, slots=True)
class TransitionContext:
    """推进所需的业务事实。守卫只读这里，不读数据库。"""

    preview_ok: bool = False
    approved: bool = False
    verified: bool = False
    deviation_ratio: Decimal | None = None
    writes: int = 0
    fail_code: str | None = None
    step_states: tuple[str, ...] = ()
    compensation_failed: int = 0

    @classmethod
    def from_mapping(cls, values: Mapping[str, object]) -> "TransitionContext":
        known = {field for field in cls.__dataclass_fields__}
        data = {key: value for key, value in values.items() if key in known}
        if isinstance(data.get("deviation_ratio"), str):
            data["deviation_ratio"] = Decimal(data["deviation_ratio"])  # type: ignore[arg-type]
        if isinstance(data.get("step_states"), list):
            data["step_states"] = tuple(data["step_states"])  # type: ignore[arg-type]
        return cls(**data)  # type: ignore[arg-type]

    @classmethod
    def of_steps(cls, states: tuple[str, ...], **kwargs: object) -> "TransitionContext":
        return cls(step_states=states, **kwargs)  # type: ignore[arg-type]


Guard = Callable[[TransitionContext], str | None]


@dataclass(frozen=True, slots=True)
class TransitionRule:
    trigger: str
    guard: Guard
    fail_codes: frozenset[str] = frozenset()
    note: str = ""


def _allow(_: TransitionContext) -> str | None:
    return None


def _need_preview_ok(ctx: TransitionContext) -> str | None:
    return None if ctx.preview_ok else "预演未通过，不能进入待审批"


def _zero_writes(ctx: TransitionContext) -> str | None:
    if ctx.writes != 0:
        return f"已终止要求零写入，实际已写入 {ctx.writes} 次"
    return None


def _deviation_exceeded(ctx: TransitionContext) -> str | None:
    if ctx.deviation_ratio is None:
        return "缺少偏差比例，不能判定是否转人工确认"
    if ctx.deviation_ratio <= DEVIATION_THRESHOLD:
        return f"偏差 {ctx.deviation_ratio} 未超过 {DEVIATION_THRESHOLD}"
    return None


def _need_approved(ctx: TransitionContext) -> str | None:
    return None if ctx.approved else "审批未满足，不能进入执行中"


def _all_steps_ok(ctx: TransitionContext) -> str | None:
    if not ctx.step_states:
        return "没有任何步骤，不能进入校验中"
    pending = [state for state in ctx.step_states if state not in SUCCEEDED_STEP_STATES]
    if pending:
        return f"仍有非成功步骤: {pending}"
    return None


def _need_verified(ctx: TransitionContext) -> str | None:
    return None if ctx.verified else "写入后校验未通过，不能标记已完成"


def _compensated(ctx: TransitionContext) -> str | None:
    if ctx.compensation_failed:
        return f"仍有 {ctx.compensation_failed} 个补偿任务失败，不能进入已回滚"
    return None


def _compensation_failed(ctx: TransitionContext) -> str | None:
    if not ctx.compensation_failed:
        return "没有失败的补偿任务，不应转需人工处置"
    return None


def _fail_code_in(allowed: frozenset[str]) -> Guard:
    def guard(ctx: TransitionContext) -> str | None:
        if ctx.fail_code not in allowed:
            return f"失败分类 {ctx.fail_code!r} 不允许走这条边（允许 {sorted(allowed)}）"
        return None

    return guard


TRANSITION_RULES: Mapping[tuple[TicketState, TicketState], tuple[TransitionRule, ...]] = {
    (TicketState.PLANNED, TicketState.PENDING_APPROVAL): (
        TransitionRule("preview_ok", _need_preview_ok, note="预演通过（含前像）"),
    ),
    (TicketState.PLANNED, TicketState.TERMINATED): (
        TransitionRule("preview_failed", _zero_writes, note="预演失败，零写入"),
    ),
    (TicketState.PLANNED, TicketState.NEEDS_REVIEW): (
        TransitionRule("deviation_gt_5pct", _deviation_exceeded, note="偏差 > 5% 转人工"),
    ),
    (TicketState.NEEDS_REVIEW, TicketState.PENDING_APPROVAL): (
        TransitionRule("review_confirmed", _allow, note="人工确认后进入审批"),
    ),
    (TicketState.NEEDS_REVIEW, TicketState.TERMINATED): (
        TransitionRule("review_terminated", _zero_writes, note="人工确认阶段终止"),
    ),
    (TicketState.PENDING_APPROVAL, TicketState.EXECUTING): (
        TransitionRule("approved", _need_approved, note="审批通过"),
    ),
    (TicketState.PENDING_APPROVAL, TicketState.REJECTED): (
        TransitionRule("rejected", _allow, note="审批驳回"),
    ),
    (TicketState.PENDING_APPROVAL, TicketState.TERMINATED): (
        TransitionRule("approval_terminated", _zero_writes, note="审批前终止"),
    ),
    (TicketState.EXECUTING, TicketState.VERIFYING): (
        TransitionRule("all_ok", _all_steps_ok, note="全部步骤成功"),
    ),
    (TicketState.EXECUTING, TicketState.COMPENSATING): (
        TransitionRule(
            "step_failed",
            _fail_code_in(COMPENSATE_FAIL_CODES),
            fail_codes=COMPENSATE_FAIL_CODES,
            note="单步失败且重试耗尽 / 写入后校验不一致",
        ),
    ),
    (TicketState.EXECUTING, TicketState.LOCKED): (
        TransitionRule(
            "external_effective",
            _fail_code_in(frozenset({"F8"})),
            fail_codes=frozenset({"F8"}),
            note="外部已生效，不再改数据",
        ),
    ),
    (TicketState.VERIFYING, TicketState.COMPLETED): (
        TransitionRule("verified", _need_verified, note="校验通过"),
    ),
    (TicketState.VERIFYING, TicketState.COMPENSATING): (
        TransitionRule(
            "mismatch",
            _fail_code_in(frozenset({"F6"})),
            fail_codes=frozenset({"F6"}),
            note="写入值不符，立即补偿",
        ),
    ),
    (TicketState.COMPENSATING, TicketState.ROLLED_BACK): (
        TransitionRule("all_compensated", _compensated, note="全部补偿成功"),
    ),
    (TicketState.COMPENSATING, TicketState.MANUAL_REQUIRED): (
        TransitionRule("compensation_failed", _compensation_failed, note="任一补偿失败"),
    ),
}

assert set(TRANSITION_RULES) == set(TRANSITION_EDGES), (
    "运行时转移表与 C3 契约边不一致：以 apps.platform.contracts.ticket 为准"
)


def legal_targets(current: TicketState | str) -> tuple[TicketState, ...]:
    state = TicketState(current)
    return tuple(
        target for (source, target) in TRANSITION_RULES if source is state
    )


def is_terminal(state: TicketState | str) -> bool:
    return not legal_targets(state)


def resolve_transition(
    current: TicketState | str,
    target: TicketState | str,
    trigger: str,
    context: TransitionContext | None = None,
) -> TransitionRule:
    """校验并返回命中的规则；不修改任何数据。"""

    source = TicketState(current)
    nxt = TicketState(target)
    rules = TRANSITION_RULES.get((source, nxt))
    if rules is None:
        raise IllegalTransitionError(
            f"非法状态迁移: {source.value} -> {nxt.value}（不在迁移白名单内）",
            current=source.value,
            target=nxt.value,
            trigger=trigger,
        )

    rule = next((item for item in rules if item.trigger == trigger), None)
    if rule is None:
        allowed = sorted(item.trigger for item in rules)
        raise IllegalTransitionError(
            f"状态迁移 {source.value} -> {nxt.value} 不接受触发 {trigger!r}（允许 {allowed}）",
            current=source.value,
            target=nxt.value,
            trigger=trigger,
        )

    violation = rule.guard(context or TransitionContext())
    if violation is not None:
        raise GuardViolationError(
            f"状态迁移 {source.value} -> {nxt.value} 校验不通过：{violation}",
            current=source.value,
            target=nxt.value,
            trigger=trigger,
        )
    return rule


def transition_contract() -> dict[str, object]:
    """供契约测试/评审查看的转移表快照。"""

    return {
        "states": [state.value for state in TicketState],
        "terminal_states": [state.value for state in TicketState if is_terminal(state)],
        "transitions": [
            {
                "from": source.value,
                "to": target.value,
                "triggers": [
                    {
                        "trigger": rule.trigger,
                        "fail_codes": sorted(rule.fail_codes),
                        "note": rule.note,
                    }
                    for rule in rules
                ],
            }
            for (source, target), rules in TRANSITION_RULES.items()
        ],
    }
