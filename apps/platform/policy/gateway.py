"""C4/C2 的写入闸门：``WriteGateway.execute()`` 是全系统唯一的外部写出口。

不可绕过的四层（§3.2.1）：
1. 场景上限 ``SceneSpec.max_level`` —— 代码常量；
2. 四路取严 ``min(场景, 连接器, 动作, 熔断)`` —— 配置只能降；
3. 字段 scope 与资金字段校验；
4. W3 无分支拒绝 —— 没有 ``bypass``/``force`` 参数，没有环境变量开关。

写入顺序固定为 预演 → 写入 → 校验；预演拿不到前像即零写入（FR-1102 / §3.4.2）。
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from time import monotonic
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
from observability.metrics import MetricsRegistry

logger = logging.getLogger("erp.platform.policy.gateway")

_DEFAULT_METRICS: MetricsRegistry | None = None


def default_metrics_registry() -> MetricsRegistry:
    """进程内唯一注册表：写阶段与补偿采集必须落在同一实例，否则 ``/metrics`` 只看到一半。"""

    global _DEFAULT_METRICS
    if _DEFAULT_METRICS is None:
        _DEFAULT_METRICS = MetricsRegistry()
    return _DEFAULT_METRICS


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
        metrics: MetricsRegistry | None = None,
    ) -> None:
        self._connector = connector
        self._connector_cap = connector_cap or (lambda action_key: WriteLevel.W2)
        self._breaker_cap = breaker_cap or (lambda scene_code: WriteLevel.W2)
        self._approvals = approvals or DenyAllApprovals()
        #: B 级（服务账号降级）租约上的字段白名单；None 表示 A 级，不额外收窄。
        self._narrowed_fields = frozenset(narrowed_fields) if narrowed_fields is not None else None
        self._metrics = metrics if metrics is not None else default_metrics_registry()

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
        self._assert_narrowed_fields(action, payload)
        self._assert_no_forbidden(action, payload)

        # 埋点只包「四路取严」之后的真实阶段调用，不按 effective_level 预判。
        started = monotonic()
        try:
            preview = await self._connector.preview(action, payload)
        except Exception:
            self._record_stage("preview", monotonic() - started, ok=False)
            raise
        # create 不是「无前像」：u8 在草稿不存在时合成 {"exists": False} 前像
        # （connectors/u8/connector.py:274-279）；before=None 只表示拿不到业务键/
        # 取快照失败，此时网关零写入并抛 PreviewFailedError，属于失败路径。
        snapshot_ok = preview.before is not None
        self._record_stage("preview", monotonic() - started, ok=snapshot_ok)
        if not snapshot_ok:
            raise PreviewFailedError(action.key, "未取得写前快照")

        started = monotonic()
        try:
            write = await self._connector.write(
                action, payload, ticket_id=ticket_id, idem_key=idem_key
            )
        except Exception:
            self._record_stage("write", monotonic() - started, ok=False)
            raise
        self._record_stage("write", monotonic() - started, ok=True)

        started = monotonic()
        try:
            verify = await self._connector.verify(
                action.verify or preview.verify_spec, payload
            )
        except Exception:
            self._record_stage("verify", monotonic() - started, ok=False)
            raise
        verified = verify.matches()
        self._record_stage("verify", monotonic() - started, ok=verified)
        if not verified:
            raise VerifyMismatchError(action.key)

        return GatewayResult(level=level, write=write, verify=verify, preview=preview)

    def _record_stage(self, stage: str, duration_seconds: float, *, ok: bool) -> None:
        """写阶段计数旁路。

        观测不得影响写路径：指标异常一律吞掉并记日志，既不改变本方法返回值，
        也不会替换掉调用方本应看到的原始异常。``ok=False`` 与阶段真实结果一致。
        """

        try:
            self._metrics.record_stage(stage, duration_seconds, ok=ok)
        except Exception:
            logger.warning("写阶段指标写入失败（stage=%s），已忽略", stage, exc_info=True)

    @staticmethod
    def _assert_field_scope(action: ActionSpec, payload: Mapping[str, Any]) -> None:
        extra = set(payload) - set(action.writable_fields)
        if extra:
            raise FieldScopeViolation(
                f"动作 {action.key} 接到的字段超出声明 scope: {sorted(extra)}"
            )

    def _assert_narrowed_fields(
        self, action: ActionSpec, payload: Mapping[str, Any]
    ) -> None:
        """B 级租约的字段白名单：白名单外字段一律拒写（不静默丢弃）。"""

        if self._narrowed_fields is None:
            return
        extra = set(payload) - set(self._narrowed_fields)
        if extra:
            raise FieldScopeViolation(
                f"动作 {action.key} 的入参超出 B 级字段白名单: {sorted(extra)}"
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
