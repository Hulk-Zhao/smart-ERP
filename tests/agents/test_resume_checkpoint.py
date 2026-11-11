"""T305 验收：工单执行器的断点续跑（真实进程 kill / 租约过期）。

四项验收（每个用例都断言）：

1. 已完成的步骤不重做：同一 ``idem_key`` 的外部调用只发生一次；
2. 不丢单：``ticket`` 投影与 ``ticket_event`` 链都能推进到终态「已完成」；
3. 哈希链连续：恢复后 ``prev_hash`` 接得上、``seq`` 连续、无分叉；
4. ``version`` 单调：恢复前后版本不回退，且等于迁移事件条数。

注入方式（两种，均为真实故障而非进程内异常）：

- ``kill_pid``：独立子进程在「外部写入已生效、本地未提交」时被 SIGKILL；
- ``lease_expiry``：子进程在外部写入前挂起（不再续租），租约过期后由第二个
  worker 接管续跑。

运行：

    $env:ERP_TEST_DATABASE_URL="postgresql+asyncpg://erp_test:erp_test@127.0.0.1:55432/smart_erp_t305"
    python -m pytest tests/agents/test_resume_checkpoint.py -q
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text

from apps.agents.events import GENESIS_HASH, TRANSITION_EVENT, replay
from apps.agents.lease import LeaseNotHeld
from apps.platform.contracts.ticket import TicketState
from dbutil import db_env, prepare_executing
from resume_worker import ProbeConnector, build_probe_runtime, plan_steps

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKER = REPO_ROOT / "tests" / "agents" / "resume_worker.py"
PROBE_SCHEMA = "resume_probe"

_PREPARE_SQL = (
    f"CREATE SCHEMA IF NOT EXISTS {PROBE_SCHEMA}",
    f"""
    CREATE TABLE IF NOT EXISTS {PROBE_SCHEMA}.write_attempt (
        id bigserial PRIMARY KEY,
        ticket_id text NOT NULL,
        action_key text NOT NULL,
        idem_key text NOT NULL,
        worker text NOT NULL,
        created_at timestamptz NOT NULL DEFAULT now()
    )
    """,
    f"""
    CREATE TABLE IF NOT EXISTS {PROBE_SCHEMA}.external_state (
        idem_key text PRIMARY KEY,
        ticket_id text NOT NULL,
        external_id text NOT NULL,
        created_at timestamptz NOT NULL DEFAULT now()
    )
    """,
    f"""
    CREATE TABLE IF NOT EXISTS {PROBE_SCHEMA}.marker (
        name text PRIMARY KEY,
        created_at timestamptz NOT NULL DEFAULT now()
    )
    """,
)

_TRUNCATE_SQL = (
    f"TRUNCATE {PROBE_SCHEMA}.write_attempt, {PROBE_SCHEMA}.external_state, "
    f"{PROBE_SCHEMA}.marker"
)


@pytest.fixture()
def probe(pg_url: str) -> str:
    async def _prepare() -> None:
        async with db_env(pg_url) as env:
            async with env.engine.begin() as conn:
                for ddl in _PREPARE_SQL:
                    await conn.execute(text(ddl))
                await conn.execute(text(_TRUNCATE_SQL))

    asyncio.run(_prepare())
    return pg_url


# --------------------------------------------------------------------- 断言


async def _chain_and_version(env: Any, ticket_id: Any) -> tuple[int, list[int]]:
    """校验哈希链/序号/重放一致，返回 (最终 version, 迁移事件版本序列)。"""

    events = await env.store.list_events(ticket_id)
    assert [event.seq for event in events] == list(range(1, len(events) + 1)), (
        "seq 必须从 1 连续递增"
    )
    prev_hash = GENESIS_HASH
    versions = [0]  # created 事件把 version 归零
    for event in events:
        assert event.prev_hash == prev_hash, f"seq={event.seq} 的 prev_hash 断链"
        assert event.hash == event.computed_hash, f"seq={event.seq} 哈希校验失败"
        prev_hash = event.hash
        if event.event_type == TRANSITION_EVENT:
            versions.append(versions[-1] + 1)

    projection = replay(events)  # 断链/篡改/状态链断裂都会抛 ReplayError
    check = await env.store.check_projection(ticket_id)
    assert check.consistent, check
    assert projection.version == check.stored_version
    assert len(versions) - 1 == projection.version, "version 必须等于迁移事件条数"
    assert versions[-1] == projection.version
    return projection.version, versions


async def _attempts_by_idem(env: Any, ticket_id: Any) -> dict[str, list[str]]:
    async with env.sf() as session:
        rows = (
            await session.execute(
                text(
                    f"SELECT idem_key, worker FROM {PROBE_SCHEMA}.write_attempt "
                    "WHERE ticket_id = :ticket_id ORDER BY id"
                ),
                {"ticket_id": str(ticket_id)},
            )
        ).all()
    grouped: dict[str, list[str]] = {}
    for idem_key, worker in rows:
        grouped.setdefault(idem_key, []).append(worker)
    return grouped


async def _external_state_count(env: Any, ticket_id: Any) -> int:
    async with env.sf() as session:
        return int(
            (
                await session.execute(
                    text(
                        f"SELECT count(*) FROM {PROBE_SCHEMA}.external_state "
                        "WHERE ticket_id = :ticket_id"
                    ),
                    {"ticket_id": str(ticket_id)},
                )
            ).scalar_one()
        )


async def _recovered_events(env: Any, ticket_id: Any) -> list[Any]:
    return [
        event
        for event in await env.store.list_events(ticket_id)
        if event.event_type == "recovered"
    ]


async def _wait_marker(env: Any, worker: str, *, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    async with env.sf() as session:
        while time.monotonic() < deadline:
            hit = (
                await session.execute(
                    text(
                        f"SELECT 1 FROM {PROBE_SCHEMA}.marker WHERE name = :name"
                    ),
                    {"name": worker},
                )
            ).first()
            if hit is not None:
                await session.rollback()
                return
            await asyncio.sleep(0.1)
    raise AssertionError(f"等待注入点超时（worker={worker}）")


def _spawn(
    url: str, ticket_id: Any, owner: str, *, ttl: int, mode: str
) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [
            sys.executable,
            str(WORKER),
            "--url",
            url,
            "--ticket",
            str(ticket_id),
            "--owner",
            owner,
            "--ttl",
            str(ttl),
            "--mode",
            mode,
        ],
        cwd=str(REPO_ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _kill(process: subprocess.Popen[str]) -> None:
    """SIGKILL 等价：Windows 下 TerminateProcess，不走 finally/清理。"""

    if process.poll() is None:
        process.kill()
    process.communicate(timeout=30)


async def _execute_after_lease_expiry(
    env: Any, connector: ProbeConnector, ticket_id: Any, *, timeout: float = 30.0
) -> Any:
    runtime = build_probe_runtime(env.sf, connector, lease_ttl_seconds=30)
    deadline = time.monotonic() + timeout
    while True:
        try:
            return await runtime.execute(
                ticket_id, owner=connector.worker, steps=plan_steps(), scene=_scene()
            )
        except LeaseNotHeld:
            if time.monotonic() >= deadline:
                raise
            await asyncio.sleep(0.2)


def _scene() -> Any:
    from support import SCENE

    return SCENE


# --------------------------------------------------------------------- 用例


def test_process_kill_after_external_write_resumes_without_replay(
    db: str, probe: str
) -> None:
    """进程在「外部已写、本地未提交」时被 kill；恢复后不重放、链不断。"""

    async def scenario() -> None:
        async with db_env(db) as env:
            ticket = await prepare_executing(env.store)
            child = _spawn(
                db, ticket.id, "worker-A", ttl=2, mode="kill_after_write"
            )
            try:
                await _wait_marker(env, "worker-A", timeout=30)

                after_crash = await env.store.list_steps(ticket.id)
                assert [(s.step_no, s.state) for s in after_crash] == [
                    (1, "succeeded"),
                    (2, "intent"),
                ], "kill 后应保留第 1 步成功、第 2 步仅落 intent"
                crashed_ticket = await env.store.get_ticket(ticket.id)
                assert crashed_ticket.state == TicketState.EXECUTING.value
                assert await _attempts_by_idem(env, ticket.id) == {
                    "idem-s1": ["worker-A"],
                    "idem-s2": ["worker-A"],
                }, "kill 时第 2 步的外部调用已生效（但本地未提交）"
                assert await _external_state_count(env, ticket.id) == 2
                v_crash = int(crashed_ticket.version)
            finally:
                _kill(child)

            connector = ProbeConnector(env.sf, worker="worker-B")
            report = await _execute_after_lease_expiry(env, connector, ticket.id)

            assert report.status is TicketState.COMPLETED
            # 第 1 步跳过（recovered=True 表示「未重做」），第 2 步由 verify 确认后
            # 标记 recovered，所以恢复报告里 recovered 计数为 2；真正的判据是下面的
            # recovered 事件与外部调用次数。
            assert report.recovered == 2
            assert connector.calls == ["write:probe.s3:idem-s3"], (
                "恢复进程只应执行第 3 步；第 1 步跳过、第 2 步不得重放"
            )

            steps = await env.store.list_steps(ticket.id)
            assert [(s.step_no, s.state) for s in steps] == [
                (1, "succeeded"),
                (2, "succeeded"),
                (3, "succeeded"),
            ]
            attempts = await _attempts_by_idem(env, ticket.id)
            assert attempts == {
                "idem-s1": ["worker-A"],
                "idem-s2": ["worker-A"],
                "idem-s3": ["worker-B"],
            }, f"同一 idem_key 不得写两次外部调用：{attempts}"
            assert await _external_state_count(env, ticket.id) == 3

            recovered = await _recovered_events(env, ticket.id)
            assert len(recovered) == 1
            assert recovered[0].payload["step_no"] == 2
            assert recovered[0].payload["idem_key"] == "idem-s2"

            final_ticket = await env.store.get_ticket(ticket.id)
            assert final_ticket.state == TicketState.COMPLETED.value
            v_final, versions = await _chain_and_version(env, ticket.id)
            assert v_final >= v_crash, "version 不得回退"
            assert v_final - v_crash == 2, "恢复只应新增 VERIFYING、COMPLETED 两次迁移"
            assert versions[-1] == v_final

    asyncio.run(scenario())


def test_lease_expiry_takeover_resumes_from_intent_without_duplicate_write(
    db: str, probe: str
) -> None:
    """worker-A 在外部写入前挂起 → 租约过期 → worker-B 接管并续跑到完成。"""

    async def scenario() -> None:
        async with db_env(db) as env:
            ticket = await prepare_executing(env.store)
            child = _spawn(
                db, ticket.id, "worker-A", ttl=1, mode="hang_before_write"
            )
            try:
                await _wait_marker(env, "worker-A", timeout=30)

                after_hang = await env.store.list_steps(ticket.id)
                assert [(s.step_no, s.state) for s in after_hang] == [
                    (1, "succeeded"),
                    (2, "intent"),
                ]
                holder, _ = await env.leases.holder(ticket.id)
                assert holder == "worker-A"
                assert await _attempts_by_idem(env, ticket.id) == {
                    "idem-s1": ["worker-A"]
                }, "挂起的 worker 不得产生第 2 步外部调用"
                assert await _external_state_count(env, ticket.id) == 1
                v_crash = int((await env.store.get_ticket(ticket.id)).version)

                connector = ProbeConnector(env.sf, worker="worker-B")
                started = time.monotonic()
                report = await _execute_after_lease_expiry(
                    env, connector, ticket.id
                )
                assert time.monotonic() - started >= 0.5, (
                    "必须在 worker-A 租约过期后才能接管"
                )
            finally:
                _kill(child)

            assert report.status is TicketState.COMPLETED
            # recovered=1 仅来自「第 1 步已成功、跳过不重做」；第 2 步应走
            # verify=absent → 正常重放，不产生 recovered 事件。
            assert report.recovered == 1

            steps = await env.store.list_steps(ticket.id)
            assert [(s.step_no, s.state) for s in steps] == [
                (1, "succeeded"),
                (2, "succeeded"),
                (3, "succeeded"),
            ]
            attempts = await _attempts_by_idem(env, ticket.id)
            assert attempts == {
                "idem-s1": ["worker-A"],
                "idem-s2": ["worker-B"],
                "idem-s3": ["worker-B"],
            }, f"接管后每个 idem_key 只写一次：{attempts}"
            assert await _external_state_count(env, ticket.id) == 3
            assert await _recovered_events(env, ticket.id) == []

            final_ticket = await env.store.get_ticket(ticket.id)
            assert final_ticket.state == TicketState.COMPLETED.value
            v_final, versions = await _chain_and_version(env, ticket.id)
            assert v_final >= v_crash
            assert v_final - v_crash == 2
            assert versions[-1] == v_final

    asyncio.run(scenario())


def test_resume_after_step_failed_before_compensation_transition(
    db: str, probe: str
) -> None:
    """崩溃窗口：步骤已判 failed，但进程死在「转补偿中」之前。

    恢复时同一 ``step_no`` 不能再落 intent（会撞唯一键、工单永久卡在执行中），
    而应沿失败分支推进到「补偿中」并最终回滚到终态。
    """

    async def scenario() -> None:
        async with db_env(db) as env:
            from apps.agents.runtime import request_hash

            ticket = await prepare_executing(env.store)
            planned = plan_steps()[0]
            step = await env.store.intent_step(
                ticket_id=ticket.id,
                step_no=1,
                action_key=planned.action.key,
                target_system=planned.target_system,
                write_level=1,
                idem_key=planned.idem_key,
                request_hash=request_hash(planned.payload),
                request_snapshot=dict(planned.payload),
            )
            await env.store.mark_step(
                step_id=step.id, state="failed", error_code="E-CONN-UNAVAILABLE"
            )
            assert (await env.store.get_ticket(ticket.id)).state == (
                TicketState.EXECUTING.value
            )
            v_crash = int((await env.store.get_ticket(ticket.id)).version)

            connector = ProbeConnector(env.sf, worker="worker-B")
            report = await _execute_after_lease_expiry(env, connector, ticket.id)

            assert report.status is TicketState.COMPENSATING, (
                "失败步骤的恢复必须沿失败分支转补偿，而不是重落 intent 卡死"
            )
            assert connector.calls == [], "已判失败的步骤不得重放外部调用"
            stored = await env.store.get_ticket(ticket.id)
            assert stored.state == TicketState.COMPENSATING.value

            comp = await build_probe_runtime(
                env.sf, connector, lease_ttl_seconds=30
            ).compensate(ticket.id, owner="comp-1")
            assert comp.status is TicketState.ROLLED_BACK
            assert (await env.store.get_ticket(ticket.id)).state == (
                TicketState.ROLLED_BACK.value
            )

            v_final, versions = await _chain_and_version(env, ticket.id)
            assert v_final > v_crash
            assert versions[-1] == v_final
            assert [
                (s.step_no, s.state) for s in await env.store.list_steps(ticket.id)
            ] == [(1, "failed")]

    asyncio.run(scenario())


def test_resume_after_verifying_before_completed(db: str, probe: str) -> None:
    """崩溃窗口：步骤全部成功、工单已进「校验中」，但没来得及转「已完成」。"""

    async def scenario() -> None:
        async with db_env(db) as env:
            from apps.agents.runtime import request_hash
            from apps.agents.state_machine import TransitionContext

            ticket = await prepare_executing(env.store)
            planned = plan_steps()[0]
            step = await env.store.intent_step(
                ticket_id=ticket.id,
                step_no=1,
                action_key=planned.action.key,
                target_system=planned.target_system,
                write_level=1,
                idem_key=planned.idem_key,
                request_hash=request_hash(planned.payload),
                request_snapshot=dict(planned.payload),
            )
            await env.store.mark_step(step_id=step.id, state="succeeded")
            await env.store.transition(
                ticket.id,
                TicketState.VERIFYING,
                "all_ok",
                context=TransitionContext(step_states=("succeeded",)),
            )
            v_crash = int((await env.store.get_ticket(ticket.id)).version)

            connector = ProbeConnector(env.sf, worker="worker-B")
            report = await _execute_after_lease_expiry(env, connector, ticket.id)

            assert report.status is TicketState.COMPLETED, (
                "所有步骤已成功时，恢复应把「校验中」推进到终态，而不是拒绝执行"
            )
            assert connector.calls == []
            assert (await env.store.get_ticket(ticket.id)).state == (
                TicketState.COMPLETED.value
            )
            v_final, versions = await _chain_and_version(env, ticket.id)
            assert v_final > v_crash
            assert versions[-1] == v_final

    asyncio.run(scenario())
