"""连接器与映射配置的授权闸口（C6）。

PRD §2.3「连接器与映射配置」只授予 IT/CIO；本模块把该行落成可测试的判定：

- **读**：``platform_admin`` 可读全量；``auditor`` 只读且敏感字段脱敏；其余角色拒绝；
- **写**：仅 ``platform_admin``；服务账号与业务角色（发起/复核/审批）一律拒绝；
- **双人**：高风险变更（endpoint / credential / field_mapping）必须由**另一名**
  平台管理员复核，自审拒绝；低风险变更单签即可；
- **审计**：每次成功变更都必须交给 ``ConfigAuditRecorder``，载荷包含
  kind/target/before/after/actor/approver/emergency，供写入 ``config_change`` 链。

本模块不依赖 ``apps/api``，也不直接写库：审计落库由调用方注入 recorder。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol
from uuid import UUID, uuid4

from apps.security.identity import UserPrincipal

#: 可读连接器/映射配置的角色（审计只读需要看配置以核对变更）。
CONFIG_READ_ROLES: frozenset[str] = frozenset({"platform_admin", "auditor"})

#: 可改配置的角色（PRD §2.3：仅 IT/CIO）。
CONFIG_WRITE_ROLES: frozenset[str] = frozenset({"platform_admin"})

#: 高风险变更类别：必须双人。新类别默认按高风险处理（fail-closed）。
HIGH_RISK_KINDS: frozenset[str] = frozenset(
    {"endpoint", "credential", "field_mapping"}
)

#: 低风险变更类别：单签即可。
LOW_RISK_KINDS: frozenset[str] = frozenset({"label", "quota"})

#: 需要脱敏的字段名（顶层键，大小写不敏感）。
SECRET_FIELDS: frozenset[str] = frozenset(
    {"password", "secret", "token", "api_key", "client_secret", "private_key"}
)

MASK = "***"


class ConfigAccessDenied(Exception):
    """配置访问拒绝的基类。"""


class ConfigRoleDenied(ConfigAccessDenied):
    """角色不在读/写白名单。"""


class ConfigDualReviewRequired(ConfigAccessDenied):
    """高风险变更缺少第二名平台管理员复核。"""


class ConfigSelfApprovalRejected(ConfigAccessDenied):
    """配置变更的提交人不得自己复核。"""


class ConfigApproverRoleRequired(ConfigAccessDenied):
    """配置变更的复核人必须是平台管理员。"""


@dataclass(frozen=True, slots=True)
class ConfigChange:
    """一次配置变更的最小描述（与 ``config_change`` 表字段对齐）。"""

    kind: str
    target: str
    before: Mapping[str, Any]
    after: Mapping[str, Any]
    emergency: bool = False
    ticket_id: UUID | None = None

    @property
    def requires_dual_review(self) -> bool:
        return self.kind in HIGH_RISK_KINDS

    @property
    def is_known_kind(self) -> bool:
        return self.kind in HIGH_RISK_KINDS or self.kind in LOW_RISK_KINDS


@dataclass(frozen=True, slots=True)
class ConfigGrant:
    masked_fields: tuple[str, ...] = ()
    requires_dual_review: bool = False


@dataclass(frozen=True, slots=True)
class ConfigAuditEntry:
    change_id: UUID
    tenant_id: UUID
    kind: str
    target: str
    before: dict[str, Any]
    after: dict[str, Any]
    actor_id: UUID
    approver_id: UUID | None
    emergency: bool
    ticket_id: UUID | None


@dataclass(frozen=True, slots=True)
class ConfigChangeResult:
    change_id: UUID
    requires_dual_review: bool
    approver_id: UUID | None
    audit: ConfigAuditEntry


class ConfigAuditRecorder(Protocol):
    """审计落点（测试用内存实现；生产 PG 实现见 ``config_change_chain``）。"""

    def record(self, entry: ConfigAuditEntry) -> None: ...


class AsyncConfigAuditRecorder(Protocol):
    """异步审计落点（asyncpg 写 ``config_change`` 链）。

    只能配合 :meth:`ConfigChangeService.apply_async` 使用；同步的 ``apply``
    遇到它会明确报错，避免「判定通过但审计没落库」。
    """

    async def record_async(self, entry: ConfigAuditEntry) -> None: ...


@dataclass
class InMemoryConfigAuditRecorder:
    entries: list[ConfigAuditEntry] = field(default_factory=list)

    def record(self, entry: ConfigAuditEntry) -> None:
        self.entries.append(entry)


class ConfigGate:
    """配置读/写授权与双人判定。"""

    def authorize_read(self, principal: UserPrincipal) -> ConfigGrant:
        if not (principal.roles & CONFIG_READ_ROLES):
            raise ConfigRoleDenied(
                f"角色 {sorted(principal.roles)} 无连接器/映射配置读权限（PRD §2.3）"
            )
        masked = SECRET_FIELDS if "auditor" in principal.roles else frozenset()
        return ConfigGrant(masked_fields=tuple(sorted(masked)))

    def authorize_write(
        self, principal: UserPrincipal, change: ConfigChange
    ) -> ConfigGrant:
        if not (principal.roles & CONFIG_WRITE_ROLES):
            raise ConfigRoleDenied(
                f"角色 {sorted(principal.roles)} 无连接器/映射配置写权限（PRD §2.3 仅 IT/CIO）"
            )
        if not change.is_known_kind:
            raise ConfigAccessDenied(
                f"未登记的配置变更类别 {change.kind!r}，按高风险处理且拒绝自动放行"
            )
        return ConfigGrant(requires_dual_review=change.requires_dual_review)

    def mask_for(
        self, principal: UserPrincipal, config: Mapping[str, Any]
    ) -> dict[str, Any]:
        """按角色脱敏：审计员看不到凭据类字段。"""

        grant = self.authorize_read(principal)
        masked = {name.lower() for name in grant.masked_fields}
        return {
            key: (MASK if key.lower() in masked else value)
            for key, value in config.items()
        }

    def assert_approved(
        self,
        *,
        actor: UserPrincipal,
        change: ConfigChange,
        approver: UserPrincipal | None,
    ) -> UserPrincipal | None:
        if not change.requires_dual_review or change.emergency:
            return None
        if approver is None:
            raise ConfigDualReviewRequired(
                f"{change.kind} 变更必须由另一名平台管理员复核"
            )
        if approver.user_id == actor.user_id:
            raise ConfigSelfApprovalRejected("配置变更不得自审（config_change 双人约束）")
        if "platform_admin" not in approver.roles:
            raise ConfigApproverRoleRequired(
                f"复核人角色 {sorted(approver.roles)} 不是平台管理员"
            )
        if approver.tenant_id != actor.tenant_id:
            raise ConfigApproverRoleRequired("复核人与提交人不在同一租户")
        return approver


class ConfigChangeService:
    """配置变更的唯一入口：授权 → 双人 → 审计，三步都不可绕过。

    同步调用用 :meth:`apply`（内存 recorder）；真实 PG 落库用
    ``await apply_async(...)`` + :class:`AsyncConfigAuditRecorder`。
    """

    def __init__(
        self,
        *,
        gate: ConfigGate | None = None,
        recorder: ConfigAuditRecorder | AsyncConfigAuditRecorder | None = None,
    ) -> None:
        self._gate = gate or ConfigGate()
        self._recorder = recorder

    @property
    def gate(self) -> ConfigGate:
        return self._gate

    def _authorize_and_build(
        self,
        *,
        principal: UserPrincipal,
        change: ConfigChange,
        approver: UserPrincipal | None,
    ) -> tuple[ConfigGrant, ConfigAuditEntry]:
        """授权与双人判定；任何一步拒绝都不会构造审计条目。"""

        grant = self._gate.authorize_write(principal, change)
        approved_by = self._gate.assert_approved(
            actor=principal, change=change, approver=approver
        )
        entry = ConfigAuditEntry(
            change_id=uuid4(),
            tenant_id=principal.tenant_id,
            kind=change.kind,
            target=change.target,
            before=dict(change.before),
            after=dict(change.after),
            actor_id=principal.user_id,
            approver_id=approved_by.user_id if approved_by else None,
            emergency=change.emergency,
            ticket_id=change.ticket_id,
        )
        return grant, entry

    @staticmethod
    def _result(grant: ConfigGrant, entry: ConfigAuditEntry) -> ConfigChangeResult:
        return ConfigChangeResult(
            change_id=entry.change_id,
            requires_dual_review=grant.requires_dual_review,
            approver_id=entry.approver_id,
            audit=entry,
        )

    def apply(
        self,
        *,
        principal: UserPrincipal,
        change: ConfigChange,
        approver: UserPrincipal | None = None,
    ) -> ConfigChangeResult:
        grant, entry = self._authorize_and_build(
            principal=principal, change=change, approver=approver
        )
        if self._recorder is not None:
            record = getattr(self._recorder, "record", None)
            if record is None:
                raise TypeError(
                    "recorder 只实现了 record_async（PG 落库），"
                    "请改用 await apply_async(...)"
                )
            record(entry)
        return self._result(grant, entry)

    async def apply_async(
        self,
        *,
        principal: UserPrincipal,
        change: ConfigChange,
        approver: UserPrincipal | None = None,
    ) -> ConfigChangeResult:
        grant, entry = self._authorize_and_build(
            principal=principal, change=change, approver=approver
        )
        if self._recorder is not None:
            record_async = getattr(self._recorder, "record_async", None)
            if record_async is not None:
                await record_async(entry)
            else:
                self._recorder.record(entry)
        return self._result(grant, entry)


def assert_audit_entries(
    entries: Sequence[ConfigAuditEntry], *, expected: int
) -> None:
    """轻量断言助手：矩阵用例用它确认「变更必进审计」。"""

    if len(entries) != expected:
        raise AssertionError(f"期望 {expected} 条配置审计，实际 {len(entries)} 条")


__all__ = [
    "CONFIG_READ_ROLES",
    "CONFIG_WRITE_ROLES",
    "HIGH_RISK_KINDS",
    "LOW_RISK_KINDS",
    "MASK",
    "SECRET_FIELDS",
    "AsyncConfigAuditRecorder",
    "ConfigAccessDenied",
    "ConfigApproverRoleRequired",
    "ConfigAuditEntry",
    "ConfigAuditRecorder",
    "ConfigChange",
    "ConfigChangeResult",
    "ConfigChangeService",
    "ConfigDualReviewRequired",
    "ConfigGate",
    "ConfigGrant",
    "ConfigRoleDenied",
    "ConfigSelfApprovalRejected",
    "InMemoryConfigAuditRecorder",
    "assert_audit_entries",
]
