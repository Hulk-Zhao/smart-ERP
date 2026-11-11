"""demo 档静态会话注入（只读当前会话，不签发令牌）。

demo 环境的令牌由部署期 env 注入，进程启动时把它绑定到会话目录；
``GET /v1/auth/session`` 只是用请求自带的令牌换回 ``{user_id, tenant_id, roles}``，
**不存在任何公开签发端点**。开关是双闸：``ERP_DEMO_SESSION_ENABLED`` 默认 false，
且 ``ERP_APP_ENV`` 必须为 ``demo``，否则 ``Settings`` 直接拒绝启动（full/生产不得继承）。
"""

from __future__ import annotations

import logging
from uuid import UUID

from apps.api.container import ApiContainer
from apps.platform.config import Settings
from apps.security.identity import UserPrincipal
from apps.security.scope import ScopeGrant

LOGGER = logging.getLogger("apps.api.session")

BANNER = (
    "!!!! DEMO SESSION ENABLED (ERP_APP_ENV=demo): 静态令牌已绑定 "
    "user=%s tenant=%s roles=%s —— 仅限演示环境，full/生产档位禁止打开 !!!!"
)


def bind_demo_session(container: ApiContainer, settings: Settings) -> bool:
    """把部署期注入的静态令牌登记进会话目录；返回是否真的绑定。"""

    if not settings.demo_session_enabled:
        return False

    principal = UserPrincipal(
        tenant_id=UUID(settings.demo_session_tenant_id),
        user_id=UUID(settings.demo_session_user_id),
        roles=frozenset(settings.demo_session_roles),
        grant=ScopeGrant(
            org_ids=frozenset(settings.demo_session_org_ids),
            periods=frozenset(settings.demo_session_periods),
            system_ids=frozenset(settings.demo_session_system_ids),
        ),
    )
    container.session_directory.bind(
        settings.demo_session_token, container.broker.issue_session(principal)
    )
    LOGGER.warning(
        BANNER,
        principal.user_id,
        principal.tenant_id,
        ",".join(sorted(principal.roles)),
    )
    return True


__all__ = ["BANNER", "bind_demo_session"]
