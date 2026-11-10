"""E2E 冒烟夹具：在真实 ``apps.main`` 上叠加最小 HTTP 读写端点。

**测试路由不进入生产代码路径**：``apps/main.py`` 不 import、不挂载任何 ``/_e2e``
路由；本文件只在测试进程里对 ``create_app()`` 返回的实例追加路由，并且必须满足
``ERP_APP_ENV=test`` 才注册。非 test 环境下路径根本不存在（404，而不是 403）。

产品侧的只读 / 写入路由尚未由其他流交付，冒烟不能伪造一个「假 ERP」，所以这里：

- 健康检查直接用 S0 的 ``apps.main.create_app``（真实 app）；
- 只读接口与写入接口是**测试专用** ``/_e2e/*`` 路由，但内部调用真实的
  ``AccessGate``（四道闸）与 ``WriteGateway``（唯一写出口），不重写策略逻辑；
- 连接器是与生产同协议的进程内替身，不启动任何外部服务。

测试结束后不残留状态：每次 ``build_harness_app()`` 都新建连接器与 trace 导出器。
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Literal
from uuid import UUID

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from apps.main import create_app as create_main_app
from apps.platform.config import Settings
from apps.platform.connectors.sdk import (
    CapabilitySet,
    CompensateResult,
    ConnectorMeta,
    Health,
    PreviewResult,
    ReadResult,
    VerifyResult,
    WriteResult,
)
from apps.platform.contracts.errors import ERROR_SPECS, ErrorCode, problem
from apps.platform.policy.gateway import DenyAllApprovals, WriteGateway
from apps.platform.policy.levels import (
    CRITICAL_READONLY_FIELDS,
    ActionSpec,
    SceneSpec,
    VerifySpec,
    WriteLevel,
)
from apps.security.approvals import ApprovalService
from apps.security.gate import AccessGate, AccessRequest
from apps.security.identity import TokenBroker, UserPrincipal
from apps.security.scope import Scope, ScopeGrant
from observability import MemoryExporter, TracedConnector, Tracer

NOW = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)
TENANT = UUID("11111111-1111-1111-1111-111111111111")
USER = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
SESSION_SYSTEMS = ("u8",)

#: 测试路由的显式门禁；只认 ``test``，不认 ``dev`` / ``local``。
APP_ENV_VAR = "ERP_APP_ENV"
TEST_ENV = "test"
E2E_PREFIX = "/_e2e"

#: 冒烟只覆盖需要治理的写入；W0 不应走写接口。
WRITE_LEVELS = Literal["W1", "W2", "W3"]


class ScopeIn(BaseModel):
    org_id: str = Field(min_length=1)
    period: str = Field(min_length=1)
    system_ids: list[str] = Field(min_length=1)


class WriteIn(BaseModel):
    ticket_id: str = Field(min_length=1)
    action_key: str = Field(min_length=1)
    level: WRITE_LEVELS
    idem_key: str = Field(min_length=1)
    payload: dict[str, Any] = Field(default_factory=dict)
    scope: ScopeIn
    amount: str = "0"
    touches_fund: bool = False


class SmokeConnector:
    """与生产 SDK 同协议的最小替身；无网络、无 PG。"""

    def __init__(self) -> None:
        self.writes: list[tuple[str, str, str]] = []
        self.compensations: list[str] = []
        self.meta = ConnectorMeta(
            system="u8",
            version="1.0",
            access_level="L1",
            security_level="A",
            supports_obo=True,
            version_fingerprint="e2e-u8",
        )

    def describe(self) -> CapabilitySet:
        return CapabilitySet(objects=("purchase_order",), actions=())

    async def read(self, entity: str, query: Any) -> ReadResult:
        return ReadResult(items=(), completeness="complete")

    async def preview(
        self, action: ActionSpec, payload: Any
    ) -> PreviewResult:
        return PreviewResult(
            diff=(),
            before={"external_id": f"PRE-{action.key}", "snapshot": dict(payload)},
            verify_spec=VerifySpec(kind="read_by_external_id", fields=("external_id",)),
        )

    async def write(
        self,
        action: ActionSpec,
        payload: Any,
        *,
        ticket_id: str,
        idem_key: str,
    ) -> WriteResult:
        self.writes.append((action.key, ticket_id, idem_key))
        return WriteResult(
            external_id=f"EXT-{len(self.writes)}",
            response={"ok": True, "http_status": 201},
            before_ref="BEFORE-1",
            after_ref="AFTER-1",
        )

    async def verify(self, spec: VerifySpec, payload: Any) -> VerifyResult:
        return VerifyResult(status="match", observed={"ok": True})

    async def compensate(self, comp: Any, *, idem_key: str) -> CompensateResult:
        self.compensations.append(idem_key)
        return CompensateResult(status="compensated")

    async def health(self) -> Health:
        return Health(
            ok=True,
            last_sync_at=None,
            failure_rate=0.0,
            rate_limit_remaining=100.0,
            version_fingerprint="e2e-u8",
        )


class Harness:
    """把 app、连接器、网关、闸门与 trace 导出器打包，便于测试断言。"""

    def __init__(self, *, env: Mapping[str, str] | None = None) -> None:
        self.env = dict(env) if env is not None else dict(os.environ)
        self.connector = SmokeConnector()
        self.exporter = MemoryExporter()
        self.tracer = Tracer(exporter=self.exporter)

        self.broker = TokenBroker(clock=lambda: NOW)
        self.approvals = ApprovalService(clock=lambda: NOW)
        self.gate = AccessGate(broker=self.broker, approvals=self.approvals)
        self.session = self.broker.issue_session(
            UserPrincipal(
                tenant_id=TENANT,
                user_id=USER,
                roles=frozenset({"operator"}),
                grant=ScopeGrant(
                    org_ids=frozenset({"org-1"}),
                    periods=frozenset({"2026-10"}),
                    system_ids=frozenset(SESSION_SYSTEMS),
                ),
            ),
            now=NOW,
        )
        self.connector_meta = self.connector.meta

        self.app: FastAPI = create_main_app(Settings(image_tag="e2e"))
        self.routes_mounted = is_test_env(self.env)
        if self.routes_mounted:
            self._mount_routes()

    # -- 路由 ---------------------------------------------------------
    def _mount_routes(self) -> None:
        app = self.app
        harness = self

        @app.get(f"{E2E_PREFIX}/tickets/{{ticket_id}}", tags=["e2e"])
        async def read_ticket(ticket_id: str) -> dict[str, Any]:
            """只读接口：返回工单投影，不触达任何外部系统。"""

            return {
                "ticket_id": ticket_id,
                "state": "planned",
                "steps": [],
                "readonly": True,
            }

        @app.post(f"{E2E_PREFIX}/write", tags=["e2e"])
        async def write(payload: WriteIn) -> Any:
            return await harness._handle_write(payload)

    async def _handle_write(self, request: WriteIn) -> Any:
        level = WriteLevel.parse(request.level)
        action = ActionSpec(
            key=request.action_key,
            level=level,
            writable_fields=frozenset({"supplier", "amount"}),
            forbidden_fields=CRITICAL_READONLY_FIELDS,
            verify=VerifySpec(kind="read_by_external_id", fields=("external_id",)),
        )
        scope = Scope(
            org_id=request.scope.org_id,
            period=request.scope.period,
            system_ids=tuple(request.scope.system_ids),
        )
        access = AccessRequest(
            session=self.session,
            requested_scope=scope,
            connector_meta=self.connector_meta,
            ticket_id=request.ticket_id,
            level=level,
            amount=Decimal(request.amount),
            touches_fund=request.touches_fund,
            field_whitelist=frozenset({"supplier", "amount"}),
        )

        trace = self.tracer.start_trace(ticket_id=request.ticket_id, agent="AG-1")
        try:
            with self.tracer.span(
                trace,
                "gateway.write",
                level=request.level,
                action_key=request.action_key,
                idem_key=request.idem_key,
            ) as span:
                try:
                    self.gate.authorize(access)
                    gateway = WriteGateway(
                        TracedConnector(self.connector, self.tracer, trace),
                        approvals=DenyAllApprovals(),
                    )
                    result = await gateway.execute(
                        action,
                        request.payload,
                        ticket_id=request.ticket_id,
                        idem_key=request.idem_key,
                        scene=_scene_for(level),
                    )
                except Exception as exc:
                    code = getattr(exc, "code", ErrorCode.AUTHZ_SCOPE)
                    span.mark_rejected(code.value if isinstance(code, ErrorCode) else str(code))
                    raise
        except Exception as exc:  # noqa: BLE001
            return _problem_response(exc, trace.trace_id)

        return {
            "ticket_id": request.ticket_id,
            "level": result.level.name,
            "external_id": result.write.external_id,
            "trace_id": trace.trace_id,
        }


def is_test_env(env: Mapping[str, str] | None = None) -> bool:
    """只认 ``ERP_APP_ENV=test``；缺省、prod、staging、dev 一律不注册测试路由。"""

    values = env if env is not None else os.environ
    return values.get(APP_ENV_VAR, "").strip().lower() == TEST_ENV


def build_harness_app(*, env: Mapping[str, str] | None = None) -> Harness:
    return Harness(env=env)


def _scene_for(level: WriteLevel) -> SceneSpec:
    return SceneSpec("AG-1", level)


def _problem_response(error: Exception, trace_id: str) -> JSONResponse:
    code = getattr(error, "code", ErrorCode.AUTHZ_SCOPE)
    if not isinstance(code, ErrorCode):
        code = ErrorCode.AUTHZ_SCOPE
    spec = ERROR_SPECS[code]
    body = problem(code, str(error), trace_id=trace_id).model_dump(mode="json")
    return JSONResponse(
        body,
        status_code=spec.http_status,
        media_type="application/problem+json",
    )
