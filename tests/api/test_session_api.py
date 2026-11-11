"""``GET /v1/auth/session`` 与 demo 静态会话注入。

三条硬约束：

1. 只有读取，没有签发：API 不出现任何公开签发端点；
2. 令牌只从请求头取（``X-Session-Token`` / ``Authorization: Bearer``），query 不认；
3. ``ERP_DEMO_SESSION_ENABLED`` 默认 false，只在 ``ERP_APP_ENV=demo`` 时允许打开。

demo 档允许配置多个静态身份（各自令牌 / user_id），用于演示「发起人 ≠ 审批人」
的双人复核；这里只演示身份可共存，不放宽审批人判据，也不提供代登录。
"""

from __future__ import annotations

import json
import logging
from uuid import UUID

import pytest
from conftest import APPROVER, AUDITOR, INITIATOR, PREFIX, REVIEWER, TENANT, ApiEnv

from apps.api.container import ApiContainer
from apps.api.demo_session import BANNER, bind_demo_session
from apps.platform.api.errors import ProblemError
from apps.platform.config import ConfigError, Settings
from apps.security.identity import ScopeGrant, UserPrincipal
from apps.security.roles import CrossCategoryReviewRejected
from apps.security.scope import Scope, ScopeGuard

TOKEN_INITIATOR = "demo-initiator-token-0001"
TOKEN_APPROVER = "demo-approver-token-0001"
TOKEN_APPROVER_SECOND = "demo-approver-2-token-001"
TOKEN_CFO = "demo-cfo-token-000000001"
FINANCE_APPROVER = UUID("eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee")


def _identity(token: str, user_id, **overrides) -> dict:
    identity = {
        "token": token,
        "user_id": str(user_id),
        "tenant_id": str(TENANT),
        "roles": ["operator"],
        "org_ids": ["ORG-1"],
        "periods": ["2026-10"],
        "system_ids": ["u8", "statement"],
    }
    identity.update(overrides)
    return identity


def _principal(session) -> UserPrincipal:
    return UserPrincipal(
        tenant_id=session.tenant_id,
        user_id=session.user_id,
        roles=session.roles,
        grant=session.grant,
    )


def _identities_json(*identities: dict) -> str:
    return json.dumps(list(identities), ensure_ascii=False)


