"""路由依赖：容器、会话、scope 查询参数、幂等键。

``trace_id`` 不在这里处理：平台中间件负责注入并写回 ``X-Trace-Id``，
错误体里的 ``trace_id`` 也由平台 ``ProblemError`` 处理器补齐。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Header, Query, Request

from apps.api.container import SESSION_HEADER, ApiContainer, get_container
from apps.api.schemas import ScopeIn
from apps.security.identity import SessionToken


def container_dep() -> ApiContainer:
    return get_container()


def session_dep(
    request: Request,
    container: Annotated[ApiContainer, Depends(container_dep)],
) -> SessionToken:
    token = request.headers.get(SESSION_HEADER)
    if not token:
        authorization = request.headers.get("authorization", "")
        if authorization.lower().startswith("bearer "):
            token = authorization[7:].strip()
    return container.session_directory.resolve(token)


class ScopeQuery:
    """G-05：三个 scope 字段都是必填查询参数，缺一个就 422。"""

    def __init__(
        self,
        org_id: Annotated[str, Query(min_length=1, max_length=64, description="组织")],
        period: Annotated[str, Query(min_length=1, max_length=32, description="期间，如 2026-10")],
        system_ids: Annotated[
            list[str],
            Query(min_length=1, max_length=16, description="系统列表，可重复传"),
        ],
    ) -> None:
        self.payload = ScopeIn(org_id=org_id, period=period, system_ids=system_ids)


ContainerDep = Annotated[ApiContainer, Depends(container_dep)]
SessionDep = Annotated[SessionToken, Depends(session_dep)]
ScopeDep = Annotated[ScopeQuery, Depends(ScopeQuery)]
IdempotencyKey = Annotated[
    str | None,
    Header(
        alias="Idempotency-Key",
        description="写接口必填；同键同参回放首次结果，同键不同参 409（G-02）",
    ),
]
PageLimit = Annotated[int | None, Query(ge=1, le=100, description="每页条数，默认 20")]

__all__ = [
    "ContainerDep",
    "IdempotencyKey",
    "PageLimit",
    "ScopeDep",
    "ScopeQuery",
    "SessionDep",
    "container_dep",
    "session_dep",
]
