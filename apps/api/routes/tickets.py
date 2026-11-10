"""工单路由：列表 / 详情 / 四个动作（审批、驳回、取消、触发补偿）。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
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
from apps.api.schemas import ActionIn, ActionOut, Page, TicketDetailOut, TicketOut

router = APIRouter(prefix="/tickets", tags=["工单"])


async def _write(
    *,
    request: Request,
    container: ContainerDep,
    idem_key: IdempotencyKey,
    body: ActionIn,
    run: Callable[[], Awaitable[ActionOut]],
) -> JSONResponse:
    """写接口统一外壳：幂等 + problem+json；业务逻辑由传入的 ``run`` 完成。"""

    async def action() -> tuple[int, dict]:
        result = await run()
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
    "",
    response_model=Page[TicketOut],
    summary="工单列表",
    description="按 scope 过滤后键集分页；状态、场景、业务键、工单号、发起人均可筛选。",
)
async def list_tickets(
    container: ContainerDep,
    session: SessionDep,
    scope: ScopeDep,
    state: Annotated[str | None, Query(description="工单状态，如 待审批")] = None,
    scene_code: Annotated[str | None, Query(description="场景码，如 ag1_reconcile")] = None,
    business_key: Annotated[str | None, Query(description="业务键")] = None,
    ticket_no: Annotated[str | None, Query(description="工单号")] = None,
    initiator_id: Annotated[UUID | None, Query(description="发起人")] = None,
    cursor: Annotated[str | None, Query(description="上一页返回的 next_cursor")] = None,
    limit: PageLimit = None,
):
    with translated():
        items, next_cursor = service.list_tickets(
            container,
            session,
            scope_in=scope.payload,
            state=state,
            scene_code=scene_code,
            business_key=business_key,
            ticket_no=ticket_no,
            initiator_id=initiator_id,
            cursor=cursor,
            limit=limit,
        )
    return Page[TicketOut](items=items, next_cursor=next_cursor)


@router.get(
    "/{ticket_id}",
    response_model=TicketDetailOut,
    summary="工单详情",
    description="含步骤、审批记录、当前所需/缺失审批角色；只返回 scope 内工单。",
)
async def get_ticket(
    ticket_id: UUID,
    container: ContainerDep,
    session: SessionDep,
    scope: ScopeDep,
):
    with translated():
        return service.get_ticket(
            container, session, scope_in=scope.payload, ticket_id=ticket_id
        )


@router.post(
    "/{ticket_id}/actions/approve",
    response_model=ActionOut,
    summary="审批通过并执行写计划",
    description=(
        "依次过 AccessGate 四道闸（含审批闸）→ WriteGateway.execute → 追加审计影像 → "
        "推进到执行中。审批闸不满足时返回 403/409 且连接器零写入。"
    ),
)
async def approve_ticket(
    ticket_id: UUID,
    body: ActionIn,
    request: Request,
    container: ContainerDep,
    session: SessionDep,
    idem_key: IdempotencyKey = None,
):
    return await _write(
        request=request,
        container=container,
        idem_key=idem_key,
        body=body,
        run=lambda: service.approve_ticket(
            container, session, ticket_id=ticket_id, body=body
        ),
    )


@router.post(
    "/{ticket_id}/actions/reject",
    response_model=ActionOut,
    summary="驳回工单",
    description="落审批记录（写入人不得是发起人）并推进到已驳回；无外部写入。",
)
async def reject_ticket(
    ticket_id: UUID,
    body: ActionIn,
    request: Request,
    container: ContainerDep,
    session: SessionDep,
    idem_key: IdempotencyKey = None,
):
    async def run() -> ActionOut:
        return service.reject_ticket(
            container, session, ticket_id=ticket_id, body=body
        )

    return await _write(
        request=request, container=container, idem_key=idem_key, body=body, run=run
    )


@router.post(
    "/{ticket_id}/actions/cancel",
    response_model=ActionOut,
    summary="取消工单",
    description="只允许计划中 / 待人工复核 / 待审批三个状态，必须写明原因；无外部写入。",
)
async def cancel_ticket(
    ticket_id: UUID,
    body: ActionIn,
    request: Request,
    container: ContainerDep,
    session: SessionDep,
    idem_key: IdempotencyKey = None,
):
    async def run() -> ActionOut:
        return service.cancel_ticket(
            container, session, ticket_id=ticket_id, body=body
        )

    return await _write(
        request=request, container=container, idem_key=idem_key, body=body, run=run
    )


@router.post(
    "/{ticket_id}/actions/compensate",
    response_model=ActionOut,
    summary="触发补偿",
    description=(
        "按审计影像推导反向操作并分组经 WriteGateway 回写；"
        "W3 或反向等级更高时整单拒绝，未人工确认的 L2 回滚 409。"
    ),
)
async def compensate_ticket(
    ticket_id: UUID,
    body: ActionIn,
    request: Request,
    container: ContainerDep,
    session: SessionDep,
    idem_key: IdempotencyKey = None,
):
    return await _write(
        request=request,
        container=container,
        idem_key=idem_key,
        body=body,
        run=lambda: service.compensate_ticket(
            container, session, ticket_id=ticket_id, body=body
        ),
    )
