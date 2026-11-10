"""12 态状态机：转移表与契约一一对应，非法迁移必须带明确错误码。"""

from __future__ import annotations

from decimal import Decimal

import pytest

from apps.platform.contracts.errors import ErrorCode
from apps.platform.contracts.ticket import TRANSITION_EDGES, TicketState

from apps.agents.state_machine import (
    TRANSITION_RULES,
    GuardViolationError,
    IllegalTransitionError,
    TransitionContext,
    is_terminal,
    legal_targets,
    resolve_transition,
    transition_contract,
)


def test_runtime_table_matches_contract_edges() -> None:
    derived = {
        (source.value, target.value) for (source, target) in TRANSITION_RULES
    }
    assert derived == set(TRANSITION_EDGES)


def test_state_names_follow_prd() -> None:
    expected = {
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
    }
    assert {state.value for state in TicketState} == expected
    snapshot = transition_contract()
    assert snapshot["states"] == [state.value for state in TicketState]


def test_terminal_states_have_no_outgoing_edges() -> None:
    terminal = {state.value for state in TicketState if is_terminal(state)}
    assert terminal == {"已完成", "已回滚", "需人工处置", "已锁定", "已终止", "已驳回"}
    for state in terminal:
        assert legal_targets(state) == ()


def test_illegal_edge_raises_state_illegal() -> None:
    with pytest.raises(IllegalTransitionError) as excinfo:
        resolve_transition("计划中", "已完成", "preview_ok")
    assert excinfo.value.code is ErrorCode.STATE_ILLEGAL
    assert "计划中 -> 已完成" in str(excinfo.value)


def test_unknown_trigger_on_legal_edge_rejected() -> None:
    with pytest.raises(IllegalTransitionError) as excinfo:
        resolve_transition("计划中", "待审批", "approved")
    assert "不接受触发" in str(excinfo.value)


def test_terminated_requires_zero_writes() -> None:
    resolve_transition(
        "计划中", "已终止", "preview_failed", TransitionContext(writes=0)
    )
    with pytest.raises(GuardViolationError) as excinfo:
        resolve_transition(
            "计划中", "已终止", "preview_failed", TransitionContext(writes=1)
        )
    assert "零写入" in str(excinfo.value)


def test_deviation_over_five_percent_goes_to_review() -> None:
    resolve_transition(
        "计划中",
        "待确认",
        "deviation_gt_5pct",
        TransitionContext(deviation_ratio=Decimal("0.051")),
    )
    with pytest.raises(GuardViolationError):
        resolve_transition(
            "计划中",
            "待确认",
            "deviation_gt_5pct",
            TransitionContext(deviation_ratio=Decimal("0.05")),
        )
    with pytest.raises(GuardViolationError):
        resolve_transition("计划中", "待确认", "deviation_gt_5pct")


def test_approval_required_before_executing() -> None:
    with pytest.raises(GuardViolationError):
        resolve_transition("待审批", "执行中", "approved", TransitionContext())
    resolve_transition(
        "待审批", "执行中", "approved", TransitionContext(approved=True)
    )


def test_all_steps_must_succeed_before_verifying() -> None:
    with pytest.raises(GuardViolationError):
        resolve_transition(
            "执行中",
            "校验中",
            "all_ok",
            TransitionContext(step_states=("succeeded", "failed")),
        )
    with pytest.raises(GuardViolationError):
        resolve_transition("执行中", "校验中", "all_ok", TransitionContext())
    resolve_transition(
        "执行中",
        "校验中",
        "all_ok",
        TransitionContext(step_states=("succeeded", "skipped")),
    )


def test_only_f8_can_lock_and_only_f6_mismatch_compensates() -> None:
    resolve_transition(
        "执行中", "已锁定", "external_effective", TransitionContext(fail_code="F8")
    )
    with pytest.raises(GuardViolationError):
        resolve_transition(
            "执行中", "已锁定", "external_effective", TransitionContext(fail_code="F5")
        )
    with pytest.raises(GuardViolationError):
        resolve_transition(
            "校验中", "补偿中", "mismatch", TransitionContext(fail_code="F5")
        )
    resolve_transition(
        "校验中", "补偿中", "mismatch", TransitionContext(fail_code="F6")
    )


def test_compensation_outcomes_are_disjoint() -> None:
    resolve_transition(
        "补偿中",
        "已回滚",
        "all_compensated",
        TransitionContext(compensation_failed=0),
    )
    with pytest.raises(GuardViolationError):
        resolve_transition(
            "补偿中",
            "已回滚",
            "all_compensated",
            TransitionContext(compensation_failed=2),
        )
    with pytest.raises(GuardViolationError):
        resolve_transition(
            "补偿中",
            "需人工处置",
            "compensation_failed",
            TransitionContext(compensation_failed=0),
        )
    resolve_transition(
        "补偿中",
        "需人工处置",
        "compensation_failed",
        TransitionContext(compensation_failed=2),
    )


def test_context_from_event_payload_coerces_types() -> None:
    context = TransitionContext.from_mapping(
        {"deviation_ratio": "0.09", "step_states": ["succeeded"], "writes": 0}
    )
    assert context.deviation_ratio == Decimal("0.09")
    assert context.step_states == ("succeeded",)
