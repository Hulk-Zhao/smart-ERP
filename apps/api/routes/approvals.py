"""审批路由：待办 / 通过 / 驳回。

审批通过只落审批记录（执行由发起人在 ``/tickets/{id}/actions/approve`` 触发），
驳回同时把工单推进到已驳回；两者都不产生外部写入。
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse

from apps.api import service
from apps.api.deps import (
    ContainerDep,
    IdempotencyKey,
    PageLimit,
    ScopeDep,
    SessionDep,
)
from apps.api.errors import translated
from apps.api.schemas import (
    ApprovalDecisionIn,
    ApprovalDecisionOut,
    ApprovalTaskOut,
    Page,
)
from apps.security.identity import SessionToken

router = APIRouter(prefix="/approvals", tags=["审批"])


async def _decide(
    *,
    request: Request,
    container: ContainerDep,
    idem_key: IdempotencyKey,
    body: ApprovalDecisionIn,
    decision: str,
    session: SessionToken,
    ticket_id: UUID,
) -> JSONResponse:
    async def action() -> tuple[int, dict]:
        result = await service.decide_approval(
            container,
            session,
            ticket_id=ticket_id,
            body=body,
            decision=decision,
        )
        return 200, result.model_dump(mode="json")

    with translated():
        status_code, payload = await service.run_idempotent(
            container,
            idem_key=idem_key,
            fingerprint=service.request_fingerprint(
                {"path": request.url.path, "body": body.model_dump(mode="json")}
            ),
            action=action,
        )
    return JSONResponse(status_code=status_code, content=payload)


@router.get(
    "/pending",
    response_model=Page[ApprovalTaskOut],
    summary="审批待办",
    description=(
        "只返回 scope 内、待审批、发起人不是当前用户，且所需角色与当前用户角色有交集的工单。"
    ),
)
async def pending(
    container: ContainerDep,
    session: SessionDep,
    scope: ScopeDep,
    scene_code: Annotated[str | None, Query(description="场景码筛选")] = None,
    cursor: Annotated[str | None, Query(description="上一页返回的 next_cursor")] = None,
    limit: PageLimit = None,
):
    with translated():
        items, next_cursor = await service.list_pending_approvals(
            container,
            session,
            scope_in=scope.payload,
            scene_code=scene_code,
            cursor=cursor,
            limit=limit,
        )
    return Page[ApprovalTaskOut](items=items, next_cursor=next_cursor)


@router.post(
    "/{ticket_id}/approve",
    response_model=ApprovalDecisionOut,
    summary="审批通过",
    description="记录审批意见；意见必填，同一人不得对同一工单重复审批。",
)
async def approve(
    ticket_id: UUID,
    body: ApprovalDecisionIn,
    request: Request,
    container: ContainerDep,
    session: SessionDep,
    idem_key: IdempotencyKey = None,
):
    return await _decide(
        request=request,
        container=container,
        idem_key=idem_key,
        body=body,
        decision="approved",
        session=session,
        ticket_id=ticket_id,
    )


@router.post(
    "/{ticket_id}/reject",
    response_model=ApprovalDecisionOut,
    summary="审批驳回",
    description="记录驳回意见并把工单推进到已驳回；意见必填。",
)
async def reject(
    ticket_id: UUID,
    body: ApprovalDecisionIn,
    request: Request,
    container: ContainerDep,
    session: SessionDep,
    idem_key: IdempotencyKey = None,
):
    return await _decide(
        request=request,
        container=container,
        idem_key=idem_key,
        body=body,
        decision="rejected",
        session=session,
        ticket_id=ticket_id,
    )
