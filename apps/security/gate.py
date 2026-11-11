"""权限四道闸的最小实现（FR-1401/1402/1403/1406）。

顺序固定（任一不过即拒绝，不进入 WriteGateway）：

1. **身份**：会话有效、凭据绑定工单、A/B 级由连接器能力推导；
2. **范围**：请求 ``scope{org_id, period, system_ids}`` ⊆ 会话授权范围；
3. **等级**：W3 直接拒绝；请求等级不得超过凭据允许等级（B 级最高 W1）；
4. **审批**：按金额/是否触及应付生成审批链，双人复核且审批人 ≠ 发起人。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from uuid import UUID

from apps.platform.connectors.sdk import ConnectorMeta
from apps.platform.contracts.errors import ErrorCode
from apps.platform.policy.levels import WriteLevel
from apps.security.approvals import ApprovalService
from apps.security.identity import CredentialLease, SessionToken, TokenBroker
from apps.security.scope import Scope, ScopeGuard


class AccessDenied(Exception):
    code: ErrorCode = ErrorCode.AUTHZ_SCOPE

    def __init__(self, message: str) -> None:
        super().__init__(message)


class W3AccessDenied(AccessDenied):
    code = ErrorCode.POLICY_W3


class LevelNotAllowed(AccessDenied):
    code = ErrorCode.POLICY_LEVEL


class ApprovalNotSatisfied(AccessDenied):
    code = ErrorCode.POLICY_APPROVAL


@dataclass(frozen=True, slots=True)
class AccessRequest:
    session: SessionToken
    requested_scope: Scope
    connector_meta: ConnectorMeta
    ticket_id: str
    level: WriteLevel | int | str
    amount: Decimal | int | str
    touches_fund: bool = False
    field_whitelist: frozenset[str] = frozenset()
    #: 凭据等级上限的比对口径；缺省等于 ``level``。
    #: 审批通过执行写计划时传**动作**等级：W2 工单可能只执行 W1 草稿写入，
    #: 治理等级（决定审批链）不该被拿来误判 B 级凭据的写入上限。
    credential_level: WriteLevel | int | str | None = None


@dataclass(frozen=True, slots=True)
class AccessGrant:
    lease: CredentialLease
    required_roles: tuple[str, ...]
    initiator_id: UUID


class AccessGate:
    def __init__(self, *, broker: TokenBroker, approvals: ApprovalService) -> None:
        self._broker = broker
        self._approvals = approvals

    def authorize(self, request: AccessRequest) -> AccessGrant:
        level = WriteLevel.parse(request.level)
        credential_level = (
            level
            if request.credential_level is None
            else WriteLevel.parse(request.credential_level)
        )

        # 闸 3 前置：W3 没有身份、范围、审批可补救，直接拒绝且不换令牌。
        if level is WriteLevel.W3 or credential_level is WriteLevel.W3:
            raise W3AccessDenied("W3 动作无执行路径（FR-1407）")

        # 闸 2：范围独立校验；不依赖调用方是否记得在 SQL 里加条件。
        ScopeGuard(request.session.grant).authorize(request.requested_scope)

        # 闸 1：身份与用户态令牌（B 级自动降级并留痕）。
        lease = self._broker.exchange(
            request.session,
            connector_meta=request.connector_meta,
            ticket_id=request.ticket_id,
            requested_scope=request.requested_scope,
            field_whitelist=request.field_whitelist,
            level=credential_level,
        )

        # 闸 3：连接器/凭据等级上限（B 级只能 W1）。
        if credential_level > lease.max_level:
            raise LevelNotAllowed(
                f"连接器 {lease.system} 当前凭据最高 {lease.max_level.name}，"
                f"拒绝 {credential_level.name} 写入"
            )

        # 闸 4：审批阈值 + 双人复核；B 级强制双人。
        required = self._approvals.required_roles(
            level=level,
            amount=request.amount,
            touches_fund=request.touches_fund,
            force_dual=lease.requires_dual_review,
        )
        if required and not self._approvals.satisfied(
            ticket_id=request.ticket_id,
            level=level,
            amount=request.amount,
            touches_fund=request.touches_fund,
            initiator_id=request.session.user_id,
            force_dual=lease.requires_dual_review,
        ):
            missing = self._approvals.missing_roles(
                ticket_id=request.ticket_id,
                level=level,
                amount=request.amount,
                touches_fund=request.touches_fund,
                initiator_id=request.session.user_id,
                force_dual=lease.requires_dual_review,
            )
            raise ApprovalNotSatisfied(
                f"审批未满足（G-04）：缺少角色 {list(missing)}"
            )

        return AccessGrant(
            lease=lease,
            required_roles=required,
            initiator_id=request.session.user_id,
        )


__all__ = [
    "AccessDenied",
    "AccessGate",
    "AccessGrant",
    "AccessRequest",
    "ApprovalNotSatisfied",
    "LevelNotAllowed",
    "W3AccessDenied",
]