def _demo_settings(identities: str | None = None, **overrides) -> Settings:
    values: dict = {
        "app_env": "demo",
        "image_tag": "test",
        "demo_session_enabled": True,
        "demo_session_identities": (
            _identities_json(
                _identity(TOKEN_INITIATOR, INITIATOR),
                _identity(TOKEN_APPROVER, REVIEWER, roles=["reviewer", "finance"]),
                _identity(
                    TOKEN_CFO,
                    AUDITOR,
                    roles=["reviewer", "approver", "cfo"],
                ),
                _identity(
                    TOKEN_APPROVER_SECOND,
                    FINANCE_APPROVER,
                    roles=["reviewer", "approver", "finance"],
                ),
            )
            if identities is None
            else identities
        ),
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


def test_demo_token_is_header_only(env: ApiEnv) -> None:
    """demo 多身份同样只认 header：同一枚令牌走 query 仍 401。"""

    assert bind_demo_session(env.container, _demo_settings()) is True

    accepted = env.client.get(
        f"{PREFIX}/auth/session", headers={"X-Session-Token": TOKEN_INITIATOR}
    )
    rejected = env.client.get(
        f"{PREFIX}/auth/session", params={"token": TOKEN_INITIATOR}
    )

    assert accepted.status_code == 200
    assert accepted.json()["user_id"] == str(INITIATOR)
    assert rejected.status_code == 401
    assert rejected.json()["code"] == "E-AUTHZ-001"


def test_demo_session_disabled_by_default() -> None:
    assert Settings(image_tag="test").demo_session_enabled is False


def test_demo_session_binds_multiple_identities_for_double_review(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """双人复核前提：发起人、reviewer、被拒的 CFO、财务审批人各自独立可解析。"""

    container = ApiContainer()

    with caplog.at_level(logging.WARNING, logger="apps.api.session"):
        assert bind_demo_session(container, _demo_settings()) is True

    initiator = container.session_directory.resolve(TOKEN_INITIATOR)
    reviewer = container.session_directory.resolve(TOKEN_APPROVER)
    cfo = container.session_directory.resolve(TOKEN_CFO)
    finance_approver = container.session_directory.resolve(TOKEN_APPROVER_SECOND)
    # 每个身份各自声明 roles / orgs / periods / systems，互不覆盖。
    assert initiator.user_id == INITIATOR
    assert initiator.roles == frozenset({"operator"})
    assert reviewer.user_id == REVIEWER
    assert reviewer.roles == frozenset({"reviewer", "finance"})
    assert cfo.user_id == AUDITOR
    # CFO 故意不含 finance：它是类别闸的反例，不是第二个财务审批人。
    assert cfo.roles == frozenset({"reviewer", "approver", "cfo"})
    assert finance_approver.user_id == FINANCE_APPROVER
    assert finance_approver.roles == frozenset({"reviewer", "approver", "finance"})
    assert reviewer.tenant_id == TENANT
    assert cfo.grant.org_ids == frozenset({"ORG-1"})
    assert cfo.grant.periods == frozenset({"2026-10"})
    assert cfo.grant.system_ids == frozenset({"u8", "statement"})

    banners = [
        record.getMessage()
        for record in caplog.records
        if BANNER.split("%d")[0] in record.getMessage()
    ]
    assert len(banners) == 1
    assert "4 个静态身份" in banners[0]
    for user_id in (INITIATOR, REVIEWER, AUDITOR, FINANCE_APPROVER):
        assert str(user_id) in banners[0]


def test_demo_identity_roles_flow_into_category_gate() -> None:
    """多身份只解决「谁在线」，不放宽 C5 类别闸。

    ``ag2_processing_fee`` 的业务类别是 finance：``dddddddd``（有 approver 但
    无 finance）以 approver 身份签票仍被 CrossCategoryReviewRejected 拦下；
    ``eeeeeeee``（持有 finance）才落得了票。这条用例证明装配链路没有偷偷加角色。
    """

    container = ApiContainer()
    bind_demo_session(container, _demo_settings())

    cfo = container.session_directory.resolve(TOKEN_CFO)
    with pytest.raises(CrossCategoryReviewRejected):
        container.approvals.build(
            ticket_id="demo-ticket",
            role="approver",
            approver=_principal(cfo),
            initiator_id=INITIATOR,
            decision="approved",
            comment="无 finance 类别",
            scene_code="ag2_processing_fee",
        )

    approver = container.session_directory.resolve(TOKEN_APPROVER_SECOND)
    record = container.approvals.build(
        ticket_id="demo-ticket",
        role="approver",
        approver=_principal(approver),
        initiator_id=INITIATOR,
        decision="approved",
        comment="财务审批",
        scene_code="ag2_processing_fee",
    )

    assert record.approver_id == FINANCE_APPROVER


def test_demo_session_switch_off_binds_nothing() -> None:
    container = ApiContainer()

    toggled_off = _demo_settings(demo_session_enabled=False)
    assert bind_demo_session(container, toggled_off) is False
    with pytest.raises(ProblemError):
        container.session_directory.resolve(TOKEN_INITIATOR)


def test_demo_session_ignores_identities_when_switched_off() -> None:
    """默认关闭：开关为 false 时坏 JSON 也不参与启动校验。"""

    settings = _demo_settings("{not-json", demo_session_enabled=False)

    assert settings.demo_session_identities == "{not-json"


@pytest.mark.parametrize("app_env", ["full", "prod", "local", "production"])
def test_demo_session_refuses_non_demo_env(app_env: str) -> None:
    with pytest.raises(ConfigError, match="ERP_APP_ENV"):
        _demo_settings(app_env=app_env)


@pytest.mark.parametrize(
    ("identities", "match"),
    [
        ("", "不能为空"),
        ("   ", "不能为空"),
        ("{", "合法 JSON"),
        ('{"token": "x"}', "JSON 数组"),
        ("[]", "空数组"),
        ("[1]", "必须是对象"),
        (json.dumps([{"token": TOKEN_INITIATOR}]), "缺少字段"),
        (
            json.dumps([_identity(TOKEN_INITIATOR, INITIATOR, extra="x")]),
            "未知字段",
        ),
        (json.dumps([_identity("short", INITIATOR)]), "至少 16"),
        (json.dumps([_identity(TOKEN_INITIATOR, "not-a-uuid")]), "UUID"),
        (
            json.dumps(
                [
                    _identity(TOKEN_INITIATOR, INITIATOR),
                    _identity(TOKEN_INITIATOR, AUDITOR),
                ]
            ),
            "令牌必须唯一",
        ),
        (
            json.dumps(
                [
                    _identity(TOKEN_INITIATOR, INITIATOR),
                    _identity(TOKEN_APPROVER, INITIATOR),
                ]
            ),
            "user_id 与其他身份重复",
        ),
        (
            json.dumps([_identity(TOKEN_INITIATOR, INITIATOR, roles=[])]),
            "非空字符串数组",
        ),
        (
            json.dumps([_identity(TOKEN_INITIATOR, INITIATOR, roles=["operator", 1])]),
            "只能包含非空字符串",
        ),
    ],
)
def test_demo_session_identity_failures_are_fatal(identities: str, match: str) -> None:
    """身份不全 / 映射重复 / 空映射一律启动失败，不跳过坏条目继续起。"""

    with pytest.raises(ConfigError, match=match):
        _demo_settings(identities)


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
