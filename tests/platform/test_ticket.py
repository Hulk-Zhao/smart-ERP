"""C3 状态机：12 状态、白名单迁移、终态不可再迁。"""

from __future__ import annotations

import pytest

from apps.platform.contracts.ticket import (
    TRANSITION_EDGES,
    TRANSITIONS,
    IllegalTransition,
    TicketState,
    assert_transition,
)


def test_exactly_twelve_states() -> None:
    assert len(TicketState) == 12
    assert len({state.value for state in TicketState}) == 12


def test_legal_transition() -> None:
    assert_transition(TicketState.PLANNED, TicketState.PENDING_APPROVAL)
    assert_transition("执行中", "校验中")
    assert_transition("校验中", "补偿中")


def test_illegal_transition_rejected() -> None:
    with pytest.raises(IllegalTransition):
        assert_transition(TicketState.PLANNED, TicketState.COMPLETED)
    with pytest.raises(IllegalTransition):
        assert_transition(TicketState.COMPLETED, TicketState.EXECUTING)


def test_terminal_states_have_no_outgoing_edges() -> None:
    terminals = {state for state, targets in TRANSITIONS.items() if not targets}
    assert terminals == {
        TicketState.COMPLETED,
        TicketState.ROLLED_BACK,
        TicketState.MANUAL_REQUIRED,
        TicketState.LOCKED,
        TicketState.TERMINATED,
        TicketState.REJECTED,
    }


def test_edges_only_reference_known_states() -> None:
    known = {state.value for state in TicketState}
    for source, target in TRANSITION_EDGES:
        assert source in known
        assert target in known
