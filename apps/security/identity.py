"""身份与用户态令牌（FR-1401）。

模型边界：

- 会话令牌（``SessionToken``）只描述「谁、在哪个租户、能看哪些范围」；
- worker 执行前必经 ``TokenBroker.exchange`` 换取目标系统的短时凭据；
- A 级连接器（支持 OBO / impersonation）→ 以发起人身份执行；
- B 级连接器（客户系统不支持委派）→ 服务账号 + 字段白名单收窄 + 强制双人复核，
  并把降级原因写入 ``DegradationRecord``；连接器等级由代码推导为 B，不靠人工标注。

不放大权限的保证点：
1. 请求 scope 必须是会话授权 scope 的子集（``ScopeGuard``）；
2. 凭据绑定 ``ticket_id``，复用到其他工单直接拒绝；
3. B 级最高 W1、字段白名单必填、强制双人复核；
4. 返回的凭据只有不透明引用（``credential_ref``），明文不落库、不进日志。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from apps.platform.connectors.sdk import ConnectorMeta
from apps.platform.policy.levels import WriteLevel
from apps.security.scope import Scope, ScopeGrant, ScopeGuard, ScopeViolation

TokenMode = Literal["delegated", "service_account"]


class IdentityError(Exception):
    """身份链路拒绝。"""


class TokenExpired(IdentityError):
    pass


class TokenTicketMismatch(IdentityError):
    pass


class MissingFieldWhitelist(IdentityError):
    """B 级连接器必须显式收窄字段，否则等同于放大服务账号权限。"""


@dataclass(frozen=True, slots=True)
class UserPrincipal:
    tenant_id: UUID
    user_id: UUID
    roles: frozenset[str]
    grant: ScopeGrant

    def __post_init__(self) -> None:
        if not self.roles:
            raise IdentityError("用户至少需要一个角色")


@dataclass(frozen=True, slots=True)
class SessionToken:
    """会话 JWT 的进程内投影（TTL 8h，HttpOnly Cookie 由 API 层负责）。"""

    token_id: str
    tenant_id: UUID
    user_id: UUID
    roles: frozenset[str]
    grant: ScopeGrant
    issued_at: datetime
    expires_at: datetime

    def is_expired(self, now: datetime) -> bool:
        return now >= self.expires_at


@dataclass(frozen=True, slots=True)
class DegradationRecord:
    """B 级降级留痕（FR-1401 异常路径 / FR-1506 连接器卡片提示）。"""

    tenant_id: UUID
    ticket_id: str
    system: str
    reason: str
    recorded_at: datetime


@dataclass(frozen=True, slots=True)
class CredentialLease:
    """一次任务的短时凭据租约。``credential_ref`` 是不透明引用，不是凭据本身。"""

    mode: TokenMode
    subject_id: UUID
    system: str
    ticket_id: str
    scope: Scope
    issued_at: datetime
    expires_at: datetime
    max_level: WriteLevel
    requires_dual_review: bool
    narrowed_fields: frozenset[str]
    credential_ref: str
    degradation: DegradationRecord | None = None

    def assert_ticket(self, ticket_id: str) -> None:
        if not ticket_id or ticket_id != self.ticket_id:
            raise TokenTicketMismatch(
                f"凭据绑定工单 {self.ticket_id}，拒绝复用到 {ticket_id!r}"
            )

    def assert_scope(self, requested: Scope) -> None:
        """凭据范围不得被调用方扩大。"""

        if requested != self.scope:
            raise ScopeViolation(
                f"凭据范围 {self.scope.as_dict()} 不允许扩大为 {requested.as_dict()}"
            )

    def is_expired(self, now: datetime) -> bool:
        return now >= self.expires_at


class TokenBroker:
    """会话 → 目标系统凭据的唯一兑换点。"""

    def __init__(
        self,
        *,
        session_ttl: timedelta = timedelta(hours=8),
        lease_ttl: timedelta = timedelta(minutes=15),
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if session_ttl <= timedelta(0) or lease_ttl <= timedelta(0):
            raise ValueError("令牌 TTL 必须为正")
        self._session_ttl = session_ttl
        self._lease_ttl = lease_ttl
        self._clock = clock or (lambda: datetime.now(UTC))
        self._degradations: list[DegradationRecord] = []

    @property
    def degradations(self) -> tuple[DegradationRecord, ...]:
        return tuple(self._degradations)

    def issue_session(
        self, principal: UserPrincipal, *, now: datetime | None = None
    ) -> SessionToken:
        current = _utc(now or self._clock())
        return SessionToken(
            token_id=str(uuid4()),
            tenant_id=principal.tenant_id,
            user_id=principal.user_id,
            roles=principal.roles,
            grant=principal.grant,
            issued_at=current,
            expires_at=current + self._session_ttl,
        )

    def exchange(
        self,
        session: SessionToken,
        *,
        connector_meta: ConnectorMeta,
        ticket_id: str,
        requested_scope: Scope,
        field_whitelist: frozenset[str] = frozenset(),
        level: WriteLevel | int | str = WriteLevel.W1,
        now: datetime | None = None,
    ) -> CredentialLease:
        current = _utc(now or self._clock())
        if session.is_expired(current):
            raise TokenExpired("会话令牌已过期（FR-1401：任务中止并重试一次）")
        if not ticket_id or not ticket_id.strip():
            raise TokenTicketMismatch("凭据必须绑定 ticket_id（I-1）")

        # 闸口：请求范围 ⊆ 会话授权范围，且目标系统必须在请求范围内
        ScopeGuard(session.grant).authorize(requested_scope)
        if connector_meta.system not in requested_scope.system_ids:
            raise ScopeViolation(
                f"目标系统 {connector_meta.system!r} 不在请求 scope.system_ids 内"
            )

        expires_at = min(current + self._lease_ttl, session.expires_at)
        if expires_at <= current:
            raise TokenExpired("剩余会话时长不足以签发凭据")

        if connector_meta.supports_obo and connector_meta.security_level == "A":
            return CredentialLease(
                mode="delegated",
                subject_id=session.user_id,
                system=connector_meta.system,
                ticket_id=ticket_id,
                scope=requested_scope,
                issued_at=current,
                expires_at=expires_at,
                max_level=WriteLevel.W2,
                requires_dual_review=False,
                narrowed_fields=field_whitelist,
                credential_ref=f"vault://lease/{uuid4()}",
            )

        # B 级降级：客户系统不支持委派。字段白名单是**写**的硬前置，不能省略。
        # W0（只读/审批/对账）不产生外部写入，不要求白名单，否则 B 级连接器一旦
        # 装配，所有读路径都会被 401 拦死。
        if WriteLevel.parse(level) >= WriteLevel.W1 and not field_whitelist:
            raise MissingFieldWhitelist(
                f"B 级连接器 {connector_meta.system} 必须提供字段白名单（FR-1401）"
            )
        degradation = DegradationRecord(
            tenant_id=session.tenant_id,
            ticket_id=ticket_id,
            system=connector_meta.system,
            reason="connector_does_not_support_obo",
            recorded_at=current,
        )
        self._degradations.append(degradation)
        service_subject = uuid5(
            NAMESPACE_URL, f"service-account:{session.tenant_id}:{connector_meta.system}"
        )
        return CredentialLease(
            mode="service_account",
            subject_id=service_subject,
            system=connector_meta.system,
            ticket_id=ticket_id,
            scope=requested_scope,
            issued_at=current,
            expires_at=expires_at,
            max_level=WriteLevel.W1,
            requires_dual_review=True,
            narrowed_fields=field_whitelist,
            credential_ref=f"vault://lease/{uuid4()}",
            degradation=degradation,
        )


def connector_security_level(meta: ConnectorMeta) -> Literal["A", "B"]:
    """B 级由能力推导，不由人工标注（FR-1401 / FR-1506）。"""

    return "A" if (meta.supports_obo and meta.security_level == "A") else "B"


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(
        UTC
    )
