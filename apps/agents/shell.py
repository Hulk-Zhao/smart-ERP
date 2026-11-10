"""AG-1~AG-8 通用执行壳：输入校验 → 熔断 → WriteGateway → F1–F8 分类。

真正的业务逻辑（怎么算差异、怎么定归因）由 S2/S5 等流提供；壳子只保证所有 Agent
在**同一套契约与闸门**下执行：

- 入参必须含 ``scope{org_id, period, system_ids}``（G-05）；
- 金额一律 ``Money{amount, currency, tax_included}``，裸数字拒绝；
- ``confidence < 0.70`` 视为不可用，不得作为结论输出；
- 写入只走 ``WriteGateway``；W3 与「超过场景上限」的动作在调用前就被拒绝；
- 熔断打开时写入 0 次（FR-1107），失败按 F1–F8 统一埋点。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from apps.agents.breaker import CircuitBreaker
from apps.agents.failure import Failure, classify_exception, failure_of
from apps.agents.runtime import ExecutionReport, PlannedStep, StepOutcome, TicketRuntime
from apps.agents.scenes import AgentScene
from apps.platform.contracts.agent import USABLE_CONFIDENCE_MIN, Scope
from apps.platform.contracts.canonical import Money
from apps.platform.contracts.errors import ErrorCode, FailCode
from apps.platform.contracts.ticket import TicketState
from apps.platform.policy.gateway import BreakerState, ForbiddenWriteError, WriteGateway
from apps.platform.policy.levels import WriteLevel


class AgentInputRejected(Exception):
    """入参不满足 PRD §5.0 契约约定，拒绝执行（零写入）。"""

    def __init__(self, message: str, *, code: ErrorCode) -> None:
        super().__init__(message)
        self.code = code


class LowConfidenceError(AgentInputRejected):
    """confidence < 0.70：不可用，不得作为结论输出。"""

    def __init__(self, value: float) -> None:
        super().__init__(
            f"confidence={value} < {USABLE_CONFIDENCE_MIN}，结论不可用",
            code=ErrorCode.DATA_AMBIGUOUS,
        )
        self.confidence = value


class BreakerOpenError(Exception):
    """熔断中：场景已降为只能预演，本次拒绝任何写入（FR-1107）。"""

    code = ErrorCode.POLICY_LEVEL

    def __init__(self, scene_code: str, state: str) -> None:
        super().__init__(f"场景 {scene_code} 熔断档位 {state}，写入为 0")
        self.scene_code = scene_code
        self.state = state


class SceneLevelExceeded(Exception):
    """动作等级高于场景上限（取严在网关内，这里提前给出可读错误）。"""

    code = ErrorCode.POLICY_LEVEL


class AgentRequest(BaseModel):
    """壳子统一入参；``scope`` 无默认值，缺失即校验失败。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    scene_code: str = Field(min_length=1)
    scope: Scope
    trigger_kind: Literal["timer", "event", "manual"]
    business_key: str = Field(min_length=1)
    payload: dict[str, Any] = Field(default_factory=dict)
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    money: dict[str, Money] = Field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class AgentRunResult:
    ticket_id: UUID
    status: TicketState
    fail_code: FailCode | None
    handling: str | None
    outcomes: tuple[StepOutcome, ...]
    invocations: int

    @property
    def failure(self) -> Failure | None:
        return failure_of(self.fail_code) if self.fail_code else None


