"""产品 API 的装配容器：会话目录、幂等表、引擎依赖的唯一注入点。

为什么是容器而不是模块级单例：四条路由都要 AccessGate / AuditLedger / 连接器等
同一批依赖，测试需要按用例替换（尤其是「连接器零写入」断言）。装配入口只暴露
``configure_container`` / ``get_container`` 两个函数，路由通过依赖注入拿容器。
"""

from __future__ import annotations

import secrets
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from apps.api.approval_sql import ApprovalStorePort
from apps.api.audit_integrity import AuditIntegrityScanner
from apps.api.errors import IdemConflict
from apps.api.metering import MeteringPolicy
from apps.api.price_book import PriceBookLookup
from apps.api.recon_store import ReconStore
from apps.api.store import (
    ReconBatchRepository,
    ReconDatasetRegistry,
)
from apps.api.ticket_sql import SqlTicketRepository, TicketStorePort
from apps.audit.ledger import AuditLedger
from apps.platform.api.errors import ProblemError
from apps.platform.connectors.sdk import Connector, ConnectorMeta
from apps.platform.contracts.errors import ErrorCode
from apps.platform.policy.gateway import WriteGateway
from apps.platform.policy.levels import SceneSpec, WriteLevel
from apps.platform.policy.registry import ActionRegistry
from apps.security.approvals import ApprovalGateAdapter, ApprovalService, TicketFacts
from apps.security.gate import AccessGate, AccessGrant, AccessRequest
from apps.security.identity import SessionToken, TokenBroker, UserPrincipal
from observability.metrics import MetricsRegistry

SESSION_HEADER = "X-Session-Token"


class SessionDirectory:
    """不透明会话令牌 → ``SessionToken``；未登记/过期一律 401。"""

    def __init__(self, clock: Callable[[], datetime] | None = None) -> None:
        self._tokens: dict[str, SessionToken] = {}
        self._clock = clock or (lambda: datetime.now(UTC))

    def issue(
        self, broker: TokenBroker, principal: UserPrincipal, *, token: str | None = None
    ) -> str:
        raw = token or secrets.token_urlsafe(24)
        self._tokens[raw] = broker.issue_session(principal)
        return raw

    def bind(self, token: str, session: SessionToken) -> None:
        self._tokens[token] = session

    def resolve(self, token: str | None) -> SessionToken:
        if not token:
            raise ProblemError(
                ErrorCode.AUTHZ_SCOPE,
                f"缺少会话令牌（请求头 {SESSION_HEADER}）",
                status_code=401,
            )
        session = self._tokens.get(token)
        if session is None:
            raise ProblemError(ErrorCode.AUTHZ_SCOPE, "会话令牌无效或已注销", status_code=401)
        if session.is_expired(self._clock()):
            raise ProblemError(ErrorCode.AUTHZ_SCOPE, "会话令牌已过期", status_code=401)
        return session


@dataclass(frozen=True, slots=True)
class IdemEntry:
    fingerprint: str
    status_code: int
    body: Any


class IdempotencyStore:
    """写接口的幂等表：同键同参返回首次结果，同键不同参 409（G-02）。"""

    def __init__(self) -> None:
        self._entries: dict[str, IdemEntry] = {}

    def lookup(self, key: str, fingerprint: str) -> IdemEntry | None:
        entry = self._entries.get(key)
        if entry is None:
            return None
        if entry.fingerprint != fingerprint:
            raise IdemConflict(
                f"幂等键 {key!r} 已用于不同请求体（G-02：同键不同参直接拒绝）"
            )
        return entry

    def remember(self, key: str, fingerprint: str, status_code: int, body: Any) -> None:
        self._entries[key] = IdemEntry(fingerprint, status_code, body)


