"""工单执行运行时：租约 → intent → 外部写 → verify → 状态机 / Saga 补偿。

写入路径只有 ``WriteGateway.execute`` 一条；本模块额外需要两种**读/补偿**能力：
``Connector.verify``（判定未知结果）与 ``Connector.compensate``（撤销已写入的步骤）。
两者都可注入替身，测试不需要真实系统。
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from decimal import Decimal
from time import monotonic
from time import time as wall_time
from typing import Any
from uuid import UUID

from apps.agents.alerts import Alert, AlertSink, NullAlertSink
from apps.agents.audit_guard import READONLY, AuditGuard, WriteDegraded
from apps.agents.compensation import CompensationQueue, CompensationTaskView
from apps.agents.events import EventRecord
from apps.agents.failure import Failure, classify_exception, failure_of
from apps.agents.lease import LeaseManager, LeaseNotHeld
from apps.agents.state_machine import DEVIATION_THRESHOLD, TransitionContext
from apps.agents.store import TicketStore
from apps.platform.connectors.sdk import CompensateResult, UnknownOutcome, VerifyResult
from apps.platform.contracts.ticket import TicketState
from apps.platform.policy.gateway import (
    GatewayResult,
    WriteGateway,
    default_metrics_registry,
)
from apps.platform.policy.levels import ActionSpec, SceneSpec
from observability.metrics import MetricsRegistry

UNIQUE_VERIFY_KINDS = frozenset({"read_by_external_id", "read_by_idem_key"})
RECOVERY_THRESHOLD_ERROR = "E-RECOVERY-THRESHOLD"

logger = logging.getLogger("erp.agents.runtime")


def request_hash(payload: Mapping[str, Any]) -> str:
    body = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class PlannedStep:
    """业务流（S2 等）给出的单步计划；运行时只负责可靠执行。"""

    action: ActionSpec
    target_system: str
    payload: Mapping[str, Any]
    idem_key: str
    verify_payload: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        if not self.idem_key:
            raise ValueError("计划步骤必须携带 idem_key（G-02）")
        if self.action.level.value > 2:
            raise ValueError(f"动作 {self.action.key} 的等级不可执行（W3 一律拒绝）")


@dataclass(frozen=True, slots=True)
class CompensateCall:
    action_key: str
    payload: Mapping[str, Any]
    original_idem_key: str
    # 执行器需要按系统路由到具体连接器；外部对象号来自步骤的 after_ref。
    # 默认值保证既有调用方（测试替身）不用改。
    target_system: str = ""
    external_id: str | None = None


@dataclass(frozen=True, slots=True)
class StepOutcome:
    step_no: int
    action_key: str
    state: str
    fail_code: str | None = None
    recovered: bool = False
    attempts: int = 1


@dataclass(frozen=True, slots=True)
class ExecutionReport:
    ticket_id: UUID
    status: TicketState
    outcomes: tuple[StepOutcome, ...] = ()
    fail_code: str | None = None
    recovered: int = 0
    invocations: int = 0

    @property
    def failure(self) -> Failure | None:
        return failure_of(self.fail_code) if self.fail_code else None


@dataclass(frozen=True, slots=True)
class CompensationReport:
    ticket_id: UUID
    status: TicketState
    order: tuple[int, ...] = ()
    done: int = 0
    failed: int = 0


VerifyFn = Callable[[PlannedStep], Awaitable[VerifyResult]]
CompensateFn = Callable[[CompensateCall, str], Awaitable[CompensateResult]]


class RuntimeGuardError(Exception):
    """运行时不满足前置条件（不是业务失败分类）。"""


@dataclass
class _RunState:
    invocations: int = 0
    outcomes: list[StepOutcome] = field(default_factory=list)


class TicketRuntime:
    def __init__(
        self,
        *,
        store: TicketStore,
        leases: LeaseManager,
        gateway: WriteGateway,
        compensation: CompensationQueue | None = None,
        verifier: VerifyFn | None = None,
        compensator: CompensateFn | None = None,
        lease_ttl_seconds: int = 30,
        max_attempts: int = 3,
        retry_backoff_seconds: Sequence[float] = (0.0, 0.0, 0.0),
        compensation_retry_seconds: float = 8.0,
        max_compensation_attempts: int = 3,
        compensation_stale_seconds: float = 60.0,
        recovery_daily_limit: int = 3,
        recovery_step_limit: int = 2,
        audit: AuditGuard | None = None,
        alert_sink: AlertSink | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], datetime] | None = None,
        metrics: MetricsRegistry | None = None,
    ) -> None:
        if verifier is None or compensator is None:
            raise ValueError("必须同时注入 verifier 与 compensator（或连接器实现）")
        self._store = store
        self._leases = leases
        self._gateway = gateway
        self._queue = compensation or CompensationQueue(store.session_factory)
        self._verifier = verifier
        self._compensator = compensator
        self._lease_ttl = lease_ttl_seconds
        self._max_attempts = max_attempts
        self._backoff = tuple(retry_backoff_seconds)
        self._comp_retry = compensation_retry_seconds
        self._max_comp_attempts = max_compensation_attempts
        self._comp_stale = compensation_stale_seconds
        self._recovery_daily_limit = recovery_daily_limit
        self._recovery_step_limit = recovery_step_limit
        self._alerts = alert_sink or NullAlertSink()
        self._audit = audit or AuditGuard(store, alert_sink=self._alerts)
        self._sleep = sleep
        self._clock = clock or (lambda: datetime.now(tz=UTC))
        self._metrics = metrics if metrics is not None else default_metrics_registry()
        # 启动自检的第一次成对采样；之后每轮补偿再采一次（只取增量，不重复累计）
        self._clock_mark = (monotonic(), wall_time())

    # -------------------------------------------------------------- 生命周期
    async def mark_preview(
        self,
        ticket_id: UUID,
        *,
        preview_ok: bool,
        deviation_ratio: Decimal | None = None,
        actor_id: UUID | None = None,
        trace_id: str | None = None,
    ) -> EventRecord:
        """预演结果 → 待审批 / 待确认 / 已终止（零写入）。"""

        if preview_ok:
            return await self._store.transition(
                ticket_id,
                TicketState.PENDING_APPROVAL,
                "preview_ok",
                context=TransitionContext(preview_ok=True),
                actor_id=actor_id,
                trace_id=trace_id,
            )
        if deviation_ratio is not None and deviation_ratio > DEVIATION_THRESHOLD:
            return await self._store.transition(
                ticket_id,
                TicketState.NEEDS_REVIEW,
                "deviation_gt_5pct",
                context=TransitionContext(deviation_ratio=deviation_ratio),
                actor_id=actor_id,
                trace_id=trace_id,
            )
        return await self._store.transition(
            ticket_id,
            TicketState.TERMINATED,
            "preview_failed",
            context=TransitionContext(writes=0),
            actor_id=actor_id,
            trace_id=trace_id,
        )

    async def approve(
        self,
        ticket_id: UUID,
        *,
        approved: bool = True,
        actor_id: UUID | None = None,
        trace_id: str | None = None,
    ) -> EventRecord:
        return await self._store.transition(
            ticket_id,
            TicketState.EXECUTING if approved else TicketState.REJECTED,
            "approved" if approved else "rejected",
            context=TransitionContext(approved=approved),
            actor_id=actor_id,
            trace_id=trace_id,
        )

    # ---------------------------------------------------------------- 执行
    async def execute(
        self,
        ticket_id: UUID,
        *,
        owner: str,
        steps: Sequence[PlannedStep],
        scene: SceneSpec,
        trace_id: str | None = None,
    ) -> ExecutionReport:
        """持租约执行计划；租约拿不到就直接拒绝（防双跑）。"""

        # 热路径 fail-closed：审计链不可信 / 全局只读时，先拒绝再谈租约与写入
        await self._audit.assert_executable(ticket_id)
        if await self._audit.global_mode() == READONLY and any(
            step.action.level.value > 0 for step in steps
        ):
            raise WriteDegraded(
                f"审计链巡检已把系统降级为只读，工单 {ticket_id} 的写类动作拒绝执行"
            )

        if not await self._leases.acquire(ticket_id, owner, ttl_seconds=self._lease_ttl):
            raise LeaseNotHeld(f"工单 {ticket_id} 已被 {owner!r} 之外的 worker 持有租约")
        try:
            ticket = await self._store.get_ticket(ticket_id)
            if TicketState(ticket.state) is not TicketState.EXECUTING:
                raise RuntimeGuardError(
                    f"工单 {ticket_id} 当前为 {ticket.state}，必须先审批进入「执行中」"
                )

            existing = {step.step_no: step for step in await self._store.list_steps(ticket_id)}
            run = _RunState()

            for index, planned in enumerate(steps, start=1):
                await self._leases.assert_held(ticket_id, owner)
                await self._leases.renew(ticket_id, owner, ttl_seconds=self._lease_ttl)
                outcome = await self._execute_step(
                    ticket_id=ticket_id,
                    step_no=index,
                    planned=planned,
                    existing=existing.get(index),
                    scene=scene,
                    run=run,
                    trace_id=trace_id,
                )
                run.outcomes.append(outcome)

                if outcome.fail_code == "F8":
                    await self._store.transition(
                        ticket_id,
                        TicketState.LOCKED,
                        "external_effective",
                        context=TransitionContext(fail_code="F8"),
                        trace_id=trace_id,
                    )
                    return ExecutionReport(
                        ticket_id=ticket_id,
                        status=TicketState.LOCKED,
                        outcomes=tuple(run.outcomes),
                        fail_code="F8",
                        recovered=sum(1 for item in run.outcomes if item.recovered),
                        invocations=run.invocations,
                    )

                if outcome.state in {"failed", "unresolved"}:
                    fail_code = outcome.fail_code or "F5"
                    await self._store.transition(
                        ticket_id,
                        TicketState.COMPENSATING,
                        "step_failed",
                        context=TransitionContext(
                            fail_code=fail_code,
                            step_states=tuple(item.state for item in run.outcomes),
                        ),
                        trace_id=trace_id,
                    )
                    await self._queue.enqueue(ticket_id)
                    return ExecutionReport(
                        ticket_id=ticket_id,
                        status=TicketState.COMPENSATING,
                        outcomes=tuple(run.outcomes),
                        fail_code=fail_code,
                        recovered=sum(1 for item in run.outcomes if item.recovered),
                        invocations=run.invocations,
                    )

            states = tuple(item.state for item in run.outcomes)
            await self._store.transition(
                ticket_id,
                TicketState.VERIFYING,
                "all_ok",
                context=TransitionContext(step_states=states),
                trace_id=trace_id,
            )
            # 每一步在网关内已 verify；这里记录整体校验结论
            await self._store.transition(
                ticket_id,
                TicketState.COMPLETED,
                "verified",
                context=TransitionContext(verified=True, step_states=states),
                trace_id=trace_id,
            )
            return ExecutionReport(
                ticket_id=ticket_id,
                status=TicketState.COMPLETED,
                outcomes=tuple(run.outcomes),
                recovered=sum(1 for item in run.outcomes if item.recovered),
                invocations=run.invocations,
            )
        finally:
            await self._leases.release(ticket_id, owner)

    # ------------------------------------------------------------- 单步推进
    async def _execute_step(
        self,
        *,
        ticket_id: UUID,
        step_no: int,
        planned: PlannedStep,
        existing: Any,
        scene: SceneSpec,
        run: _RunState,
        trace_id: str | None = None,
    ) -> StepOutcome:
        # 1) 已成功/跳过的步骤永不重做（断点续跑）
        if existing is not None and existing.state in {"succeeded", "skipped", "compensated"}:
            return StepOutcome(
                step_no=step_no,
                action_key=planned.action.key,
                state=existing.state,
                recovered=True,
            )

        # 2) 崩溃恢复：已落 intent 的步骤先 verify，不盲目重放
        if existing is not None and existing.state in {"intent", "running"}:
            observed = await self._verify(planned)
            if observed.matches():
                return await self._recover(
                    ticket_id=ticket_id,
                    step_id=existing.id,
                    step_no=step_no,
                    planned=planned,
                    observed=observed,
                    trace_id=trace_id,
                )
            if observed.status == "mismatch":
                await self._store.mark_step(
                    step_id=existing.id,
                    state="unresolved",
                    error_code="E-VERIFY-MISMATCH",
                )
                return StepOutcome(
                    step_no=step_no,
                    action_key=planned.action.key,
                    state="unresolved",
                    fail_code="F6",
                )
            step = existing  # absent：安全可重放
            attempt_start = max(1, int(existing.attempt))
        else:
            step = await self._store.intent_step(
                ticket_id=ticket_id,
                step_no=step_no,
                action_key=planned.action.key,
                target_system=planned.target_system,
                write_level=planned.action.level.value,
                idem_key=planned.idem_key,
                request_hash=request_hash(planned.payload),
                request_snapshot=dict(planned.payload),
            )
            attempt_start = 1

        last_error: str | None = None
        for attempt in range(attempt_start, self._max_attempts + 1):
            run.invocations += 1
            try:
                result: GatewayResult = await self._gateway.execute(
                    planned.action,
                    planned.payload,
                    ticket_id=str(ticket_id),
                    idem_key=planned.idem_key,
                    scene=scene,
                )
                await self._store.mark_step(
                    step_id=step.id,
                    state="succeeded",
                    response_snapshot={
                        "external_id": result.write.external_id,
                        "level": result.level.name,
                        "verify": result.verify.status,
                    },
                    before_ref=result.write.before_ref,
                    after_ref=result.write.after_ref,
                )
                return StepOutcome(
                    step_no=step_no,
                    action_key=planned.action.key,
                    state="succeeded",
                    attempts=attempt,
                )
            except UnknownOutcome:
                # 结果未知：不重试，先问目标系统
                observed = await self._verify(planned)
                if observed.matches():
                    return await self._recover(
                        ticket_id=ticket_id,
                        step_id=step.id,
                        step_no=step_no,
                        planned=planned,
                        observed=observed,
                        attempts=attempt,
                        trace_id=trace_id,
                    )
                if observed.absent():
                    last_error = "E-CONN-UNAVAILABLE"
                    if attempt < self._max_attempts:
                        await self._sleep(self._backoff_at(attempt))
                        continue
                    await self._store.mark_step(
                        step_id=step.id, state="failed", error_code=last_error
                    )
                    return StepOutcome(
                        step_no=step_no,
                        action_key=planned.action.key,
                        state="failed",
                        fail_code="F5",
                        attempts=attempt,
                    )
                await self._store.mark_step(
                    step_id=step.id, state="unresolved", error_code="E-VERIFY-MISMATCH"
                )
                return StepOutcome(
                    step_no=step_no,
                    action_key=planned.action.key,
                    state="unresolved",
                    fail_code="F6",
                    attempts=attempt,
                )
            except Exception as exc:
                failure = classify_exception(exc)
                if failure is None:
                    await self._store.mark_step(
                        step_id=step.id,
                        state="failed",
                        error_code=getattr(getattr(exc, "code", None), "value", type(exc).__name__),
                    )
                    raise
                if failure.retriable and attempt < self._max_attempts:
                    last_error = getattr(getattr(exc, "code", None), "value", str(exc))
                    await self._sleep(self._backoff_at(attempt))
                    continue
                await self._store.mark_step(
                    step_id=step.id,
                    state="failed",
                    error_code=getattr(getattr(exc, "code", None), "value", failure.code),
                )
                return StepOutcome(
                    step_no=step_no,
                    action_key=planned.action.key,
                    state="failed",
                    fail_code=failure.code,
                    attempts=attempt,
                )

        raise RuntimeGuardError(f"步骤 {step_no} 未收敛（last_error={last_error}）")

    def _backoff_at(self, attempt: int) -> float:
        index = min(attempt - 1, len(self._backoff) - 1)
        return float(self._backoff[index])

    # ---------------------------------------------------------------- 补偿
    def _sample_clock(self) -> None:
        """成对采样单调钟/墙钟；单调前进而墙钟后退就是回拨（skew 为负）。"""

        tick, wall = monotonic(), wall_time()
        last_tick, last_wall = self._clock_mark
        self._clock_mark = (tick, wall)
        try:
            self._metrics.observe_clock_skew(
                monotonic_delta_seconds=tick - last_tick,
                wall_delta_seconds=wall - last_wall,
            )
        except Exception:
            logger.warning("时钟偏移采样失败，本轮跳过", exc_info=True)

    async def _observe_compensation(self) -> None:
        """补偿轮次入口的指标写入。

        只在轮次入口调一次（不在每个任务上写）；统计失败时只记日志、不写样本，
        绝不写假 0，也绝不把补偿主流程带崩。
        """

        self._sample_clock()
        try:
            overdue, future_retry = await self._queue.metrics_counts()
            self._metrics.set_compensation_overdue(overdue)
            self._metrics.set_compensation_future_retry(future_retry)
        except Exception:
            logger.warning("补偿队列观测采集失败，本轮不写样本", exc_info=True)

        # unresolved 是全局快照，按轮次写一次：挂到工单状态变更上会对每条工单各写
        # 一次（写放大且无收益）；补偿轮次是按需触发的天然周期点，采样频率有界。
        try:
            self._metrics.set_unresolved(await self._store.count_unresolved())
        except Exception:
            logger.warning("未解决工单数采集失败，本轮不写样本", exc_info=True)

    async def compensate(
        self,
        ticket_id: UUID,
        *,
        owner: str | None = None,
        trace_id: str | None = None,
    ) -> CompensationReport:
        """倒序消费补偿队列；全部成功 → 已回滚，任一失败 → 需人工处置。"""

        held = False
        lease_stop: asyncio.Event | None = None
        lease_task: asyncio.Task[None] | None = None
        if owner is not None:
            if not await self._leases.acquire(ticket_id, owner, ttl_seconds=self._lease_ttl):
                raise LeaseNotHeld(f"工单 {ticket_id} 的补偿执行未取得租约")
            held = True
            # 多步补偿的总时长可能超过租约 TTL：不续期就会被别的执行器接管并重跑。
            lease_stop = asyncio.Event()
            lease_task = asyncio.create_task(
                self._renew_ticket_lease(ticket_id, owner, lease_stop)
            )
        try:
            ticket = await self._store.get_ticket(ticket_id)
            if TicketState(ticket.state) is not TicketState.COMPENSATING:
                raise RuntimeGuardError(
                    f"工单 {ticket_id} 当前为 {ticket.state}，不在「补偿中」"
                )

            owner_name = owner or "compensator"
            # 观测旁路：每轮一次，先于认领，采集失败不影响后续补偿
            await self._observe_compensation()
            # 启动/重试恢复入口：心跳过期的 running 先重新入队，再按 DESC 续跑
            reclaimed = await self._queue.reclaim_stale(
                ticket_id, stale_after_seconds=self._comp_stale
            )
            if reclaimed:
                self._alerts.emit(
                    Alert(
                        "P1",
                        "COMPENSATION_STALE_RECLAIMED",
                        f"工单 {ticket_id} 回收 {len(reclaimed)} 个 stale running 补偿任务",
                        subject=str(ticket_id),
                    )
                )

            tasks = await self._queue.enqueue(ticket_id)
            for _ in range(len(tasks) * self._max_comp_attempts + 1):
                task = await self._queue.claim_next(ticket_id, owner_name)
                if task is None:
                    break
                if task.attempts > self._max_comp_attempts:
                    await self._queue.mark_failed(
                        task.id,
                        "补偿尝试次数超限",
                        max_attempts=1,
                        owner=owner_name,
                    )
                    self._alerts.emit(
                        Alert(
                            "P0",
                            "COMPENSATION_ATTEMPTS_EXCEEDED",
                            f"工单 {ticket_id} 补偿 seq={task.compensate_seq} 超过 "
                            f"{self._max_comp_attempts} 次，转人工",
                            subject=str(ticket_id),
                        )
                    )
                    await self._store.record_compensation(
                        ticket_id=ticket_id,
                        compensate_seq=task.compensate_seq,
                        state="attempts_exceeded",
                        trace_id=trace_id,
                    )
                    continue
                await self._queue.heartbeat(task.id, owner_name)
                await self._run_compensation(
                    ticket_id, task, owner=owner_name, trace_id=trace_id
                )

            counts = await self._queue.counts(ticket_id)
            steps = await self._store.list_steps(ticket_id)
            recovery_threshold_hit = any(
                step.state == "unresolved"
                and step.error_code == RECOVERY_THRESHOLD_ERROR
                for step in steps
            )
            if counts.failed or counts.running or counts.queued or recovery_threshold_hit:
                failed = max(1, counts.failed)
                await self._store.transition(
                    ticket_id,
                    TicketState.MANUAL_REQUIRED,
                    "compensation_failed",
                    context=TransitionContext(compensation_failed=failed),
                    trace_id=trace_id,
                )
                return CompensationReport(
                    ticket_id=ticket_id,
                    status=TicketState.MANUAL_REQUIRED,
                    order=tuple(task.compensate_seq for task in tasks),
                    done=counts.done,
                    failed=failed,
                )

            await self._store.transition(
                ticket_id,
                TicketState.ROLLED_BACK,
                "all_compensated",
                context=TransitionContext(compensation_failed=0),
                trace_id=trace_id,
            )
            return CompensationReport(
                ticket_id=ticket_id,
                status=TicketState.ROLLED_BACK,
                order=tuple(task.compensate_seq for task in tasks),
                done=counts.done,
                failed=0,
            )
        finally:
            if lease_task is not None and lease_stop is not None:
                lease_stop.set()
                lease_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await lease_task
            if held:
                await self._leases.release(ticket_id, owner)  # type: ignore[arg-type]

    async def recover_stale_compensations(
        self, *, stale_after_seconds: float | None = None
    ) -> tuple[CompensationTaskView, ...]:
        """启动恢复入口：全库扫描心跳过期的 running 并重新入队。"""

        reclaimed = await self._queue.reclaim_stale_all(
            stale_after_seconds=(
                self._comp_stale if stale_after_seconds is None else stale_after_seconds
            )
        )
        if reclaimed:
            self._alerts.emit(
                Alert(
                    "P1",
                    "COMPENSATION_STALE_RECLAIMED",
                    f"启动恢复：回收 {len(reclaimed)} 个 stale running 补偿任务",
                )
            )
        return tuple(reclaimed)

    async def _renew_ticket_lease(
        self, ticket_id: UUID, owner: str, stop: asyncio.Event
    ) -> None:
        """补偿循环期间按 TTL/3 续工单租约；停止事件置位即退出。"""

        interval = max(1.0, self._lease_ttl / 3)
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval)
                return
            except TimeoutError:
                pass
            try:
                renewed = await self._leases.renew(
                    ticket_id, owner, ttl_seconds=self._lease_ttl
                )
            except Exception:
                logger.exception("工单 %s 补偿租约续期异常", ticket_id)
                continue
            if not renewed:
                logger.warning("工单 %s 补偿租约已被接管，停止续期", ticket_id)
                return

    async def _compensate_with_heartbeat(
        self, task_id: UUID, owner: str, call: CompensateCall, idem: str
    ) -> CompensateResult:
        """单次补偿调用期间持续写任务心跳，避免 stale 阈值内被重认领。"""

        if self._compensator is None:
            raise RuntimeGuardError("未注入补偿执行器")
        interval = max(1.0, min(20.0, self._comp_stale / 3))
        call_task = asyncio.ensure_future(self._compensator(call, idem))
        try:
            while True:
                done, _ = await asyncio.wait({call_task}, timeout=interval)
                if done:
                    return call_task.result()
                alive = await self._queue.heartbeat(task_id, owner)
                if not alive:
                    raise LeaseNotHeld(
                        f"补偿任务 {task_id} 心跳失效（已被接管），放弃本次结果"
                    )
        finally:
            if not call_task.done():
                call_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await call_task

    async def _run_compensation(
        self,
        ticket_id: UUID,
        task: CompensationTaskView,
        *,
        owner: str,
        trace_id: str | None,
    ) -> None:
        step = await self._store.get_step(task.original_step)
        if step.write_level > 2:
            await self._queue.mark_failed(
                task.id,
                "补偿等级高于原动作（不可执行）",
                max_attempts=1,
                owner=owner,
            )
            return
        call = CompensateCall(
            action_key=step.action_key,
            payload=dict(step.request_snapshot or {}),
            original_idem_key=step.idempotency_key,
            target_system=step.target_system,
            external_id=step.after_ref,
        )
        idem = f"{step.idempotency_key}:undo"
        try:
            if self._compensator is None:
                raise RuntimeGuardError("未注入补偿执行器")
            # 单次外部调用可能超过 stale 阈值：执行期间持续续心跳，
            # 否则行会被别的实例判死重认领（at-least-once 之外的额外重复）。
            result = await self._compensate_with_heartbeat(task.id, owner, call, idem)
        except Exception as exc:  # noqa: BLE001
            await self._queue.mark_failed(
                task.id,
                f"{type(exc).__name__}: {exc}",
                max_attempts=self._max_comp_attempts,
                retry_in_seconds=self._comp_retry,
                owner=owner,
            )
            await self._store.record_compensation(
                ticket_id=ticket_id,
                compensate_seq=task.compensate_seq,
                state="retry_or_failed",
                trace_id=trace_id,
            )
            await self._sleep(self._comp_retry)
            return

        if result.status in {"compensated", "unchanged"}:
            await self._queue.mark_done(task.id, owner=owner)
            await self._store.record_compensation(
                ticket_id=ticket_id,
                compensate_seq=task.compensate_seq,
                state=result.status,
                trace_id=trace_id,
            )
            return

        await self._queue.mark_failed(
            task.id,
            f"补偿未成功: {result.status}",
            max_attempts=self._max_comp_attempts,
            retry_in_seconds=self._comp_retry,
            owner=owner,
        )
        await self._sleep(self._comp_retry)

    # ---------------------------------------------------------------- 内部
    async def _verify(self, planned: PlannedStep) -> VerifyResult:
        """只接受「按唯一键（幂等键/外部单号）」的确认，避免凭印象判 match。"""

        if self._verifier is None:
            raise RuntimeGuardError("未注入 verifier，无法判定未知结果")
        spec = planned.action.verify
        fields = set(getattr(spec, "fields", ()) or ())
        if (
            spec is None
            or getattr(spec, "kind", "") not in UNIQUE_VERIFY_KINDS
            or not ({"external_id", "idem_key"} & fields)
        ):
            self._alerts.emit(
                Alert(
                    "P1",
                    "VERIFY_SPEC_NOT_UNIQUE",
                    f"动作 {planned.action.key} 的 verify 未按唯一键确认，按 unresolved 处理",
                    subject=planned.idem_key,
                )
            )
            return VerifyResult(
                status="mismatch",
                observed={"reason": "verify_spec_not_unique",
                          "kind": getattr(spec, "kind", None)},
            )
        payload = dict(planned.verify_payload or planned.payload)
        payload.setdefault("idem_key", planned.idem_key)
        return await self._verifier(replace(planned, verify_payload=payload))

    async def _recover(
        self,
        *,
        ticket_id: UUID,
        step_id: UUID,
        step_no: int,
        planned: PlannedStep,
        observed: VerifyResult,
        attempts: int = 1,
        trace_id: str | None = None,
    ) -> StepOutcome:
        """外部已成功、本地未提交：写显式 recovered 事件；超阈值强制转人工。"""

        prior_step = await self._store.count_recoveries(ticket_id, step_no=step_no)
        prior_today = await self._store.count_recoveries(ticket_id, today_only=True)
        if (
            prior_step >= self._recovery_step_limit
            or prior_today >= self._recovery_daily_limit
        ):
            await self._store.mark_step(
                step_id=step_id,
                state="unresolved",
                error_code=RECOVERY_THRESHOLD_ERROR,
            )
            self._alerts.emit(
                Alert(
                    "P0",
                    "RECOVERY_THRESHOLD_EXCEEDED",
                    f"工单 {ticket_id} 步骤 {step_no} 反复 recovered"
                    f"（同步骤 {prior_step} 次 / 当日 {prior_today} 次），强制转人工",
                    subject=str(ticket_id),
                )
            )
            return StepOutcome(
                step_no=step_no,
                action_key=planned.action.key,
                state="unresolved",
                fail_code="F6",
                attempts=attempts,
            )

        await self._store.mark_step(
            step_id=step_id,
            state="succeeded",
            response_snapshot={"recovered_by_verify": dict(observed.observed or {})},
        )
        await self._store.record_recovery(
            ticket_id=ticket_id,
            step_no=step_no,
            idem_key=planned.idem_key,
            verify_observed=dict(observed.observed or {}),
            prior_recoveries=prior_step,
            trace_id=trace_id,
        )
        self._alerts.emit(
            Alert(
                "info",
                "STEP_RECOVERED",
                f"工单 {ticket_id} 步骤 {step_no} 由 verify 确认后恢复",
                subject=planned.idem_key,
            )
        )
        return StepOutcome(
            step_no=step_no,
            action_key=planned.action.key,
            state="succeeded",
            recovered=True,
            attempts=attempts,
        )
