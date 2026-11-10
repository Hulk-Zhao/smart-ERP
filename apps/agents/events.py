"""``ticket_event`` 事件溯源：追加写、哈希链、纯函数重放与投影重建（§3.1.1）。

- ``ticket_event`` 是唯一真源，只 INSERT；``ticket`` 是它的投影；
- 每条事件的 ``hash = sha256(prev_hash + canonical(body))``，``prev_hash`` 指向上一条；
- :func:`replay` 不碰数据库，可对任意事件序列验证「重放结果 = 当前状态」，
  同时发现篡改（哈希不符）与断链（from_state 与上一态不一致）。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from apps.agents.state_machine import (
    TransitionContext,
    TransitionError,
    resolve_transition,
)
from apps.platform.contracts.errors import ErrorCode
from apps.platform.contracts.ticket import (
    IllegalTransition as ContractIllegalTransition,
)
from apps.platform.contracts.ticket import TicketState, assert_transition

GENESIS_HASH = "0" * 64
CREATED_EVENT = "created"
TRANSITION_EVENT = "transition"
STEP_EVENT = "step"
COMPENSATION_EVENT = "compensation"


class ReplayError(Exception):
    """事件序列不可重放（哈希断裂 / 序号不连续 / 状态链断裂）。"""

    code = ErrorCode.STATE_ILLEGAL


def _canonical(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        default=str,
    )


def compute_hash(
    prev_hash: str,
    *,
    seq: int,
    event_type: str,
    from_state: str | None,
    to_state: str | None,
    payload: Mapping[str, Any],
    trace_id: str | None,
) -> str:
    body = _canonical(
        {
            "seq": seq,
            "event_type": event_type,
            "from_state": from_state,
            "to_state": to_state,
            "payload": dict(payload),
            "trace_id": trace_id,
        }
    )
    return hashlib.sha256(f"{prev_hash}\n{body}".encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class EventRecord:
    """一条事件的只读视图（DB 行与内存构造共用）。"""

    seq: int
    event_type: str
    from_state: str | None
    to_state: str | None
    payload: Mapping[str, Any] = field(default_factory=dict)
    actor_id: str | None = None
    trace_id: str | None = None
    prev_hash: str = GENESIS_HASH
    hash: str = ""

    @property
    def computed_hash(self) -> str:
        return compute_hash(
            self.prev_hash,
            seq=self.seq,
            event_type=self.event_type,
            from_state=self.from_state,
            to_state=self.to_state,
            payload=self.payload,
            trace_id=self.trace_id,
        )

    def seal(self) -> EventRecord:
        """补齐 hash（构造新事件时用）。"""

        return EventRecord(
            seq=self.seq,
            event_type=self.event_type,
            from_state=self.from_state,
            to_state=self.to_state,
            payload=self.payload,
            actor_id=self.actor_id,
            trace_id=self.trace_id,
            prev_hash=self.prev_hash,
            hash=self.computed_hash,
        )


def make_event(
    prev: EventRecord | None,
    *,
    event_type: str,
    from_state: str | None = None,
    to_state: str | None = None,
    payload: Mapping[str, Any] | None = None,
    actor_id: str | None = None,
    trace_id: str | None = None,
) -> EventRecord:
    """基于上一条事件生成下一条（序号 +1、prev_hash 链接、hash 封口）。"""

    seq = 1 if prev is None else prev.seq + 1
    prev_hash = GENESIS_HASH if prev is None else prev.hash
    return EventRecord(
        seq=seq,
        event_type=event_type,
        from_state=from_state,
        to_state=to_state,
        payload=dict(payload or {}),
        actor_id=actor_id,
        trace_id=trace_id,
        prev_hash=prev_hash,
    ).seal()


@dataclass(frozen=True, slots=True)
class Projection:
    """由事件折叠出的工单投影。"""

    state: TicketState
    version: int
    last_seq: int
    last_hash: str
    steps: Mapping[int, str] = field(default_factory=dict)

    @property
    def terminal(self) -> bool:
        from apps.agents.state_machine import is_terminal

        return is_terminal(self.state)


def replay(events: Iterable[EventRecord], *, verify_hashes: bool = True) -> Projection:
    """按 seq 升序折叠事件；不改数据、不依赖 DB。"""

    ordered = sorted(events, key=lambda item: item.seq)
    if not ordered:
        raise ReplayError("没有任何事件，无法重放")

    state: TicketState | None = None
    version = 0
    prev_hash = GENESIS_HASH
    expected_seq = 1
    steps: dict[int, str] = {}

    for event in ordered:
        if event.seq != expected_seq:
            raise ReplayError(
                f"事件序号不连续：期望 {expected_seq}，实际 {event.seq}"
            )
        expected_seq += 1

        if event.prev_hash != prev_hash:
            raise ReplayError(f"seq={event.seq} 的 prev_hash 与上一条不衔接（断链）")
        if verify_hashes and event.hash and event.computed_hash != event.hash:
            raise ReplayError(f"seq={event.seq} 的哈希校验失败（事件被篡改）")
        prev_hash = event.hash or event.computed_hash

        if event.event_type == CREATED_EVENT:
            if state is not None:
                raise ReplayError("created 事件只能出现一次")
            state = TicketState(event.to_state or TicketState.PLANNED.value)
            version = 0
            continue

        if event.event_type == TRANSITION_EVENT:
            if state is None:
                raise ReplayError(f"seq={event.seq} 在 created 之前出现迁移事件")
            if event.from_state != state.value:
                raise ReplayError(
                    f"seq={event.seq} 的 from_state={event.from_state!r} 与当前投影 "
                    f"{state.value!r} 不一致（状态链断裂）"
                )
            target = event.to_state or ""
            trigger = str(event.payload.get("trigger", ""))
            context_payload = event.payload.get("context")
            try:
                if isinstance(context_payload, Mapping):
                    resolve_transition(
                        state, target, trigger, TransitionContext.from_mapping(context_payload)
                    )
                else:
                    # 历史事件没有上下文快照时，只校验边合法性（DB 触发器同口径）
                    assert_transition(state, target)
            except (TransitionError, ContractIllegalTransition, ValueError) as exc:
                raise ReplayError(f"seq={event.seq} 的状态迁移非法：{exc}") from exc
            state = TicketState(target)
            version += 1
            continue

        if event.event_type == STEP_EVENT:
            step_no = event.payload.get("step_no")
            step_state = event.payload.get("state")
            if not isinstance(step_no, int) or not isinstance(step_state, str):
                raise ReplayError(f"seq={event.seq} 的步骤事件缺少 step_no/state")
            steps[step_no] = step_state
            continue

        # compensation 事件只记录、不影响工单状态投影

    if state is None:
        raise ReplayError("事件序列缺少 created 事件")
    return Projection(state=state, version=version, last_seq=ordered[-1].seq, last_hash=prev_hash, steps=steps)


def replay_state(events: Sequence[EventRecord]) -> TicketState:
    return replay(events).state
