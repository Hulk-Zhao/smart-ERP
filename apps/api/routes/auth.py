"""会话路由：只读当前会话，不签发令牌。

令牌由部署期注入（``ERP_DEMO_SESSION_*``，仅 demo 档），本路由用请求自带的
``X-Session-Token`` 换回 ``{user_id, tenant_id, roles, default_scope}``；
不提供任何公开签发端点。``default_scope`` 只从令牌解析出的授权范围派生，
不读 query/body、不查库。
"""

from __future__ import annotations

import re

from fastapi import APIRouter, HTTPException, Response

from apps.api.deps import SessionDep
from apps.api.schemas import (
    DemoIdentitiesOut,
    DemoIdentityOut,
    SessionOut,
    SessionScopeOut,
)
from apps.platform.config import get_settings, parse_demo_identities
from apps.security.scope import ScopeGrant

router = APIRouter(prefix="/auth", tags=["会话"])

_PERIOD_PATTERN = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


def _default_scope(grant: ScopeGrant) -> SessionScopeOut:
    """服务端只做「唯一确定的默认值」，不做猜测。

    - 组织：恰好一个才返回；多个返回 ``None``，交给前端选择；
    - 期间：只认合法 ``YYYY-MM``，取其中最新（字典序最大）；无合法值返回 ``None``；
    - 系统：授权集合全量、排序（前端当筛选默认值不会越权）。
    """

    org_id = next(iter(grant.org_ids)) if len(grant.org_ids) == 1 else None
    valid_periods = [item for item in grant.periods if _PERIOD_PATTERN.match(item)]
    return SessionScopeOut(
        org_id=org_id,
        period=max(valid_periods) if valid_periods else None,
        system_ids=sorted(grant.system_ids),
    )


@router.get("/session", response_model=SessionOut)
def read_session(session: SessionDep) -> SessionOut:
    """当前会话身份；缺失/过期/无效令牌统一 401（由 ``session_dep`` 抛出）。"""

    return SessionOut(
        user_id=session.user_id,
        tenant_id=session.tenant_id,
        roles=sorted(session.roles),
        default_scope=_default_scope(session.grant),
    )


@router.get("/demo-identities", response_model=DemoIdentitiesOut)
def list_demo_identities(response: Response) -> DemoIdentitiesOut:
    """演示身份清单：仅 demo 档且显式开启时存在，其他环境一律 404。

    返回的 token 是部署期注入的静态会话令牌，等于把「登录」做成一键演示，
    所以必须过双闸：``ERP_APP_ENV=demo``（Settings 强制）与
    ``ERP_DEMO_SESSION_ENABLED=true``。不写日志、禁止缓存。
    """

    settings = get_settings()
    if not settings.demo_session_enabled:
        raise HTTPException(status_code=404, detail="演示身份切换未开启")

    response.headers["Cache-Control"] = "no-store"
    items = [
        DemoIdentityOut(
            user_id=identity.user_id,
            roles=sorted(identity.roles),
            label=f"{str(identity.user_id)[:8]} · {'/'.join(sorted(identity.roles))}",
            token=identity.token,
        )
        for identity in parse_demo_identities(settings.demo_session_identities)
    ]
    return DemoIdentitiesOut(items=items)


__all__ = ["router"]
