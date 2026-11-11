"""四组业务路由的汇总路由器（统一挂在 ``/v1`` 下）。"""

from __future__ import annotations

from fastapi import APIRouter

from apps.api.routes import approvals, audit, auth, metering, recon, tickets

v1_router = APIRouter(prefix="/v1")
v1_router.include_router(auth.router)
v1_router.include_router(tickets.router)
v1_router.include_router(recon.router)
v1_router.include_router(approvals.router)
v1_router.include_router(audit.router)
v1_router.include_router(metering.router)

__all__ = ["v1_router"]
