"""``GET /v1/auth/session`` 与 demo 静态会话注入。

三条硬约束：

1. 只有读取，没有签发：API 不出现任何公开签发端点；
2. 令牌只从请求头取（``X-Session-Token`` / ``Authorization: Bearer``），query 不认；
3. ``ERP_DEMO_SESSION_ENABLED`` 默认 false，只在 ``ERP_APP_ENV=demo`` 时允许打开。
"""

from __future__ import annotations

import logging

import pytest
from conftest import APPROVER, PREFIX, REVIEWER, TENANT, ApiEnv

from apps.api.container import ApiContainer
from apps.api.demo_session import BANNER, bind_demo_session
from apps.platform.api.errors import ProblemError
from apps.platform.config import ConfigError, Settings
from apps.security.identity import ScopeGrant, UserPrincipal
from apps.security.scope import Scope, ScopeGuard

TOKEN = "demo-token-0123456789abcdef"


def _demo_settings(**overrides) -> Settings:
    values: dict = {
        "app_env": "demo",
        "image_tag": "test",
        "demo_session_enabled": True,
        "demo_session_token": TOKEN,
        "demo_session_user_id": str(APPROVER),
        "demo_session_tenant_id": str(TENANT),
        "demo_session_roles": ("approver", "cfo"),
        "demo_session_org_ids": ("ORG-1",),
        "demo_session_periods": ("2026-10",),
        "demo_session_system_ids": ("u8", "statement"),
    }
    values.update(overrides)
    return Settings(**values)


def test_read_current_session(env: ApiEnv) -> None:
    response = env.client.get(
        f"{PREFIX}/auth/session", headers=env.headers("reviewer")
    )

    assert response.status_code == 200
    body = response.json()
    assert body == {
        "user_id": str(REVIEWER),
        "tenant_id": str(TENANT),
        "roles": ["finance", "reviewer"],
        "default_scope": {
            "org_id": "ORG-1",
            "period": "2026-10",
            "system_ids": ["statement", "u8"],
        },
    }


def test_missing_token_is_401(env: ApiEnv) -> None:
    response = env.client.get(f"{PREFIX}/auth/session")

    assert response.status_code == 401
    assert response.json()["code"] == "E-AUTHZ-001"


def test_unknown_token_is_401(env: ApiEnv) -> None:
    response = env.client.get(
        f"{PREFIX}/auth/session", headers={"X-Session-Token": "not-a-real-token"}
    )

    assert response.status_code == 401
    assert response.json()["code"] == "E-AUTHZ-001"


def test_bearer_header_is_accepted(env: ApiEnv) -> None:
    response = env.client.get(
        f"{PREFIX}/auth/session",
        headers={"Authorization": f"Bearer {env.tokens['approver']}"},
    )

    assert response.status_code == 200
    assert response.json()["user_id"] == str(APPROVER)


def test_query_token_is_not_accepted(env: ApiEnv) -> None:
    """令牌只从 header 取：query 里带 token 也必须 401，不能成为第二条身份通道。"""

    response = env.client.get(
        f"{PREFIX}/auth/session", params={"token": env.tokens["reviewer"]}
    )

    assert response.status_code == 401
    assert response.json()["code"] == "E-AUTHZ-001"


def test_demo_session_disabled_by_default() -> None:
    assert Settings(image_tag="test").demo_session_enabled is False


def test_demo_session_binds_token_and_logs_banner(caplog: pytest.LogCaptureFixture) -> None:
    container = ApiContainer()

    with caplog.at_level(logging.WARNING, logger="apps.api.session"):
        assert bind_demo_session(container, _demo_settings()) is True

    session = container.session_directory.resolve(TOKEN)
    assert session.user_id == APPROVER
    assert session.tenant_id == TENANT
    assert session.roles == frozenset({"approver", "cfo"})
    assert any(BANNER.split("%s")[0] in record.getMessage() for record in caplog.records)


def test_demo_session_switch_off_binds_nothing() -> None:
    container = ApiContainer()

    assert bind_demo_session(container, _demo_settings(demo_session_enabled=False)) is False
    with pytest.raises(ProblemError):
        container.session_directory.resolve(TOKEN)


@pytest.mark.parametrize("app_env", ["full", "prod", "local", "production"])
def test_demo_session_refuses_non_demo_env(app_env: str) -> None:
    with pytest.raises(ConfigError, match="ERP_APP_ENV"):
        _demo_settings(app_env=app_env)


