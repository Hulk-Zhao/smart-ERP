"""会话路由：只读当前会话，不签发令牌。

令牌由部署期注入（``ERP_DEMO_SESSION_*``，仅 demo 档），本路由用请求自带的
``X-Session-Token`` 换回 ``{user_id, tenant_id, roles}``；不提供任何公开签发端点。
"""

from __future__ import annotations

from fastapi import APIRouter

from apps.api.deps import SessionDep
from apps.api.schemas import SessionOut

router = APIRouter(prefix="/auth", tags=["会话"])


@router.get("/session", response_model=SessionOut)
def read_session(session: SessionDep) -> SessionOut:
    """当前会话身份；缺失/过期/无效令牌统一 401（由 ``session_dep`` 抛出）。"""

    return SessionOut(
        user_id=session.user_id,
        tenant_id=session.tenant_id,
        roles=sorted(session.roles),
    )


__all__ = ["router"]
