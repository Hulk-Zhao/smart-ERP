"""C4/C2 的写入闸门：``WriteGateway.execute()`` 是全系统唯一的外部写出口。

不可绕过的四层（§3.2.1）：
1. 场景上限 ``SceneSpec.max_level`` —— 代码常量；
2. 四路取严 ``min(场景, 连接器, 动作, 熔断)`` —— 配置只能降；
3. 字段 scope 与资金字段校验；
4. W3 无分支拒绝 —— 没有 ``bypass``/``force`` 参数，没有环境变量开关。

写入顺序固定为 预演 → 写入 → 校验；预演拿不到前像即零写入（FR-1102 / §3.4.2）。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from apps.platform.connectors.sdk import (
    Connector,
    PreviewResult,
    VerifyResult,
    WriteResult,
)
from apps.platform.contracts.errors import ErrorCode
from apps.platform.policy.levels import (
    ActionSpec,
    SceneSpec,
    WriteLevel,
    effective_level,
)


class WriteGatewayError(Exception):
    """闸门拒绝写入。``code`` 对应 C10 错误码。"""

    code: ErrorCode

    def __init__(self, message: str, code: ErrorCode) -> None:
        super().__init__(message)
        self.code = code


class ForbiddenWriteError(WriteGatewayError):
    """W3 或资金类字段触碰 → 安全事件，无重试。"""

    def __init__(self, action_key: str) -> None:
        super().__init__(
            f"W3 动作 {action_key} 不存在执行路径（FR-1406/1407）",
            ErrorCode.POLICY_W3,
        )


class ApprovalRequiredError(WriteGatewayError):
    def __init__(self, ticket_id: str, level: WriteLevel) -> None:
        super().__init__(
            f"工单 {ticket_id} 的 {level.name} 写入未满足审批（G-04/FR-1403）",
            ErrorCode.POLICY_APPROVAL,
        )


class FieldScopeViolation(WriteGatewayError):
    def __init__(self, message: str) -> None:
        super().__init__(message, ErrorCode.AUTHZ_FIELD)


class PreviewFailedError(WriteGatewayError):
    """预演失败 → 工单必须终止，零写入。"""

    def __init__(self, action_key: str, reason: str) -> None:
        super().__init__(
            f"动作 {action_key} 预演失败（{reason}），本次零写入", ErrorCode.DATA_MISSING
        )


class VerifyMismatchError(WriteGatewayError):
    def __init__(self, action_key: str) -> None:
        super().__init__(f"动作 {action_key} 写入后校验不一致（F6）", ErrorCode.VERIFY_MISMATCH)


class BreakerState:
    """熔断档位（G-07）。状态真源在 PG ``breaker_state``，由 scheduler 写。"""

    HEALTHY = "none"
    W0_ONLY = "W0_only"
    READONLY = "readonly"


BREAKER_CAP: dict[str, WriteLevel] = {
    BreakerState.HEALTHY: WriteLevel.W2,  # 无降级：上限交给 min() 的其余三路
    BreakerState.W0_ONLY: WriteLevel.W0,
    BreakerState.READONLY: WriteLevel.W0,
}


class ApprovalGate(Protocol):
    def is_satisfied(self, ticket_id: str, level: WriteLevel) -> bool: ...


class DenyAllApprovals:
    """默认实现：没有审批表数据就一律拒绝 >= W1 的写入。"""

    def is_satisfied(self, ticket_id: str, level: WriteLevel) -> bool:
        return False


@dataclass(frozen=True, slots=True)
class GatewayResult:
    level: WriteLevel
    write: WriteResult
    verify: VerifyResult
    preview: PreviewResult


class WriteGateway:
    """唯一写出口。构造参数里没有、也不允许出现「允许 W3」之类的开关。"""

    def __init__(
        self,
        connector: Connector,
        *,
        connector_cap: Callable[[str], WriteLevel] | None = None,
        breaker_cap: Callable[[str], WriteLevel] | None = None,
        approvals: ApprovalGate | None = None,
        narrowed_fields: frozenset[str] | None = None,
    ) -> None:
        self._connector = connector
        self._connector_cap = connector_cap or (lambda action_key: WriteLevel.W2)
        self._breaker_cap = breaker_cap or (lambda scene_code: WriteLevel.W2)
        self._approvals = approvals or DenyAllApprovals()
        # 审批单据给出的字段子集：只可能进一步收窄写入面，不参与 effective_level 计算，
        # 因此不会放宽写等级——W3 仍然在 execute 里直接拒绝。
        self._narrowed_fields = None if narrowed_fields is None else frozenset(narrowed_fields)

    async def execute(
        self,
        action: ActionSpec,
        payload: Mapping[str, Any],
        *,
        ticket_id: str,
        idem_key: str,
        scene: SceneSpec,
    ) -> GatewayResult:
        level = effective_level(
            scene_cap=scene.max_level,
            connector_cap=self._connector_cap(action.key),
            action_level=action.level,
            breaker_cap=self._breaker_cap(scene.code),
        )

        # W3 无执行分支：这里之后不存在任何「继续执行」的路径。
        if level is WriteLevel.W3:
            raise ForbiddenWriteError(action.key)

        if level >= WriteLevel.W1 and not self._approvals.is_satisfied(ticket_id, level):
            raise ApprovalRequiredError(ticket_id, level)

        self._assert_field_scope(action, payload)
        self._assert_no_forbidden(action, payload)

        preview = await self._connector.preview(action, payload)
        if preview.before is None:
            raise PreviewFailedError(action.key, "未取得写前快照")

        write = await self._connector.write(
            action, payload, ticket_id=ticket_id, idem_key=idem_key
        )
        verify = await self._connector.verify(action.verify or preview.verify_spec, payload)
        if not verify.matches():
            raise VerifyMismatchError(action.key)

        return GatewayResult(level=level, write=write, verify=verify, preview=preview)

    def _assert_field_scope(self, action: ActionSpec, payload: Mapping[str, Any]) -> None:
        extra = set(payload) - set(action.writable_fields)
        if extra:
            raise FieldScopeViolation(
                f"动作 {action.key} 接到的字段超出声明 scope: {sorted(extra)}"
            )
        if self._narrowed_fields is not None:
            outside = set(payload) - self._narrowed_fields
            if outside:
                raise FieldScopeViolation(
                    f"动作 {action.key} 接到的字段超出审批给定的范围: {sorted(outside)}"
                )

    @staticmethod
    def _assert_no_forbidden(action: ActionSpec, payload: Mapping[str, Any]) -> None:
        forbidden = set(payload) & set(action.forbidden_fields)
        if forbidden:
            raise FieldScopeViolation(
                f"动作 {action.key} 命中资金/只读字段: {sorted(forbidden)}"
            )


async def execute_write(
    gateway: WriteGateway,
    action: ActionSpec,
    payload: Mapping[str, Any],
    *,
    ticket_id: str,
    idem_key: str,
    scene: SceneSpec,
) -> GatewayResult:
    """便捷包装；除此之外不应存在第二个写入口。"""

    return await gateway.execute(
        action, payload, ticket_id=ticket_id, idem_key=idem_key, scene=scene
    )
