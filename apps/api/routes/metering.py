"""计量路由：按批次的计量表与逐行明细（只读，W0）。

只输出程序性事实（是否自动放行、是否需人工、有无证据链），不判断「结论是否
正确」——计费时本来就不可知。缺口字段返回 null，见响应 ``data_gaps``。
"""

from __future__ import annotations

from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Query, Response

from apps.api import service
from apps.api.deps import ContainerDep, ScopeDep, SessionDep
from apps.api.errors import translated
from apps.api.schemas import MeteringStatementOut

router = APIRouter(prefix="/metering", tags=["计量"])


@router.get(
    "/batches/{batch_id}/statement",
    response_model=MeteringStatementOut,
    summary="按批次出计量表",
    description=(
        "按对账批次输出结果计费的计量表（W0 只读：不写库、不碰资金、不改状态）。"
        "「有效对账行数」只按程序性事实计算：自动放行 − 已确认错放行 − 客户原因；"
        "后两项当前无数据源，按 0 扣减并在 calculation/data_gaps 中标注为上界。"
        "单行单价只从 ERP_METERING_UNIT_PRICE 读取，未配置直接 503。"
        "format=csv 导出逐行明细（UTF-8 BOM，X-Content-SHA256 为内容摘要）。"
    ),
    responses={
        200: {
            "content": {"text/csv": {}},
            "description": "format=csv 时为逐行明细 CSV",
        }
    },
)
async def get_statement(
    batch_id: UUID,
    container: ContainerDep,
    session: SessionDep,
    scope: ScopeDep,
    format: Annotated[
        Literal["json", "csv"],
        Query(description="json=计量表（含逐行明细）；csv=逐行明细导出"),
    ] = "json",
):
    with translated():
        if format == "csv":
            filename, content, digest = await service.export_metering_csv(
                container, session, scope_in=scope.payload, batch_id=batch_id
            )
            return Response(
                content=content,
                media_type="text/csv; charset=utf-8",
                headers={
                    "Content-Disposition": f'attachment; filename="{filename}"',
                    "X-Content-SHA256": digest,
                },
            )
        return await service.get_metering_statement(
            container, session, scope_in=scope.payload, batch_id=batch_id
        )