def validate_input(raw: Mapping[str, Any], scene: AgentScene) -> AgentRequest:
    """契约校验；错误按「范围 / 字段 / 缺数据」三类给出 C10 错误码。"""

    try:
        request = AgentRequest.model_validate(raw)
    except ValidationError as exc:
        fields = {
            str(error["loc"][0]) for error in exc.errors() if error.get("loc")
        }
        if "scope" in fields:
            raise AgentInputRejected(
                f"缺少或非法的 scope{{org_id, period, system_ids}}（G-05）：{exc}",
                code=ErrorCode.AUTHZ_SCOPE,
            ) from exc
        if "money" in fields:
            raise AgentInputRejected(
                f"金额必须是 Money 对象，禁止裸数字：{exc}",
                code=ErrorCode.AUTHZ_FIELD,
            ) from exc
        raise AgentInputRejected(f"入参不合法：{exc}", code=ErrorCode.DATA_MISSING) from exc

    if request.scene_code != scene.scene.code:
        raise AgentInputRejected(
            f"入参场景 {request.scene_code!r} 与执行壳 {scene.scene.code!r} 不一致",
            code=ErrorCode.AUTHZ_SCOPE,
        )

    # 金额对象化：payload 里出现场景声明的金额字段时也必须是 Money 结构
    for field in scene.money_fields:
        if field in request.payload and not isinstance(request.payload[field], Mapping):
            raise AgentInputRejected(
                f"payload.{field} 必须是 Money{{amount,currency,tax_included}}，禁止裸数字",
                code=ErrorCode.AUTHZ_FIELD,
            )

    if request.confidence is not None and request.confidence < USABLE_CONFIDENCE_MIN:
        raise LowConfidenceError(request.confidence)

    return request


class AgentShell:
    """一个场景一个壳；``plan`` 由业务流给出，壳子只做契约与闸门。"""

    def __init__(
        self,
        scene: AgentScene,
        *,
        runtime: TicketRuntime,
        gateway: WriteGateway,
        breaker: CircuitBreaker,
    ) -> None:
        self._scene = scene
        self._runtime = runtime
        self._gateway = gateway
        self._breaker = breaker

    @property
    def scene(self) -> AgentScene:
        return self._scene

    def breaker_cap(self, _scene_code: str) -> WriteLevel:
        """可直接传给 ``WriteGateway(breaker_cap=shell.breaker_cap)``。"""

        return self._breaker.cap(self._scene.scene.code)

    def assert_writable(self, steps: Sequence[PlannedStep]) -> None:
        state = self._breaker.state(self._scene.scene.code)
        if state != BreakerState.HEALTHY:
            raise BreakerOpenError(self._scene.scene.code, state)
        for step in steps:
            if step.action.level is WriteLevel.W3:
                raise ForbiddenWriteError(step.action.key)
            if step.action.level > self._scene.scene.max_level:
                raise SceneLevelExceeded(
                    f"动作 {step.action.key} 等级 {step.action.level.name} 超过场景 "
                    f"{self._scene.scene.code} 上限 {self._scene.scene.max_level.name}"
                )

    async def run(
        self,
        *,
        ticket_id: UUID,
        raw_input: Mapping[str, Any],
        steps: Sequence[PlannedStep],
        owner: str,
        trace_id: str | None = None,
    ) -> AgentRunResult:
        request = validate_input(raw_input, self._scene)
        self.assert_writable(steps)

        try:
            report = await self._runtime.execute(
                ticket_id, owner=owner, steps=steps, scene=self._scene.scene, trace_id=trace_id
            )
        except Exception as exc:
            failure = classify_exception(exc)
            self._breaker.record(
                self._scene.scene.code,
                ok=False,
                fail_code=failure.code if failure else None,
                subject=request.business_key,
            )
            raise

        self._record(report, subject=request.business_key)
        failure = report.failure
        return AgentRunResult(
            ticket_id=ticket_id,
            status=report.status,
            fail_code=report.fail_code,  # type: ignore[arg-type]
            handling=failure.handling if failure else None,
            outcomes=report.outcomes,
            invocations=report.invocations,
        )

    def _record(self, report: ExecutionReport, *, subject: str) -> None:
        ok = report.status is TicketState.COMPLETED
        self._breaker.record(
            self._scene.scene.code,
            ok=ok,
            fail_code=report.fail_code,
            subject=subject,
        )

    def breaker_snapshot(self) -> dict[str, object]:
        return self._breaker.snapshot(self._scene.scene.code)
