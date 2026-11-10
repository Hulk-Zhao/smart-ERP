"""审计路由：按工单读影像、校验链、导出明细。"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query, Response

from apps.api import service
from apps.api.deps import ContainerDep, PageLimit, ScopeDep, SessionDep
from apps.api.errors import translated
from apps.api.schemas import AuditChainOut, AuditImageOut, Page

router = APIRouter(prefix="/audit", tags=["审计"])


@router.get(
    "/tickets/{ticket_id}/images",
    response_model=Page[AuditImageOut],
    summary="按工单读取审计影像",
    description="返回操作前后竖表、哈希链指针；可按系统、步骤过滤，游标分页。",
)
async def list_images(
    ticket_id: UUID,
    container: ContainerDep,
    session: SessionDep,
    scope: ScopeDep,
    system: Annotated[str | None, Query(description="系统过滤，如 u8")] = None,
    step_id: Annotated[UUID | None, Query(description="步骤过滤")] = None,
    cursor: Annotated[str | None, Query(description="上一页返回的 next_cursor")] = None,
    limit: PageLimit = None,
):
    with translated():
        items, next_cursor = await service.list_audit_images(
            container,
            session,
            scope_in=scope.payload,
            ticket_id=ticket_id,
            system=system,
            step_id=step_id,
            cursor=cursor,
            limit=limit,
        )
    return Page[AuditImageOut](items=items, next_cursor=next_cursor)


@router.get(
    "/tickets/{ticket_id}/chain",
    response_model=AuditChainOut,
    summary="按工单校验审计链",
    description="校验租户链完整性（append-only + 前向哈希），并返回该工单影像数与链尾哈希。",
)
async def chain(
    ticket_id: UUID,
    container: ContainerDep,
    session: SessionDep,
    scope: ScopeDep,
):
    with translated():
        return await service.audit_chain(
            container, session, scope_in=scope.payload, ticket_id=ticket_id
        )


@router.get(
    "/export",
    summary="导出审计明细",
    description=(
        "按工单导出 CSV（UTF-8 BOM），响应头 X-Content-SHA256 给出内容摘要，便于对账复核。"
    ),
    responses={
        200: {
            "content": {"text/csv": {}},
            "description": "CSV 明细，含哈希与字段差异",
        }
    },
)
async def export_audit(
    ticket_id: Annotated[UUID, Query(description="工单")],
    container: ContainerDep,
    session: SessionDep,
    scope: ScopeDep,
    system: Annotated[str | None, Query(description="系统过滤")] = None,
):
    with translated():
        filename, content, digest = await service.export_audit_csv(
            container,
            session,
            scope_in=scope.payload,
            ticket_id=ticket_id,
            system=system,
        )
    return Response(
        content=content,
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Content-SHA256": digest,
        },
    )
