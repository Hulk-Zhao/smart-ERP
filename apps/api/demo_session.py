"""demo 档静态会话注入（只读当前会话，不签发令牌）。

demo 环境的令牌由部署期 env 注入，进程启动时把每个身份绑定到会话目录；
``GET /v1/auth/session`` 只是用请求自带的令牌换回 ``{user_id, tenant_id, roles}``，
**不存在任何公开签发端点**。开关是双闸：``ERP_DEMO_SESSION_ENABLED`` 默认 false，
且 ``ERP_APP_ENV`` 必须为 ``demo``，否则 ``Settings`` 直接拒绝启动（full/生产不得继承）。

多身份只用于演示「发起人 ≠ 审批人」的双人复核：每条身份一枚令牌、一个 user_id，
演示时开两个窗口各带一枚，审计里能看到两个不同的操作人；这里不提供任何代登录能力。
"""

from __future__ import annotations

import logging

from apps.api.container import ApiContainer
from apps.platform.config import Settings, parse_demo_identities
from apps.security.identity import UserPrincipal
from apps.security.scope import ScopeGrant

LOGGER = logging.getLogger("apps.api.session")

BANNER = (
    "!!!! DEMO SESSION ENABLED (ERP_APP_ENV=demo): 已绑定 %d 个静态身份 "
    "[%s] —— 仅限演示环境，full/生产档位禁止打开 !!!!"
)


def bind_demo_session(container: ApiContainer, settings: Settings) -> bool:
    """把部署期注入的静态身份逐个登记进会话目录；返回是否真的绑定。"""

    if not settings.demo_session_enabled:
        return False

    identities = parse_demo_identities(settings.demo_session_identities)
    labels: list[str] = []
    for identity in identities:
        principal = UserPrincipal(
            tenant_id=identity.tenant_id,
            user_id=identity.user_id,
            roles=frozenset(identity.roles),
            grant=ScopeGrant(
                org_ids=frozenset(identity.org_ids),
                periods=frozenset(identity.periods),
                system_ids=frozenset(identity.system_ids),
            ),
        )
        container.session_directory.bind(
            identity.token, container.broker.issue_session(principal)
        )
        labels.append(
            f"user={identity.user_id} roles={','.join(sorted(principal.roles))}"
        )
    LOGGER.warning(BANNER, len(identities), "; ".join(labels))
    return True


__all__ = ["BANNER", "bind_demo_session"]
