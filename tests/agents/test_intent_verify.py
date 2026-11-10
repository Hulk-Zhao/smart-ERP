"""两段提交 intent → verify：外部成功 / 失败 / 超时三种情况。"""

from __future__ import annotations

import asyncio
import hashlib

from dbutil import db_env, prepare_executing
from support import SCENE, ScriptedConnector, build_runtime, make_action

from apps.agents.runtime import PlannedStep
from apps.platform.contracts.ticket import TicketState

PAYLOAD = {"supplier": "SUP-1", "amount": "10.00"}


def _step(key: str = "fake.draft.create", idem: str = "idem-1") -> PlannedStep:
    return PlannedStep(
        action=make_action(key),
        target_system="fake",
        payload=PAYLOAD,
        idem_key=idem,
    )


def test_external_success_but_unknown_is_recovered_by_verify(db: str) -> None:
    """超时/断连但目标系统已生效：不重试，verify=match 后记为成功。"""

    async def scenario() -> None:
        async with db_env(db) as env:
            ticket = await prepare_executing(env.store)
            connector = ScriptedConnector(behaviours={"fake.draft.create": "unknown_success"})
            runtime = build_runtime(env, connector)

            report = await runtime.execute(
                ticket.id, owner="worker-a", steps=[_step()], scene=SCENE
            )

            assert report.status is TicketState.COMPLETED
            assert report.fail_code is None
            assert report.outcomes[0].state == "succeeded"
            assert report.outcomes[0].recovered is True
            assert len(connector.writes) == 1  # 没有盲目重放
            assert connector.verify_calls >= 1
            steps = await env.store.list_steps(ticket.id)
            assert steps[0].state == "succeeded"
            assert steps[0].response_snapshot["recovered_by_verify"] == {"ok": True}

    asyncio.run(scenario())


def test_external_failure_retries_then_compensates(db: str) -> None:
    """外部明确没写成：可安全重放，3 次后 F5 进补偿。"""

    async def scenario() -> None:
        async with db_env(db) as env:
            ticket = await prepare_executing(env.store)
            connector = ScriptedConnector(behaviours={"fake.draft.create": "unknown_absent"})
            runtime = build_runtime(env, connector, max_attempts=3)

            report = await runtime.execute(
                ticket.id, owner="worker-a", steps=[_step()], scene=SCENE
            )

            assert report.status is TicketState.COMPENSATING
            assert report.fail_code == "F5"
            assert report.outcomes[0].state == "failed"
            assert len(connector.writes) == 3
            stored = await env.store.get_ticket(ticket.id)
            assert stored.state == TicketState.COMPENSATING.value

    asyncio.run(scenario())


def test_external_timeout_with_mismatch_goes_unresolved(db: str) -> None:
    """结果未知且 verify 值不符：标 unresolved + F6，立即补偿而不是重试。"""

    async def scenario() -> None:
        async with db_env(db) as env:
            ticket = await prepare_executing(env.store)
            connector = ScriptedConnector(
                behaviours={"fake.draft.create": "unknown_mismatch"}
            )
            runtime = build_runtime(env, connector)

            report = await runtime.execute(
                ticket.id, owner="worker-a", steps=[_step()], scene=SCENE
            )

            assert report.status is TicketState.COMPENSATING
            assert report.fail_code == "F6"
            assert report.outcomes[0].state == "unresolved"
            assert len(connector.writes) == 1
            steps = await env.store.list_steps(ticket.id)
            assert steps[0].state == "unresolved"
            assert steps[0].error_code == "E-VERIFY-MISMATCH"

    asyncio.run(scenario())


def test_crash_resume_verifies_intent_before_replay(db: str) -> None:
    """已落 intent 但本地未提交：续跑先 verify，不重放写入。"""

    async def scenario() -> None:
        async with db_env(db) as env:
            ticket = await prepare_executing(env.store)
            await env.store.intent_step(
                ticket_id=ticket.id,
                step_no=1,
                action_key="fake.draft.create",
                target_system="fake",
                write_level=1,
                idem_key="idem-1",
                request_hash=hashlib.sha256(repr(PAYLOAD).encode()).hexdigest(),
                request_snapshot=PAYLOAD,
            )
            connector = ScriptedConnector()
            runtime = build_runtime(env, connector)

            report = await runtime.execute(
                ticket.id, owner="worker-resume", steps=[_step()], scene=SCENE
            )

            assert report.status is TicketState.COMPLETED
            assert report.outcomes[0].recovered is True
            assert connector.writes == []  # 只 verify，没有再写
            steps = await env.store.list_steps(ticket.id)
            assert len(steps) == 1
            assert steps[0].state == "succeeded"

    asyncio.run(scenario())


def test_transient_failure_is_retried_three_times(db: str) -> None:
    async def scenario() -> None:
        async with db_env(db) as env:
            ticket = await prepare_executing(env.store)
            connector = ScriptedConnector(behaviours={"fake.draft.create": "transient"})
            runtime = build_runtime(env, connector, max_attempts=3)

            report = await runtime.execute(
                ticket.id, owner="worker-a", steps=[_step()], scene=SCENE
            )

            assert report.fail_code == "F5"
            assert len(connector.writes) == 3
            assert report.status is TicketState.COMPENSATING

    asyncio.run(scenario())


def test_external_effective_locks_without_compensation(db: str) -> None:
    async def scenario() -> None:
        async with db_env(db) as env:
            ticket = await prepare_executing(env.store)
            connector = ScriptedConnector(behaviours={"fake.draft.create": "f8"})
            runtime = build_runtime(env, connector)

            report = await runtime.execute(
                ticket.id, owner="worker-a", steps=[_step()], scene=SCENE
            )

            assert report.status is TicketState.LOCKED
            assert report.fail_code == "F8"
            assert await env.queue.list_tasks(ticket.id) == []

    asyncio.run(scenario())
