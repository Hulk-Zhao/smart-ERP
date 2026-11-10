"""D4 审计链 fail-closed + D1 recovered 事件/阈值。

覆盖：
1. 篡改链尾 → execute 前 O(1) 校验拒绝（0 写入、P0 告警）；
2. 篡改中间事件 → 热路径 O(1) 看不见，但离线全链巡检发现并全局降级只读；
3. recovered 必须写显式事件（带幂等键证据），且外部只写一次；
4. 同一步骤反复 recovered 超过阈值 → 强制转人工。
"""

from __future__ import annotations

import asyncio
from uuid import UUID

import pytest
from dbutil import db_env, prepare_executing
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool
from support import SCENE, ScriptedConnector, build_runtime, make_action

from apps.agents.alerts import RecordingAlertSink
from apps.agents.audit_guard import (
    READONLY,
    AuditChainBroken,
    AuditGuard,
    ExportLimitExceeded,
    WriteDegraded,
    verify_before_export,
)
from apps.agents.runtime import RECOVERY_THRESHOLD_ERROR, PlannedStep
from apps.platform.contracts.ticket import TicketState


class WorkerCrash(BaseException):
    """模拟进程在 verify 前死亡。"""


class CrashOnceConnector(ScriptedConnector):
    crash_verify_once: bool = True

    async def verify(self, spec, payload):
        if self.crash_verify_once:
            self.crash_verify_once = False
            raise WorkerCrash("外部已写、本地未提交")
        return await super().verify(spec, payload)


def _step(key: str = "s1") -> PlannedStep:
    return PlannedStep(
        action=make_action(key),
        target_system="fake",
        payload={"supplier": "S-001", "amount": "100.00"},
        idem_key=f"idem-{key}",
    )


async def _tamper(url: str, ticket_id: UUID, *, seq: int | None) -> None:
    """模拟磁盘损坏：提权禁用 append-only 触发器后改写一条事件。"""

    engine = create_async_engine(url, poolclass=NullPool)
    target = (
        "seq = :seq"
        if seq is not None
        else "seq = (SELECT max(seq) FROM erp.ticket_event WHERE ticket_id = :ticket_id)"
    )
    try:
        async with engine.begin() as conn:
            await conn.execute(text("ALTER TABLE erp.ticket_event DISABLE TRIGGER USER"))
            await conn.execute(
                text(
                    "UPDATE erp.ticket_event "
                    "SET payload = payload || '{\"tampered\": true}'::jsonb "
                    f"WHERE ticket_id = :ticket_id AND {target}"
                ),
                {"ticket_id": ticket_id, "seq": seq},
            )
            await conn.execute(text("ALTER TABLE erp.ticket_event ENABLE TRIGGER USER"))
    finally:
        await engine.dispose()


async def _assert_tail_tamper_blocks_execute(db: str) -> None:
    async with db_env(db) as env:
        ticket = await prepare_executing(env.store)
        await _tamper(db, ticket.id, seq=None)

        alerts = RecordingAlertSink()
        connector = ScriptedConnector()
        runtime = build_runtime(env, connector, alert_sink=alerts)

        with pytest.raises(AuditChainBroken):
            await runtime.execute(ticket.id, owner="worker-A", steps=[_step()], scene=SCENE)

        assert connector.writes == []
        assert "AUDIT_CHAIN_BROKEN" in alerts.codes()
        assert (await env.store.get_ticket(ticket.id)).state == TicketState.EXECUTING.value


async def _assert_middle_tamper_detected_offline(db: str) -> None:
    async with db_env(db) as env:
        ticket = await prepare_executing(env.store)
        other = await prepare_executing(env.store)
        await _tamper(db, ticket.id, seq=2)

        alerts = RecordingAlertSink()
        connector = ScriptedConnector()
        runtime = build_runtime(env, connector, alert_sink=alerts)

        # 链尾与锚点仍一致：O(1) 热路径按设计放过中间篡改
        report = await runtime.execute(ticket.id, owner="worker-A", steps=[_step()], scene=SCENE)
        assert report.status is TicketState.COMPLETED

        guard = AuditGuard(env.store, alert_sink=alerts)
        scan = await guard.daily_verify()
        assert ticket.id in scan.broken
        assert scan.degraded
        assert "AUDIT_CHAIN_BROKEN" in alerts.codes()
        assert await guard.global_mode() == READONLY

        # 全局只读后，写类动作必须拒绝（W0 只读不受影响）
        before = len(connector.writes)
        with pytest.raises(WriteDegraded):
            await runtime.execute(other.id, owner="worker-B", steps=[_step("s2")], scene=SCENE)
        assert len(connector.writes) == before


async def _assert_recovery_event_and_single_write(db: str) -> None:
    async with db_env(db) as env:
        ticket = await prepare_executing(env.store)
        crashing = CrashOnceConnector()
        first = build_runtime(env, crashing)
        with pytest.raises(WorkerCrash):
            await first.execute(ticket.id, owner="worker-A", steps=[_step()], scene=SCENE)

        restart = ScriptedConnector()
        second = build_runtime(env, restart)
        report = await second.execute(
            ticket.id, owner="worker-B", steps=[_step()], scene=SCENE
        )

        assert report.status is TicketState.COMPLETED
        assert len(crashing.writes) == 1
        assert restart.writes == []
        steps = await env.store.list_steps(ticket.id)
        assert steps[0].state == "succeeded"

        events = await env.store.list_events(ticket.id)
        recovered = [event for event in events if event.event_type == "recovered"]
        assert len(recovered) == 1
        assert recovered[0].payload["idem_key"] == "idem-s1"
        assert recovered[0].payload["evidence"]["step_no"] == 1
        assert await env.store.count_recoveries(ticket.id, step_no=1) == 1


