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
