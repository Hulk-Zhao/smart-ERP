"""C6 连接器与映射配置授权矩阵（读 / 写 / 双人 / 审计）。

矩阵口径与 ``docs/product/tech/permission-matrix.md`` §8 对齐：

| 操作 | 平台管理员 | 审计员 | 业务角色 | 服务账号 |
|---|---|---|---|---|
| 读配置 | 允许（全量） | 允许（机密字段脱敏） | 拒绝 | 拒绝 |
| 改低风险（label/quota） | 允许（单签） | 拒绝 | 拒绝 | 拒绝 |
| 改高风险（endpoint/credential/field_mapping） | 需双人 | 拒绝 | 拒绝 | 拒绝 |
| 变更审计 | 每次 apply 必留痕 | — | — | — |
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateTable

from apps.security.config_access import (
    HIGH_RISK_KINDS,
    MASK,
    SECRET_FIELDS,
    ConfigApproverRoleRequired,
    ConfigChange,
    ConfigChangeService,
    ConfigDualReviewRequired,
    ConfigGate,
    ConfigRoleDenied,
    ConfigSelfApprovalRejected,
    InMemoryConfigAuditRecorder,
)
from apps.security.identity import ScopeGrant, UserPrincipal
from apps.security.models import ConfigChangeRow

TENANT = UUID("11111111-1111-1111-1111-111111111111")
ORG = "ORG-1"
PERIOD = "2026-10"

ADMIN = UUID("eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee")
ADMIN2 = UUID("99999999-9999-9999-9999-999999999999")
AUDITOR = UUID("dddddddd-dddd-dddd-dddd-dddddddddddd")


def _principal(user_id: UUID, roles: frozenset[str]) -> UserPrincipal:
    return UserPrincipal(
        tenant_id=TENANT,
        user_id=user_id,
        roles=roles,
        grant=ScopeGrant(
            org_ids=frozenset({ORG}),
            periods=frozenset({PERIOD}),
            system_ids=frozenset({"u8"}),
        ),
    )


def _admin(user_id: UUID = ADMIN) -> UserPrincipal:
    return _principal(user_id, frozenset({"platform_admin"}))


def _change(kind: str = "label") -> ConfigChange:
    return ConfigChange(
        kind=kind,
        target="connector:u8",
        before={"label": "U8 生产"},
        after={"label": "U8 生产（新）"},
    )


# --------------------------------------------------------------------------- 读


@pytest.mark.parametrize(
    "roles",
    [
        frozenset({"platform_admin"}),
        frozenset({"auditor"}),
    ],
)
def test_c6_config_read_allowed_for_it_and_auditor(roles):
    grant = ConfigGate().authorize_read(_principal(uuid4(), roles))
    assert grant.requires_dual_review is False


@pytest.mark.parametrize(
    "roles",
    [
        frozenset({"operator"}),
        frozenset({"reviewer"}),
        frozenset({"approver"}),
        frozenset({"cfo"}),
        frozenset({"service_account"}),
    ],
)
def test_c6_config_read_denied_for_business_and_service_account(roles):
    with pytest.raises(ConfigRoleDenied):
        ConfigGate().authorize_read(_principal(uuid4(), roles))


def test_c6_auditor_sees_masked_secrets_only():
    gate = ConfigGate()
    config = {"endpoint": "https://u8.example", "password": "p@ss", "api_key": "k-1"}

    auditor_view = gate.mask_for(_principal(AUDITOR, frozenset({"auditor"})), config)
    admin_view = gate.mask_for(_admin(), config)

    assert auditor_view["endpoint"] == "https://u8.example"
    assert auditor_view["password"] == MASK
    assert auditor_view["api_key"] == MASK
    assert admin_view["password"] == "p@ss"
    assert set(gate.authorize_read(_principal(AUDITOR, frozenset({"auditor"}))).masked_fields) == set(
        SECRET_FIELDS
    )


# --------------------------------------------------------------------------- 写


def test_c6_low_risk_change_single_platform_admin():
    recorder = InMemoryConfigAuditRecorder()
    service = ConfigChangeService(recorder=recorder)

    result = service.apply(principal=_admin(), change=_change("label"))

    assert result.requires_dual_review is False
    assert result.approver_id is None
    assert len(recorder.entries) == 1


@pytest.mark.parametrize(
    "roles",
    [
        frozenset({"operator"}),
        frozenset({"reviewer"}),
        frozenset({"approver"}),
        frozenset({"cfo"}),
        frozenset({"auditor"}),
        frozenset({"service_account"}),
    ],
)
def test_c6_config_write_denied_for_non_it_roles(roles):
    service = ConfigChangeService(recorder=InMemoryConfigAuditRecorder())
    with pytest.raises(ConfigRoleDenied):
        service.apply(principal=_principal(uuid4(), roles), change=_change("label"))


@pytest.mark.parametrize("kind", sorted(HIGH_RISK_KINDS))
def test_c6_high_risk_change_requires_second_platform_admin(kind):
    service = ConfigChangeService(recorder=InMemoryConfigAuditRecorder())

    with pytest.raises(ConfigDualReviewRequired):
        service.apply(principal=_admin(), change=_change(kind))

    result = service.apply(
        principal=_admin(), change=_change(kind), approver=_admin(ADMIN2)
    )
    assert result.requires_dual_review is True
    assert result.approver_id == ADMIN2


def test_c6_high_risk_change_rejects_self_approval():
    service = ConfigChangeService(recorder=InMemoryConfigAuditRecorder())
    with pytest.raises(ConfigSelfApprovalRejected):
        service.apply(
            principal=_admin(), change=_change("credential"), approver=_admin()
        )


def test_c6_high_risk_change_rejects_non_admin_approver():
    service = ConfigChangeService(recorder=InMemoryConfigAuditRecorder())
    with pytest.raises(ConfigApproverRoleRequired):
        service.apply(
            principal=_admin(),
            change=_change("field_mapping"),
            approver=_principal(uuid4(), frozenset({"reviewer"})),
        )


def test_c6_unknown_change_kind_is_rejected():
    service = ConfigChangeService(recorder=InMemoryConfigAuditRecorder())
    with pytest.raises(Exception) as excinfo:
        service.apply(principal=_admin(), change=_change("unregistered_kind"))
    assert "未登记" in str(excinfo.value)


# --------------------------------------------------------------------------- 审计


def test_c6_every_change_is_audited_with_actor_and_approver():
    recorder = InMemoryConfigAuditRecorder()
    service = ConfigChangeService(recorder=recorder)

    service.apply(principal=_admin(), change=_change("quota"))
    service.apply(
        principal=_admin(), change=_change("endpoint"), approver=_admin(ADMIN2)
    )

    assert len(recorder.entries) == 2
    low, high = recorder.entries
    assert low.kind == "quota" and low.approver_id is None
    assert low.actor_id == ADMIN and low.tenant_id == TENANT
    assert low.before == {"label": "U8 生产"}
    assert high.kind == "endpoint" and high.approver_id == ADMIN2
    assert high.actor_id == ADMIN


def test_c6_config_change_db_check_prevents_self_approval():
    """DB 兜底：config_change 的复核人不得等于提交人。"""

    ddl = str(
        CreateTable(ConfigChangeRow.__table__).compile(dialect=postgresql.dialect())
    )
    assert "ck_config_change_dual" in ddl
    assert "approver_id IS NULL OR approver_id <> actor_id" in ddl
