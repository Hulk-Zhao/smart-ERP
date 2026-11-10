"""审计链守护：热路径 O(1) fail-closed + 全链巡检 + 出表闸门（§3.1.1 / §3.1.4）。

口径（对外统一表述）：**审计链保证篡改可被发现并立即冻结写入，不承诺篡改不可能。**
哈希链不是密码学意义上的不可篡改，它保证的是「改了会被发现」，以及发现后
系统立刻停止写入，避免基于被污染记录继续付款/改单。

三层防线：

1. **热路径 O(1)**：每次 ``execute`` 前取该工单「锚点」
   （``erp.ticket_event_anchor``，随每次追加同事务更新）与链尾事件，比对 seq/hash，
   再校验链尾自校验和 ``prev_hash``；任一处不符 → 拒绝执行并报 P0。
2. **定时全链巡检**：:meth:`AuditGuard.verify_all` / :meth:`AuditGuard.hourly_verify`
   / :meth:`AuditGuard.run_periodic` 做全链重放 + 锚点核对；发现断链**先**把全局写入
   置为只读，**再**发 P0（冻结先于告警），等人工确认后恢复。
3. **出表闸门**：:func:`verify_before_export`（同步）在生成计量表/对账单前强制全链校验，
   断链直接抛 :class:`AuditChainBroken`，拒绝出表。API 侧调用方式见
   ``apps/agents/AUDIT_GUARD.md``。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from apps.agents.alerts import Alert, AlertSink, NullAlertSink
from apps.agents.events import EventRecord, ReplayError, replay
from apps.agents.store import TicketStore
from apps.platform.contracts.errors import ErrorCode

WRITABLE = "writable"
READONLY = "readonly"

DEFAULT_SCAN_INTERVAL_SECONDS = 3600.0

# 出表闸门的可接受规模：超过就拒绝出表（要求按批次拆分），不降级为后台异步放行。
# 本机 PG15 实测 20k 事件 ≈0.65s，50k 事件在 2s 预算内；超限拒绝而不是硬扛。
EXPORT_EVENT_LIMIT = 50_000
EXPORT_TIME_BUDGET_SECONDS = 2.0


class AuditChainBroken(Exception):
    """审计链不可信：拒绝任何执行/出表（fail-closed）。"""

    code = ErrorCode.STATE_ILLEGAL


class ExportLimitExceeded(Exception):
    """出表批次超过可接受规模：拒绝出表，必须拆分批次，不得降级放行。"""

    code = ErrorCode.STATE_ILLEGAL


class WriteDegraded(Exception):
    """审计链巡检失败后的全局只读：写类动作一律拒绝。"""

    code = ErrorCode.STATE_ILLEGAL


@dataclass(frozen=True, slots=True)
class ScanReport:
    checked: int
    broken: tuple[UUID, ...]
    degraded: bool
    elapsed_ms: float = 0.0

    @property
    def ok(self) -> bool:
        return not self.broken


class AuditGuard:
    def __init__(
        self,
        store: TicketStore,
        *,
        alert_sink: AlertSink | None = None,
        export_event_limit: int = EXPORT_EVENT_LIMIT,
    ) -> None:
        self._store = store
        self._sf = store.session_factory
        self._alerts = alert_sink or NullAlertSink()
        self._export_event_limit = export_event_limit

    # ------------------------------------------------------------ 热路径 O(1)
    def _broken(self, ticket_id: UUID, reason: str) -> AuditChainBroken:
        self._alerts.emit(
            Alert(
                "P0",
                "AUDIT_CHAIN_BROKEN",
                f"工单 {ticket_id} 审计链异常：{reason}",
                subject=str(ticket_id),
            )
        )
        return AuditChainBroken(f"审计链异常（工单 {ticket_id}）：{reason}")

    async def assert_executable(self, ticket_id: UUID) -> None:
        """执行前校验：锚点 + 链尾自校验，不通过就拒绝。"""

        async with self._sf() as session:
            anchor = (
                await session.execute(
                    text(
                        "SELECT seq, hash FROM erp.ticket_event_anchor "
                        "WHERE ticket_id = :ticket_id"
                    ),
                    {"ticket_id": ticket_id},
                )
            ).mappings().one_or_none()
            tail = (
                await session.execute(
                    text(
                        "SELECT seq, event_type, from_state, to_state, payload, "
                        "trace_id, prev_hash, hash FROM erp.ticket_event "
                        "WHERE ticket_id = :ticket_id ORDER BY seq DESC LIMIT 1"
                    ),
                    {"ticket_id": ticket_id},
                )
            ).mappings().one_or_none()
            prev = None
            if tail is not None and tail["seq"] > 1:
                prev = (
                    await session.execute(
                        text(
                            "SELECT hash FROM erp.ticket_event "
                            "WHERE ticket_id = :ticket_id AND seq = :seq"
                        ),
                        {"ticket_id": ticket_id, "seq": tail["seq"] - 1},
                    )
                ).mappings().one_or_none()

        if tail is None:
            return
        if anchor is None:
            raise self._broken(ticket_id, "缺少事件锚点，无法确认链尾未被改写")

        record = EventRecord(
            seq=tail["seq"],
            event_type=tail["event_type"],
            from_state=tail["from_state"],
            to_state=tail["to_state"],
            payload=tail["payload"] or {},
            trace_id=tail["trace_id"],
            prev_hash=tail["prev_hash"],
            hash=tail["hash"],
        )
        if anchor["seq"] != record.seq or anchor["hash"] != record.hash:
            raise self._broken(
                ticket_id,
                f"链尾与锚点不一致：锚点 seq={anchor['seq']} hash={anchor['hash'][:12]}…，"
                f"链尾 seq={record.seq} hash={record.hash[:12]}…",
            )
        if record.computed_hash != record.hash:
            raise self._broken(ticket_id, f"seq={record.seq} 哈希自校验失败（事件被改写）")
        if prev is not None and record.prev_hash != prev["hash"]:
            raise self._broken(ticket_id, f"seq={record.seq} 的 prev_hash 与上一条不衔接")

    # -------------------------------------------------------------- 全局开关
    async def global_mode(self) -> str:
        async with self._sf() as session:
            mode = (
                await session.execute(
                    text(
                        "SELECT mode FROM erp.audit_guard_state "
                        "WHERE scope = 'global'"
                    )
                )
            ).scalar_one_or_none()
        return mode or WRITABLE

    async def set_global_mode(self, mode: str, reason: str) -> None:
        async with self._sf() as session, session.begin():
            await session.execute(
                text(
                    "INSERT INTO erp.audit_guard_state (scope, mode, reason) "
                    "VALUES ('global', :mode, :reason) "
                    "ON CONFLICT (scope) DO UPDATE SET mode = EXCLUDED.mode, "
                    "reason = EXCLUDED.reason, updated_at = now()"
                ),
                {"mode": mode, "reason": reason},
            )

    # -------------------------------------------------------------- 全链巡检
    async def _load_anchors(
        self, ticket_ids: Sequence[UUID] | None
    ) -> dict[UUID, tuple[int, str]]:
        sql = "SELECT ticket_id, seq, hash FROM erp.ticket_event_anchor"
        params: dict[str, object] = {}
        if ticket_ids is not None:
            ids = list(ticket_ids)
            if not ids:
                return {}
            sql += " WHERE ticket_id = ANY(:ids)"
            params["ids"] = ids
        async with self._sf() as session:
            rows = (await session.execute(text(sql), params)).all()
        return {row[0]: (int(row[1]), str(row[2])) for row in rows}

    async def verify_all(
        self,
        *,
        ticket_ids: Sequence[UUID] | None = None,
        refresh_anchors: bool = True,
    ) -> ScanReport:
        """全链重放 + 锚点核对。

        - 断链：**先**把全局写入置只读，**再**对每个断链工单发 P0（冻结先于告警）；
        - 全绿：可选刷新锚点（回填/校正），不动全局开关（恢复需人工确认）。
        """

        started = time.perf_counter()
        ids = list(ticket_ids) if ticket_ids is not None else None
        events_by_ticket = await self._store.list_events_bulk(ids)
        anchors = await self._load_anchors(ids)

        broken: list[tuple[UUID, str]] = []
        healthy: list[tuple[UUID, int, str]] = []
        for ticket_id, events in events_by_ticket.items():
            try:
                projection = replay(events)
            except ReplayError as exc:
                broken.append((ticket_id, f"全链校验失败：{exc}"))
                continue
            anchor = anchors.get(ticket_id)
            if anchor is None:
                broken.append((ticket_id, "缺少事件锚点，无法确认链尾未被改写"))
                continue
            if anchor != (projection.last_seq, projection.last_hash):
                broken.append(
                    (
                        ticket_id,
                        "锚点与重放链尾不一致（疑似删除/回滚链尾事件）："
                        f"锚点 seq={anchor[0]}，重放 seq={projection.last_seq}",
                    )
                )
                continue
            healthy.append((ticket_id, projection.last_seq, projection.last_hash))

        if broken:
            # 冻结先于告警：P0 发出时写入已经不可用，时延≈0。
            await self.set_global_mode(
                READONLY, f"审计链断链 {len(broken)} 个工单，全局写入降级"
            )
            for ticket_id, reason in broken:
                self._alerts.emit(
                    Alert(
                        "P0",
                        "AUDIT_CHAIN_BROKEN",
                        f"工单 {ticket_id} 全链校验失败：{reason}",
                        subject=str(ticket_id),
                    )
                )
        elif refresh_anchors and healthy:
            async with self._sf() as session, session.begin():
                for ticket_id, seq, digest in healthy:
                    await session.execute(
                        text(
                            "INSERT INTO erp.ticket_event_anchor (ticket_id, seq, hash) "
                            "VALUES (:ticket_id, :seq, :hash) "
                            "ON CONFLICT (ticket_id) DO UPDATE SET seq = EXCLUDED.seq, "
                            "hash = EXCLUDED.hash, updated_at = now()"
                        ),
                        {"ticket_id": ticket_id, "seq": seq, "hash": digest},
                    )

        return ScanReport(
            checked=len(events_by_ticket),
            broken=tuple(ticket_id for ticket_id, _ in broken),
            degraded=bool(broken),
            elapsed_ms=(time.perf_counter() - started) * 1000.0,
        )

    async def daily_verify(self, *, refresh_anchors: bool = True) -> ScanReport:
        """每日全链巡检（保留旧名；与小时级巡检同一实现）。"""

        return await self.verify_all(refresh_anchors=refresh_anchors)

    async def hourly_verify(self, *, refresh_anchors: bool = True) -> ScanReport:
        """小时级全链巡检（推荐默认周期；间隔由 :meth:`run_periodic` 配置）。"""

        return await self.verify_all(refresh_anchors=refresh_anchors)

    async def run_periodic(
        self,
        *,
        interval_seconds: float = DEFAULT_SCAN_INTERVAL_SECONDS,
        max_scans: int | None = None,
        refresh_anchors: bool = True,
    ) -> list[ScanReport]:
        """可配置周期的巡检循环：先立即扫一次，再按间隔重复。

        ``max_scans`` 仅用于测试/一次性任务；生产常驻进程不传（无限循环）。
        断链检测到后写入在返回前已冻结，因此「P0 → 写入冻结」时延≈0；
        「篡改 → 冻结」的最长时延 = 巡检间隔 + 单次全链扫描耗时。
        """

        if interval_seconds <= 0:
            raise ValueError("interval_seconds 必须大于 0")
        if max_scans is not None and max_scans < 1:
            raise ValueError("max_scans 必须 >= 1")
        reports: list[ScanReport] = []
        while True:
            reports.append(
                await self.verify_all(refresh_anchors=refresh_anchors)
            )
            if max_scans is not None and len(reports) >= max_scans:
                return reports
            await asyncio.sleep(interval_seconds)

    # -------------------------------------------------------------- 出表闸门
    async def assert_exportable(
        self, ticket_ids: Sequence[UUID] | None = None
    ) -> ScanReport:
        """异步出表闸门：规模检查 + 全链校验，任一不满足都拒绝出表。

        - 事件数超过 ``export_event_limit`` → 抛 :class:`ExportLimitExceeded`，
          要求调用方按工单/时间拆批；**不得**改成后台异步校验后继续出表；
        - 断链 → 先冻结全局写入 + P0，再抛 :class:`AuditChainBroken`。

        异步 API（FastAPI 请求内）必须 ``await`` 本方法；同步包装器只给
        CLI / 定时任务用，见 :func:`verify_before_export`。
        """

        total_events = await self._store.count_events(ticket_ids)
        if total_events > self._export_event_limit:
            raise ExportLimitExceeded(
                f"出表批次事件数 {total_events} 超过上限 {self._export_event_limit}"
                f"（约 {EXPORT_TIME_BUDGET_SECONDS:.0f}s 预算），拒绝出表；"
                "请按工单/时间拆分批次后重试"
            )

        report = await self.verify_all(ticket_ids=ticket_ids)
        if not report.ok:
            sample = ", ".join(str(item) for item in report.broken[:5])
            more = "" if len(report.broken) <= 5 else f" 等 {len(report.broken)} 个"
            raise AuditChainBroken(
                f"审计链校验失败，拒绝出表（断链工单：{sample}{more}）；"
                "全局写入已冻结为只读，需人工确认后恢复"
            )
        return report


def verify_before_export(
    database_url: str,
    *,
    ticket_ids: Iterable[UUID] | None = None,
    alert_sink: AlertSink | None = None,
) -> ScanReport:
    """同步出表闸门：**只给 CLI / 定时任务用**，异步 API 禁止调用。

    FastAPI 等异步请求里已经在跑事件循环，``asyncio.run`` 会直接抛
    ``RuntimeError: asyncio.run() cannot be called from a running event loop``。
    请求内请改用 ``await AuditGuard(store).assert_exportable(ticket_ids)``。

    - 全链重放 + 锚点核对，耗时 O(期间事件总数)，一次查询批量取事件（无 N+1）；
      本机 PG15 实测参考：5 千条约 0.2s，2 万条约 0.65s；
    - 规模上限：单批 50,000 条事件（约 2s 预算）。超限抛
      :class:`ExportLimitExceeded`，**拒绝出表**，必须拆批后重试——
      不允许降级为后台校验后放行；
    - 成功返回 :class:`ScanReport`（``checked`` 为工单数，``elapsed_ms`` 为耗时）；
    - 断链**不返回结果**，直接抛 :class:`AuditChainBroken`，调用方必须让异常冒泡，
      不得吞掉后继续出表；此时全局写入已在抛异常前冻结为只读，并已发 P0。
    """

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass
    else:  # pragma: no cover - 仅防止误用，异步侧应走 await
        raise RuntimeError(
            "verify_before_export 是同步接口，不能在运行中的事件循环里调用；"
            "异步 API 请 await AuditGuard(store).assert_exportable(ticket_ids)"
        )

    async def _run() -> ScanReport:
        engine = create_async_engine(database_url, poolclass=NullPool)
        try:
            sf = async_sessionmaker(engine, expire_on_commit=False)
            guard = AuditGuard(TicketStore(sf), alert_sink=alert_sink)
            return await guard.assert_exportable(
                None if ticket_ids is None else list(ticket_ids)
            )
        finally:
            await engine.dispose()

    return asyncio.run(_run())
