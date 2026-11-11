"""对账路由：发起 / 状态 / 差异列表 / 差异明细。"""

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
    BillingBucket,
    Page,
    ReconBatchOut,
    ReconBatchSummaryOut,
    ReconDiffDetailOut,
    ReconDiffOut,
    ReconRunIn,
)

router = APIRouter(prefix="/recon", tags=["对账"])


@router.post(
    "/runs",
    response_model=ReconBatchOut,
    status_code=201,
    summary="发起对账",
    description=(
        "按 scope 收窄后调用对账引擎；数据行须由 dataset 显式提供或事先登记，"
        "缺数据直接 422，不猜取数来源。发起本身无外部写入（W0）。\n\n"
        "**按原批次重跑**：批次与行快照 append-only，改判不能原地修改，只能显式重跑。"
        "重跑必须同时给 `rerun_of`（原批次 id）与 `rerun_reason`（非空原因），"
        "服务端生成**新批次 id**，新批次的 `rerun_of` 指向原批次；原批次数据保持不变。"
        "同租户/组织/期间/输入指纹的首次执行只允许一次，重复提交 409 `E-IDEM-CONFLICT`。"
    ),
)
async def create_run(
    body: ReconRunIn,
    request: Request,
    container: ContainerDep,
    session: SessionDep,
    idem_key: IdempotencyKey = None,
):
    async def action() -> tuple[int, dict]:
        result = await service.run_recon(container, session, body=body)
        return 201, result.model_dump(mode="json")

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
    "/batches",
    response_model=Page[ReconBatchSummaryOut],
    summary="批次列表",
    description=(
        "按 `(created_at, id)` 键集分页（倒序），租户/组织/期间/系统全部下推到查询条件。"
        "行数、差异数、差异金额取自与 `GET /recon/runs/{batch_id}` 同一份持久化投影，"
        "不另算一遍；旧批次没有投影时相应字段为 `null`，不伪造 0。\n\n"
        "**空态语义**：`items: []` 只表示该 scope 下确实没有批次；scope 越权返回 "
        "403 `E-AUTHZ-001`，绝不用空数组冒充「没有权限」。"
    ),
)
async def list_batches(
    container: ContainerDep,
    session: SessionDep,
    scope: ScopeDep,
    cursor: Annotated[str | None, Query(description="上一页返回的 next_cursor")] = None,
    limit: PageLimit = None,
):
    with translated():
        items, next_cursor = await service.list_recon_batches(
            container,
            session,
            scope_in=scope.payload,
            cursor=cursor,
            limit=limit,
        )
    return Page[ReconBatchSummaryOut](items=items, next_cursor=next_cursor)


@router.get(
    "/runs/{batch_id}",
    response_model=ReconBatchOut,
    summary="对账状态",
    description="返回批次状态、结论、指标与差异条数；批次不在 scope 内 403。",
)
async def get_run(
    batch_id: UUID,
    container: ContainerDep,
    session: SessionDep,
    scope: ScopeDep,
):
    with translated():
        return await service.get_recon_batch(
            container, session, scope_in=scope.payload, batch_id=batch_id
        )


@router.get(
    "/runs/{batch_id}/diffs",
    response_model=Page[ReconDiffOut],
    summary="差异列表",
    description=(
        "按差异类型、是否需人工、供应商、计费桶过滤，游标分页。"
        "`billing_bucket` 与计量表四类计数同源，避免「计数说 2、点进去 3 行」。"
    ),
)
async def list_diffs(
    batch_id: UUID,
    container: ContainerDep,
    session: SessionDep,
    scope: ScopeDep,
    diff_type: Annotated[
        service.DiffTypeCode | None,
        Query(description="差异类型（枚举由引擎 DiffType 派生，含 rounding_tolerance）"),
    ] = None,
    needs_manual: Annotated[bool | None, Query(description="只看需要人工的差异")] = None,
    supplier_code: Annotated[str | None, Query(description="供应商编码")] = None,
    billing_bucket: Annotated[
        BillingBucket | None,
        Query(description="计费桶：billable / auto_released / dispute_hold / other_unbillable"),
    ] = None,
    cursor: Annotated[str | None, Query(description="上一页返回的 next_cursor")] = None,
    limit: PageLimit = None,
):
    with translated():
        items, next_cursor = await service.list_recon_diffs(
            container,
            session,
            scope_in=scope.payload,
            batch_id=batch_id,
            diff_type=None if diff_type is None else diff_type.value,
            needs_manual=needs_manual,
            supplier_code=supplier_code,
            billing_bucket=None if billing_bucket is None else billing_bucket.value,
            cursor=cursor,
            limit=limit,
        )
    return Page[ReconDiffOut](items=items, next_cursor=next_cursor)


@router.get(
    "/runs/{batch_id}/diffs/{diff_id}",
    response_model=ReconDiffDetailOut,
    summary="差异明细",
    description="差异本体 + 命中行 + 差异草稿 + 未归因金额，均可回溯到影像坐标。",
)
async def get_diff(
    batch_id: UUID,
    diff_id: str,
    container: ContainerDep,
    session: SessionDep,
    scope: ScopeDep,
):
    with translated():
        return await service.get_recon_diff(
            container,
            session,
            scope_in=scope.payload,
            batch_id=batch_id,
            diff_id=diff_id,
        )
