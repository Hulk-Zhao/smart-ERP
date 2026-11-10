"""三级回滚（FR-1302~1305）：由审计记录与步骤推导反向操作，只走 WriteGateway。

边界（路线 §2.4）：运行时决定「该不该补偿」，审计流决定「回滚如何被记录与执行」。
本模块不直接调用连接器，也不存在 W3 执行分支：

- 原步骤 W3 → 整单拒绝回滚（``RollbackForbidden``）；
- 反向动作 W3 → 整单拒绝；
- 反向动作等级不得高于原动作（tech 01 §3.1.5）；
- 每次实际回写都经 ``WriteGateway.execute``（预演 → 写入 → 校验 → 审批/等级校验），
  成功后再追加一条审计影像，回滚本身因此同样可查。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Callable, Mapping, Sequence
from uuid import UUID

from apps.audit.ledger import AuditLedger
from apps.platform.policy.gateway import WriteGateway
from apps.platform.policy.levels import ActionSpec, SceneSpec, WriteLevel
from apps.platform.policy.registry import ActionRegistry
from apps.platform.contracts.audit import AuditImage

AUTO_COMPENSATIONS: frozenset[str] = frozenset(
    {"delete_draft", "release_reservation", "cancel_request"}
)


class RollbackError(Exception):
    """回滚被拒。"""


class RollbackForbidden(RollbackError):
    """W3 或违反「反向等级不高于原等级」：无执行路径，整单拒绝。"""


class RollbackRejected(RollbackError):
    """流程前置未满足（如 L2 未人工确认、反向动作不可执行）。"""


class RollbackKind(str, Enum):
    AUTO = "auto"
    MANUAL = "manual"
    IMPOSSIBLE = "impossible"
    FORBIDDEN = "forbidden"


@dataclass(frozen=True, slots=True)
class StepWrite:
    """一次已成功的写入步骤：ticket_step 的投影 + 对应审计影像。"""

    step_id: UUID
    step_no: int
    action_key: str
    level: WriteLevel
    idem_key: str
    audit_image: AuditImage

    def __post_init__(self) -> None:
        if not self.action_key.strip():
            raise ValueError("action_key 不得为空")
        if not self.idem_key.strip():
            raise ValueError("idem_key 不得为空（补偿键 = 原键 + ':undo'）")


@dataclass(frozen=True, slots=True)
class RollbackItem:
    step: StepWrite
    kind: RollbackKind
    action: ActionSpec | None
    reason: str = ""

    @property
    def action_key(self) -> str:
        return self.step.action_key


@dataclass(frozen=True, slots=True)
class RollbackPlan:
    ticket_id: UUID
    items: tuple[RollbackItem, ...]

    @property
    def has_forbidden(self) -> bool:
        return any(item.kind is RollbackKind.FORBIDDEN for item in self.items)

    @property
    def auto_items(self) -> tuple[RollbackItem, ...]:
        return tuple(item for item in self.items if item.kind is RollbackKind.AUTO)

    @property
    def manual_items(self) -> tuple[RollbackItem, ...]:
        return tuple(item for item in self.items if item.kind is RollbackKind.MANUAL)

    @property
    def impossible_items(self) -> tuple[RollbackItem, ...]:
        return tuple(item for item in self.items if item.kind is RollbackKind.IMPOSSIBLE)

    def summary(self) -> dict[str, object]:
        """FR-1305 影响评估：与执行共用同一份推导结果，避免口径漂移。"""

        return {
            "ticket_id": str(self.ticket_id),
            "one_click_allowed": not self.has_forbidden and not self.impossible_items,
            "items": [
                {
                    "step_no": item.step.step_no,
                    "action_key": item.action_key,
                    "kind": item.kind.value,
                    "reason": item.reason,
                    "object_id": item.step.audit_image.object_id,
                    "system": item.step.audit_image.system,
                }
                for item in self.items
            ],
        }


@dataclass(frozen=True, slots=True)
class ManualTask:
    """L3：不可回滚项的处置任务，必须有责任人（FR-1304）。"""

    ticket_id: UUID
    step_id: UUID
    action_key: str
    reason: str
    requires_owner: bool = True


@dataclass(frozen=True, slots=True)
class ExecutedRollback:
    item: RollbackItem
    level: WriteLevel
    audit_image: AuditImage


@dataclass(frozen=True, slots=True)
class RollbackResult:
    executed: tuple[ExecutedRollback, ...]
    manual_tasks: tuple[ManualTask, ...]

    @property
    def rolled_back(self) -> bool:
        return not self.manual_tasks and bool(self.executed)


class RollbackPlanner:
    """纯推导：不写库、不调连接器。"""

    def __init__(self, registry: ActionRegistry) -> None:
        self._registry = registry

    def plan(self, ticket_id: UUID, steps: Sequence[StepWrite]) -> RollbackPlan:
        items = [
            self._classify(step)
            for step in sorted(steps, key=lambda step: step.step_no, reverse=True)
        ]
        return RollbackPlan(ticket_id=ticket_id, items=tuple(items))

    def _classify(self, step: StepWrite) -> RollbackItem:
        level = WriteLevel.parse(step.level)
        if level is WriteLevel.W3:
            return RollbackItem(step, RollbackKind.FORBIDDEN, None, "原步骤为 W3，禁止回滚")

        try:
            action = self._registry.get(step.action_key)
        except KeyError:
            return RollbackItem(
                step, RollbackKind.IMPOSSIBLE, None, "动作未注册，无法确定反向操作"
            )

        if WriteLevel.parse(action.level) is WriteLevel.W3:
            return RollbackItem(
                step, RollbackKind.FORBIDDEN, action, "原动作声明为 W3，禁止回滚"
            )
        if action.compensation is None:
            return RollbackItem(
                step, RollbackKind.IMPOSSIBLE, action, "动作未声明补偿（FR-1102）"
            )

        inverse = self._inverse_of(action)
        if inverse is None:
            return RollbackItem(
                step,
                RollbackKind.IMPOSSIBLE,
                action,
                f"补偿动作 {action.compensation.action_key} 未注册",
            )
        inverse_level = WriteLevel.parse(inverse.level)
        if inverse_level is WriteLevel.W3:
            return RollbackItem(
                step, RollbackKind.FORBIDDEN, inverse, "反向动作是 W3，禁止执行"
            )
        if inverse_level > level:
            return RollbackItem(
                step,
                RollbackKind.FORBIDDEN,
                inverse,
                f"反向动作 {inverse_level.name} 高于原动作 {level.name}（§3.1.5）",
            )

        kind = (
            RollbackKind.AUTO
            if action.compensation.kind in AUTO_COMPENSATIONS
            else RollbackKind.MANUAL
        )
        return RollbackItem(step, kind, inverse, action.compensation.kind)

    def _inverse_of(self, action: ActionSpec) -> ActionSpec | None:
        compensation = action.compensation
        if compensation is None:
            return None
        if compensation.action_key:
            try:
                return self._registry.get(compensation.action_key)
            except KeyError:
                return None
        # 未显式登记反向动作时，按「同连接器 + 同别名 + 补偿类型」推导标准动作 key；
        # 等级/字段沿用原动作声明（只降不升，§3.1.5）。
        return ActionSpec(
            key=_compensation_key(action.key, compensation.kind),
            level=WriteLevel.parse(action.level),
            writable_fields=action.writable_fields,
            forbidden_fields=action.forbidden_fields,
            compensation=None,
            verify=action.verify,
            rate_limit=action.rate_limit,
            idempotent=action.idempotent,
        )


PayloadBuilder = Callable[[RollbackItem], Mapping[str, object]]


class RollbackService:
    """执行端：唯一回写通道是 ``WriteGateway``。"""

    def __init__(
        self,
        *,
        gateway: WriteGateway,
        ledger: AuditLedger,
        scene: SceneSpec,
    ) -> None:
        self._gateway = gateway
        self._ledger = ledger
        self._scene = scene

    async def execute(
        self,
        plan: RollbackPlan,
        *,
        confirmed_manual: bool = False,
        payload_builder: PayloadBuilder | None = None,
    ) -> RollbackResult:
        if plan.has_forbidden:
            forbidden = [
                f"step#{item.step.step_no}:{item.reason}"
                for item in plan.items
                if item.kind is RollbackKind.FORBIDDEN
            ]
            raise RollbackForbidden("回滚被硬拒绝: " + "; ".join(forbidden))

        if plan.manual_items and not confirmed_manual:
            raise RollbackRejected(
                "L2 回滚需人工确认（FR-1303）："
                + ", ".join(item.action_key for item in plan.manual_items)
            )

        executed: list[ExecutedRollback] = []
        manual_tasks: list[ManualTask] = []
        builder = payload_builder or _default_payload

        for item in plan.items:
            if item.kind is RollbackKind.IMPOSSIBLE:
                manual_tasks.append(
                    ManualTask(
                        ticket_id=plan.ticket_id,
                        step_id=item.step.step_id,
                        action_key=item.action_key,
                        reason=item.reason,
                    )
                )
                await self._audit_manual_task(plan.ticket_id, item)
                continue
            if item.action is None:
                raise RollbackRejected(f"{item.action_key} 缺少可执行的反向动作")

            result = await self._gateway.execute(
                item.action,
                builder(item),
                ticket_id=str(plan.ticket_id),
                idem_key=f"{item.step.idem_key}:undo",
                scene=self._scene,
            )
            image = await self._ledger.append_image(
                tenant_id=item.step.audit_image.tenant_id,
                ticket_id=plan.ticket_id,
                step_id=item.step.step_id,
                system=item.step.audit_image.system,
                object_type=f"{item.step.audit_image.object_type}:rollback",
                object_id=item.step.audit_image.object_id,
                before=result.preview.before,
                after=result.write.response,
                before_ref=result.write.before_ref,
                after_ref=result.write.after_ref,
            )
            executed.append(
                ExecutedRollback(item=item, level=result.level, audit_image=image)
            )

        return RollbackResult(executed=tuple(executed), manual_tasks=tuple(manual_tasks))

    async def _audit_manual_task(self, ticket_id: UUID, item: RollbackItem) -> None:
        image = item.step.audit_image
        await self._ledger.append_image(
            tenant_id=image.tenant_id,
            ticket_id=ticket_id,
            step_id=item.step.step_id,
            system=image.system,
            object_type=f"{image.object_type}:manual_task",
            object_id=image.object_id,
            before={"rollback": "not_executed"},
            after={"rollback": "manual_task", "reason": item.reason},
            field_diff=None,
        )


def _default_payload(item: RollbackItem) -> Mapping[str, object]:
    """反向动作的最小入参：以 business key / draft_id 指回原对象。"""

    assert item.action is not None
    fields = item.action.writable_fields
    if not fields:
        raise RollbackRejected(f"{item.action.key} 未声明可写字段，无法生成反向入参")
    key = "draft_id" if "draft_id" in fields else sorted(fields)[0]
    return {key: item.step.audit_image.object_id}


def _compensation_key(action_key: str, kind: str) -> str:
    """``{connector}.{alias}.{action}`` → ``{connector}.{alias}.{kind}``。"""

    connector, alias, _ = action_key.split(".")
    return f"{connector}.{alias}.{kind}"