async def _assert_repeated_recovery_forces_manual(db: str) -> None:
    async with db_env(db) as env:
        ticket = await prepare_executing(env.store)
        for prior in range(2):
            await env.store.record_recovery(
                ticket_id=ticket.id,
                step_no=1,
                idem_key="idem-s1",
                verify_observed={"ok": True},
                prior_recoveries=prior,
            )

        crashing = CrashOnceConnector()
        first = build_runtime(env, crashing)
        with pytest.raises(WorkerCrash):
            await first.execute(ticket.id, owner="worker-A", steps=[_step()], scene=SCENE)

        alerts = RecordingAlertSink()
        restart = ScriptedConnector()
        second = build_runtime(env, restart, alert_sink=alerts)
        report = await second.execute(
            ticket.id, owner="worker-B", steps=[_step()], scene=SCENE
        )
        assert report.status is TicketState.COMPENSATING
        steps = await env.store.list_steps(ticket.id)
        assert steps[0].state == "unresolved"
        assert steps[0].error_code == RECOVERY_THRESHOLD_ERROR
        assert "RECOVERY_THRESHOLD_EXCEEDED" in alerts.codes()
        assert restart.writes == []

        final = await second.compensate(ticket.id, owner="comp-1")
        assert final.status is TicketState.MANUAL_REQUIRED


def test_tampered_tail_blocks_execute(db: str) -> None:
    asyncio.run(_assert_tail_tamper_blocks_execute(db))


def test_middle_tamper_detected_by_daily_verify_then_readonly(db: str) -> None:
    asyncio.run(_assert_middle_tamper_detected_offline(db))


def test_recovery_event_written_and_external_write_once(db: str) -> None:
    asyncio.run(_assert_recovery_event_and_single_write(db))


def test_repeated_recovery_forces_manual(db: str) -> None:
    asyncio.run(_assert_repeated_recovery_forces_manual(db))


async def _assert_export_gate_blocks_broken_chain(db: str) -> None:
    """出表闸门：健康链放行；中间事件被篡改 → 拒绝出表 + 冻结写入 + P0。"""

    async with db_env(db) as env:
        ticket = await prepare_executing(env.store)
        alerts = RecordingAlertSink()
        guard = AuditGuard(env.store, alert_sink=alerts)

        ok = await guard.assert_exportable([ticket.id])
        assert ok.ok and ok.checked == 1
        assert ok.elapsed_ms >= 0

        await _tamper(db, ticket.id, seq=2)
        with pytest.raises(AuditChainBroken):
            await guard.assert_exportable([ticket.id])
        assert await guard.global_mode() == READONLY
        assert "AUDIT_CHAIN_BROKEN" in alerts.codes()


async def _assert_periodic_scan_runs_configured_times(db: str) -> None:
    async with db_env(db) as env:
        await prepare_executing(env.store)
        guard = AuditGuard(env.store)
        reports = await guard.run_periodic(interval_seconds=0.01, max_scans=2)
        assert len(reports) == 2
        assert all(report.ok for report in reports)
        assert all(report.checked == 1 for report in reports)

        with pytest.raises(ValueError):
            await guard.run_periodic(interval_seconds=0, max_scans=1)


def test_export_gate_blocks_broken_chain(db: str) -> None:
    asyncio.run(_assert_export_gate_blocks_broken_chain(db))


def test_periodic_scan_runs_configured_times(db: str) -> None:
    asyncio.run(_assert_periodic_scan_runs_configured_times(db))


def test_export_gate_sync_wrapper(db: str) -> None:
    """API 侧实际调用的是同步函数：健康放行、断链抛异常。"""

    async def _prepare(tamper: bool) -> UUID:
        async with db_env(db) as env:
            ticket = await prepare_executing(env.store)
            if tamper:
                await _tamper(db, ticket.id, seq=2)
            return ticket.id

    healthy = asyncio.run(_prepare(False))
    report = verify_before_export(db, ticket_ids=[healthy])
    assert report.ok and report.checked == 1

    broken = asyncio.run(_prepare(True))
    with pytest.raises(AuditChainBroken):
        verify_before_export(db, ticket_ids=[broken])


async def _assert_export_gate_rejects_oversized_batch(db: str) -> None:
    async with db_env(db) as env:
        ticket = await prepare_executing(env.store)
        guard = AuditGuard(env.store, export_event_limit=2)
        with pytest.raises(ExportLimitExceeded):
            await guard.assert_exportable([ticket.id])


def test_export_gate_rejects_oversized_batch(db: str) -> None:
    asyncio.run(_assert_export_gate_rejects_oversized_batch(db))


def test_sync_gate_refuses_to_run_inside_event_loop(db: str) -> None:
    """防止 API 在 FastAPI 请求里误调同步包装器。"""

    async def _call() -> None:
        with pytest.raises(RuntimeError, match="assert_exportable"):
            verify_before_export(db, ticket_ids=[])

    asyncio.run(_call())
