"""工单执行运行时：租约 → intent → 外部写 → verify → 状态机 / Saga 补偿。

写入路径只有 ``WriteGateway.execute`` 一条；本模块额外需要两种**读/补偿**能力：
``Connector.verify``（判定未知结果）与 ``Connector.compensate``（撤销已写入的步骤）。
两者都可注入替身，测试不需要真实系统。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from apps.agents.compensation import CompensationQueue, CompensationTaskView
from apps.agents.events import EventRecord
from apps.agents.failure import Failure, classify_exception, failure_of
from apps.agents.lease import LeaseManager, LeaseNotHeld
from apps.agents.state_machine import DEVIATION_THRESHOLD, TransitionContext
from apps.agents.store import TicketStore
from apps.platform.connectors.sdk import CompensateResult, UnknownOutcome, VerifyResult
from apps.platform.contracts.ticket import TicketState
from apps.platform.policy.gateway import GatewayResult, WriteGateway
from apps.platform.policy.levels import ActionSpec, SceneSpec


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
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], datetime] | None = None,
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
        self._sleep = sleep
        self._clock = clock or (lambda: datetime.now(tz=UTC))

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
                await self._store.mark_step(
                    step_id=existing.id,
                    state="succeeded",
                    response_snapshot={"recovered_by_verify": dict(observed.observed or {})},
                )
                return StepOutcome(
                    step_no=step_no,
                    action_key=planned.action.key,
                    state="succeeded",
                    recovered=True,
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
                    await self._store.mark_step(
                        step_id=step.id,
                        state="succeeded",
                        response_snapshot={
                            "recovered_by_verify": dict(observed.observed or {})
                        },
                    )
                    return StepOutcome(
                        step_no=step_no,
                        action_key=planned.action.key,
                        state="succeeded",
                        recovered=True,
                        attempts=attempt,
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
    async def compensate(
        self,
        ticket_id: UUID,
        *,
        owner: str | None = None,
        trace_id: str | None = None,
    ) -> CompensationReport:
        """倒序消费补偿队列；全部成功 → 已回滚，任一失败 → 需人工处置。"""

        held = False
        if owner is not None:
            if not await self._leases.acquire(ticket_id, owner, ttl_seconds=self._lease_ttl):
                raise LeaseNotHeld(f"工单 {ticket_id} 的补偿执行未取得租约")
            held = True
        try:
            ticket = await self._store.get_ticket(ticket_id)
            if TicketState(ticket.state) is not TicketState.COMPENSATING:
                raise RuntimeGuardError(
                    f"工单 {ticket_id} 当前为 {ticket.state}，不在「补偿中」"
                )

            tasks = await self._queue.enqueue(ticket_id)
            for _ in range(len(tasks) * self._max_comp_attempts + 1):
                task = await self._queue.claim_next(ticket_id)
                if task is None:
                    break
                await self._run_compensation(ticket_id, task, trace_id=trace_id)

            counts = await self._queue.counts(ticket_id)
            if counts.failed or counts.running or counts.queued:
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
            if held:
                await self._leases.release(ticket_id, owner)  # type: ignore[arg-type]

    async def _run_compensation(
        self,
        ticket_id: UUID,
        task: CompensationTaskView,
        *,
        trace_id: str | None,
    ) -> None:
        step = await self._store.get_step(task.original_step)
        if step.write_level > 2:
            await self._queue.mark_failed(
                task.id, "补偿等级高于原动作（不可执行）", max_attempts=1
            )
            return
        call = CompensateCall(
            action_key=step.action_key,
            payload=dict(step.request_snapshot or {}),
            original_idem_key=step.idempotency_key,
        )
        idem = f"{step.idempotency_key}:undo"
        try:
            if self._compensator is None:
                raise RuntimeGuardError("未注入补偿执行器")
            result = await self._compensator(call, idem)
        except Exception as exc:  # noqa: BLE001
            await self._queue.mark_failed(
                task.id,
                f"{type(exc).__name__}: {exc}",
                max_attempts=self._max_comp_attempts,
                retry_in_seconds=self._comp_retry,
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
            await self._queue.mark_done(task.id)
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
        )
        await self._sleep(self._comp_retry)

    # ---------------------------------------------------------------- 内部
    async def _verify(self, planned: PlannedStep) -> VerifyResult:
        if self._verifier is None:
            raise RuntimeGuardError("未注入 verifier，无法判定未知结果")
        return await self._verifier(planned)
