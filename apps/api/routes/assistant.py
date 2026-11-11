"""助手路由：本期只做一类建议——「这张工单下一步该怎么处理」。"""

from __future__ import annotations

from fastapi import APIRouter

from apps.api import service
from apps.api.deps import ContainerDep, SessionDep
from apps.api.errors import translated
from apps.api.schemas import SuggestIn, SuggestOut

router = APIRouter(prefix="/assistant", tags=["助手"])


@router.post("/suggest", response_model=SuggestOut)
async def suggest(
    body: SuggestIn, container: ContainerDep, session: SessionDep
) -> SuggestOut:
    """只读建议：不写库、不执行外部动作；LLM 不可用时降级为规则建议。"""

    with translated():
        result = await service.suggest_next_action(
            container, session, body.ticket_id
        )

    return SuggestOut(
        suggestion=result.suggestion,
        reasons=list(result.reasons),
        source=result.source,  # type: ignore[arg-type]
        model=result.model,
        generated_at=result.generated_at,
        fallback_reason=result.fallback_reason,
    )


__all__ = ["router"]