def test_demo_session_requires_complete_identity() -> None:
    with pytest.raises(ConfigError, match="ERP_DEMO_SESSION_TOKEN"):
        _demo_settings(demo_session_token="")


def test_demo_session_rejects_short_token() -> None:
    with pytest.raises(ConfigError, match="至少 16 个字符"):
        _demo_settings(demo_session_token="short")


def test_demo_session_rejects_bad_uuid() -> None:
    with pytest.raises(ConfigError, match="UUID"):
        _demo_settings(demo_session_user_id="not-a-uuid")


# --------------------------------------------------------------- 默认 scope 派生


def _bind_scope(
    env: ApiEnv,
    token: str,
    *,
    orgs: set[str],
    periods: set[str],
    systems: set[str],
) -> None:
    """把一个带指定授权范围的令牌登记进当前测试容器。"""

    principal = UserPrincipal(
        tenant_id=TENANT,
        user_id=APPROVER,
        roles=frozenset({"approver"}),
        grant=ScopeGrant(
            org_ids=frozenset(orgs),
            periods=frozenset(periods),
            system_ids=frozenset(systems),
        ),
    )
    env.container.session_directory.bind(
        token, env.container.broker.issue_session(principal)
    )


def _scope_of(env: ApiEnv, token: str) -> dict:
    response = env.client.get(
        f"{PREFIX}/auth/session", headers={"X-Session-Token": token}
    )

    assert response.status_code == 200
    return response.json()["default_scope"]


def test_default_scope_single_org_and_latest_valid_period(env: ApiEnv) -> None:
    token = "scope-single-org-token"
    _bind_scope(
        env,
        token,
        orgs={"ORG-SZ"},
        periods={"2026-07", "2026-08", "2026-09"},
        systems={"u8", "wms", "sap"},
    )

    assert _scope_of(env, token) == {
        "org_id": "ORG-SZ",
        "period": "2026-09",
        "system_ids": ["sap", "u8", "wms"],
    }


def test_multiple_orgs_return_null_so_the_client_must_choose(env: ApiEnv) -> None:
    """多组织不猜：服务端返回 null，由前端进入选择态。"""

    token = "scope-multi-org-token"
    _bind_scope(
        env,
        token,
        orgs={"ORG-SZ", "ORG-SH"},
        periods={"2026-09"},
        systems={"u8"},
    )

    scope = _scope_of(env, token)

    assert scope["org_id"] is None
    assert scope["period"] == "2026-09"


def test_invalid_period_is_rejected_instead_of_compared_as_string(env: ApiEnv) -> None:
    """非 YYYY-MM 不参与「最新」比较；全不合法时返回 null。"""

    mixed = "scope-period-mixed-token"
    _bind_scope(
        env,
        mixed,
        orgs={"ORG-SZ"},
        periods={"2026-13", "2026-9", "2026-09"},
        systems={"u8"},
    )
    assert _scope_of(env, mixed)["period"] == "2026-09"

    invalid = "scope-period-invalid-token"
    _bind_scope(
        env,
        invalid,
        orgs={"ORG-SZ"},
        periods={"2026-13", "2026-9"},
        systems={"u8"},
    )
    assert _scope_of(env, invalid)["period"] is None


def test_request_scope_is_never_echoed(env: ApiEnv) -> None:
    """默认 scope 只来自令牌授权，query 里传别的 org/period 不回显、不报错。"""

    response = env.client.get(
        f"{PREFIX}/auth/session",
        headers=env.headers("reviewer"),
        params={"org_id": "ORG-2", "period": "2026-12", "system_ids": ["sap"]},
    )

    assert response.status_code == 200
    assert response.json()["default_scope"] == {
        "org_id": "ORG-1",
        "period": "2026-10",
        "system_ids": ["statement", "u8"],
    }


def test_default_scope_is_accepted_by_scope_guard(env: ApiEnv) -> None:
    """返回的默认 scope 必须是授权范围子集，而不是另一套口径。"""

    session = env.container.session_directory.resolve(env.tokens["reviewer"])
    scope = _scope_of(env, env.tokens["reviewer"])

    # 单一 org、合法 period 时两个字段必然非空；构造 Scope 再走一次闸口。
    ScopeGuard(session.grant).authorize(
        Scope(
            org_id=scope["org_id"],
            period=scope["period"],
            system_ids=tuple(scope["system_ids"]),
        )
    )
