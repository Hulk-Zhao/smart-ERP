"""端到端：执行壳 + 网关 + 事件重放 + 熔断。"""

from __future__ import annotations

import asyncio

import pytest
from dbutil import db_env, prepare_executing
from support import ScriptedConnector, build_runtime, make_action

from apps.agents.breaker import BreakerPolicy, CircuitBreaker
from apps.agents.events import replay
from apps.agents.runtime import PlannedStep
from apps.agents.scenes import scene_of
from apps.agents.shell import AgentShell, BreakerOpenError, SceneLevelExceeded
from apps.platform.contracts.ticket import TicketState
from apps.platform.policy.gateway import BreakerState
from apps.platform.policy.levels import WriteLevel

RAW_INPUT = {
    "scene_code": "AG-1",
    "scope": {"org_id": "org-1", "period": "2026-10", "system_ids": ["erp", "mes"]},
    "trigger_kind": "timer",
    "business_key": "2026-10/SUP-1",
    "payload": {"supplier": "SUP-1"},
    "confidence": 0.92,
    "money": {"diff_amount": {"amount": "120.5000", "currency": "CNY", "tax_included": True}},
}


def _plan() -> list[PlannedStep]:
    return [
        PlannedStep(
            action=make_action("fake.draft.create.1"),
            target_system="fake",
            payload={"supplier": "SUP-1", "amount": "10.00"},
            idem_key="idem-1",
        ),
        PlannedStep(
            action=make_action("fake.draft.create.2"),
            target_system="fake",
            payload={"supplier": "SUP-1", "amount": "20.00"},
            idem_key="idem-2",
        ),
    ]


def test_happy_path_completes_and_replays(db: str) -> None:
    async def scenario() -> None:
        async with db_env(db) as env:
            ticket = await prepare_executing(env.store)
            connector = ScriptedConnector()
            breaker = CircuitBreaker(BreakerPolicy())
            runtime = build_runtime(env, connector, breaker=breaker)
            shell = AgentShell(
                scene_of("AG-1"),
                runtime=runtime,
                gateway=None,  # type: ignore[arg-type]
                breaker=breaker,
            )

            result = await shell.run(
                ticket_id=ticket.id,
                raw_input=RAW_INPUT,
                steps=_plan(),
                owner="worker-a",
            )

            assert result.status is TicketState.COMPLETED
            assert result.fail_code is None
            assert result.invocations == 2
            assert breaker.state("AG-1") == BreakerState.HEALTHY

            check = await env.store.check_projection(ticket.id)
            assert check.consistent
            events = await env.store.list_events(ticket.id)
            assert replay(events).state is TicketState.COMPLETED
            assert [event.seq for event in events] == list(range(1, len(events) + 1))

            steps = await env.store.list_steps(ticket.id)
            assert [step.state for step in steps] == ["succeeded", "succeeded"]
            assert steps[0].before_ref == "DRAFT-BEFORE"
            assert steps[0].after_ref == "DRAFT-AFTER"

    asyncio.run(scenario())


def test_failure_is_classified_and_breaker_blocks_writes(db: str) -> None:
    async def scenario() -> None:
        async with db_env(db) as env:
            connector = ScriptedConnector(behaviours={"fake.draft.create.1": "transient"})
            breaker = CircuitBreaker(BreakerPolicy(min_samples=2))
            runtime = build_runtime(env, connector, breaker=breaker, max_attempts=3)
            shell = AgentShell(
                scene_of("AG-1"),
                runtime=runtime,
                gateway=None,  # type: ignore[arg-type]
                breaker=breaker,
            )

            for _ in range(2):
                ticket = await prepare_executing(env.store)
                result = await shell.run(
                    ticket_id=ticket.id,
                    raw_input={
                        **RAW_INPUT,
                        "business_key": f"2026-10/{ticket.business_key}",
                    },
                    steps=_plan()[:1],
                    owner="worker-a",
                )
                assert result.fail_code == "F5"
                assert result.status is TicketState.COMPENSATING

            assert breaker.is_open("AG-1") is True
            writes_before = len(connector.writes)

            ticket = await prepare_executing(env.store)
            with pytest.raises(BreakerOpenError):
                await shell.run(
                    ticket_id=ticket.id,
                    raw_input={
                        **RAW_INPUT,
                        "business_key": f"2026-10/{ticket.business_key}",
                    },
                    steps=_plan()[:1],
                    owner="worker-a",
                )
            assert len(connector.writes) == writes_before  # 熔断期间写入 0 次

    asyncio.run(scenario())


def test_scene_cap_is_enforced_before_gateway(db: str) -> None:
    async def scenario() -> None:
        async with db_env(db) as env:
            ticket = await prepare_executing(env.store)
            connector = ScriptedConnector()
            runtime = build_runtime(env, connector)
            shell = AgentShell(
                scene_of("AG-1"),
                runtime=runtime,
                gateway=None,  # type: ignore[arg-type]
                breaker=CircuitBreaker(BreakerPolicy()),
            )
            w2_step = PlannedStep(
                action=make_action("fake.post.create", level=WriteLevel.W2),
                target_system="fake",
                payload={"supplier": "SUP-1", "amount": "1.00"},
                idem_key="idem-w2",
            )
            with pytest.raises(SceneLevelExceeded):
                await shell.run(
                    ticket_id=ticket.id,
                    raw_input=RAW_INPUT,
                    steps=[w2_step],
                    owner="worker-a",
                )
            assert connector.writes == []

    asyncio.run(scenario())