@dataclass
class ApiContainer:
    #: 工单端口：生产默认 PostgreSQL（读库 + 事件化写同一条路径）；
    #: 测试显式注入内存 ``TicketRepository``，不连库。
    tickets: TicketStorePort = field(default_factory=SqlTicketRepository)
    #: 审批记录持久化端口；``None`` 表示纯进程内（单测默认，行为与改造前一致）。
    approval_records: ApprovalStorePort | None = None
    #: 孤儿 ``audit_image_id`` 巡检；``None`` 表示不启动后台巡检（单测默认）。
    audit_integrity: AuditIntegrityScanner | None = None
    recon_batches: ReconBatchRepository = field(default_factory=ReconBatchRepository)
    recon_datasets: ReconDatasetRegistry = field(default_factory=ReconDatasetRegistry)
    ledger: AuditLedger = field(default_factory=AuditLedger)
    broker: TokenBroker = field(default_factory=TokenBroker)
    approvals: ApprovalService = field(default_factory=ApprovalService)
    gate: AccessGate | None = None
    sessions: SessionDirectory | None = None
    connectors: dict[str, Connector] = field(default_factory=dict)
    scenes: dict[str, SceneSpec] = field(default_factory=dict)
    actions: ActionRegistry = field(default_factory=ActionRegistry)
    idempotency: IdempotencyStore = field(default_factory=IdempotencyStore)
    #: 计量策略；None 表示请求时从 ``ERP_METERING_*`` 读取（测试可注入固定策略）。
    metering: MeteringPolicy | None = None
    #: 对账持久化端口；None 时使用 PostgreSQL 实现（测试注入 FileReconStore）。
    recon_store: ReconStore | None = None
    #: 进程内指标注册表；业务埋点与 ``/metrics`` 必须共用这一个实例。
    metrics: MetricsRegistry = field(default_factory=MetricsRegistry)
    #: 合同价目取价端口；None 时使用 PostgreSQL 实现（测试注入假实现，不连库）。
    price_book: PriceBookLookup | None = None
    clock: Callable[[], datetime] = field(
        default=lambda: datetime.now(UTC)
    )

    def __post_init__(self) -> None:
        self.gate = self.gate or AccessGate(broker=self.broker, approvals=self.approvals)
        self.sessions = self.sessions or SessionDirectory(clock=self.clock)

    # -- 装配辅助 ----------------------------------------------------------
    def connector_meta(self, system: str) -> ConnectorMeta:
        connector = self.connectors.get(system)
        if connector is None:
            raise ProblemError(
                ErrorCode.CONN_UNAVAILABLE,
                f"系统 {system!r} 未装配连接器，无法取得凭据/执行写入",
            )
        return connector.meta

    def pick_meta(self, system_ids: tuple[str, ...]) -> ConnectorMeta:
        for system in system_ids:
            if system in self.connectors:
                return self.connector_meta(system)
        raise ProblemError(
            ErrorCode.CONN_UNAVAILABLE,
            f"scope.system_ids={list(system_ids)} 内没有已装配的连接器",
        )

    def scene_for(self, scene_code: str, level: WriteLevel) -> SceneSpec:
        return self.scenes.get(scene_code) or SceneSpec(code=scene_code, max_level=level)

    @property
    def session_directory(self) -> SessionDirectory:
        assert self.sessions is not None  # __post_init__ 已补齐
        return self.sessions

    def authorize(self, request: AccessRequest) -> AccessGrant:
        """四道闸的唯一入口；W3 在这里就被拒，不会走到连接器。"""

        assert self.gate is not None  # __post_init__ 已补齐
        return self.gate.authorize(request)

    def gateway_for(
        self,
        system: str,
        ticket_id: str,
        facts: TicketFacts,
        *,
        narrowed_fields: frozenset[str] | None = None,
    ) -> WriteGateway:
        """为一张工单构造写闸门；连接器按工单的目标系统取，找不到直接 503。"""

        connector = self.connectors.get(system)
        if connector is None:
            raise ProblemError(
                ErrorCode.CONN_UNAVAILABLE,
                f"系统 {system!r} 未装配连接器，无法执行写入",
            )
        return WriteGateway(
            connector,
            approvals=ApprovalGateAdapter(self.approvals, {ticket_id: facts}),
            narrowed_fields=narrowed_fields,
            # 必须用容器注册表：``/metrics`` 挂的是它，用默认单例会写出「端点 200 但空值」。
            metrics=self.metrics,
        )


_CONTAINER: ApiContainer | None = None


def configure_container(container: ApiContainer) -> None:
    """测试/装配入口：替换进程内容器。"""

    global _CONTAINER
    _CONTAINER = container


def get_container() -> ApiContainer:
    global _CONTAINER
    if _CONTAINER is None:
        # 生产装配：工单/审批都落 PostgreSQL；巡检复用同一个指标注册表，
        # 保证 ``/metrics`` 能读到后台巡检写的值。
        from apps.api.approval_sql import SqlApprovalStore
        from apps.api.demo_session import bind_demo_session
        from apps.audit.ledger import AuditLedger
        from apps.audit.store import SqlAuditStore
        from apps.platform.config import get_settings
        from apps.platform.db.session import build_engine, build_session_factory

        settings = get_settings()
        # 审计读路径与巡检同源：影像来自 erp.audit_image（见 apps/audit/image_link.py），
        # 生产不再使用进程内台账，否则重启后证据区为空、且与 B-AUDIT-ORPHAN 各数一套。
        audit_store = SqlAuditStore(build_session_factory(build_engine(settings)))
        _CONTAINER = ApiContainer(
            approval_records=SqlApprovalStore(),
            ledger=AuditLedger(store=audit_store),
        )
        _CONTAINER.audit_integrity = AuditIntegrityScanner(metrics=_CONTAINER.metrics)
        # demo 档静态会话：部署期 env 注入，默认关闭；full/生产由 Settings 拒绝启动。
        bind_demo_session(_CONTAINER, get_settings())
    return _CONTAINER


def reset_container() -> None:
    global _CONTAINER
    _CONTAINER = None


__all__ = [
    "SESSION_HEADER",
    "ApiContainer",
    "IdemEntry",
    "IdempotencyStore",
    "SessionDirectory",
    "configure_container",
    "get_container",
    "reset_container",
]
