"""事件溯源纯函数：重放 = 当前状态、哈希链、断链与篡改检测。"""

from __future__ import annotations

from dataclasses import replace

import pytest

from apps.agents.events import (
    CREATED_EVENT,
    GENESIS_HASH,
    STEP_EVENT,
    TRANSITION_EVENT,
    EventRecord,
    ReplayError,
    make_event,
    replay,
    replay_state,
)


def _chain() -> list[EventRecord]:
    created = make_event(None, event_type=CREATED_EVENT, to_state="计划中", payload={"scene_code": "AG-1"})
    pending = make_event(
        created,
        event_type=TRANSITION_EVENT,
        from_state="计划中",
        to_state="待审批",
        payload={"trigger": "preview_ok", "context": {"preview_ok": True}},
    )
    executing = make_event(
        pending,
        event_type=TRANSITION_EVENT,
        from_state="待审批",
        to_state="执行中",
        payload={"trigger": "approved", "context": {"approved": True}},
    )
    step = make_event(
        executing,
        event_type=STEP_EVENT,
        payload={"step_no": 1, "state": "succeeded", "action_key": "fake.draft.create"},
    )
    verifying = make_event(
        step,
        event_type=TRANSITION_EVENT,
        from_state="执行中",
        to_state="校验中",
        payload={"trigger": "all_ok", "context": {"step_states": ["succeeded"]}},
    )
    completed = make_event(
        verifying,
        event_type=TRANSITION_EVENT,
        from_state="校验中",
        to_state="已完成",
        payload={"trigger": "verified", "context": {"verified": True, "step_states": ["succeeded"]}},
    )
    return [created, pending, executing, step, verifying, completed]


def test_replay_reconstructs_current_state() -> None:
    projection = replay(_chain())
    assert projection.state.value == "已完成"
    assert projection.version == 4
    assert projection.steps == {1: "succeeded"}
    assert projection.last_seq == 6
    assert projection.terminal is True


def test_hash_chain_links_each_event() -> None:
    events = _chain()
    assert events[0].prev_hash == GENESIS_HASH
    for previous, current in zip(events, events[1:]):
        assert current.prev_hash == previous.hash
        assert current.hash == current.computed_hash


def test_tampered_payload_is_detected() -> None:
    events = _chain()
    events[2] = replace(events[2], payload={**events[2].payload, "trigger": "hacked"})
    with pytest.raises(ReplayError, match="哈希校验失败"):
        replay(events)


def test_gap_in_sequence_is_detected() -> None:
    events = _chain()
    del events[1]
    with pytest.raises(ReplayError, match="序号不连续"):
        replay(events)


def test_state_chain_break_is_detected() -> None:
    events = _chain()
    events[2] = replace(
        events[2],
        from_state="待确认",
    )
    # 重新封口，保证失败原因只可能是状态链断裂，而不是哈希
    events[2] = events[2].seal()
    with pytest.raises(ReplayError):
        replay(events)


def test_replay_rechecks_guards_when_context_present() -> None:
    created = make_event(None, event_type=CREATED_EVENT, to_state="计划中")
    terminated = make_event(
        created,
        event_type=TRANSITION_EVENT,
        from_state="计划中",
        to_state="已终止",
        payload={"trigger": "preview_failed", "context": {"writes": 3}},
    )
    with pytest.raises(ReplayError, match="状态迁移非法"):
        replay([created, terminated])


def test_illegal_edge_is_rejected_even_without_context() -> None:
    created = make_event(None, event_type=CREATED_EVENT, to_state="计划中")
    jumped = make_event(
        created,
        event_type=TRANSITION_EVENT,
        from_state="计划中",
        to_state="已完成",
        payload={"trigger": "done"},
    )
    with pytest.raises(ReplayError, match="状态迁移非法"):
        replay([created, jumped])


def test_empty_sequence_rejected() -> None:
    with pytest.raises(ReplayError):
        replay([])
    with pytest.raises(ReplayError):
        replay_state([])
