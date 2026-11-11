"""产品 API 的内存仓储（工单投影 / 对账批次 / 数据集登记）。

本层只做「接口需要的读取与状态投影」，不复制状态机：推进仍走
``apps.agents.state_machine.resolve_transition``，写入仍走 ``AccessGate`` +
``WriteGateway``。PG 读写见 ``apps.api.ticket_sql``（事件与投影同事务）；这里的
内存实现只保证 API 语义可测、可替换。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from apps.agents.state_machine import TransitionContext, resolve_transition
from apps.api.pagination import decode_cursor, encode_cursor
from apps.platform.contracts.ticket import TicketState
from apps.platform.policy.levels import WriteLevel
from apps.security.scope import Scope

STEP_SUCCEEDED = "succeeded"


@dataclass(frozen=True, slots=True)
class PendingWrite:
    """工单进入待审批前锁定的写计划：审批通过后只执行这一份，不再接受新入参。"""

    action_key: str
    system: str
    payload: Mapping[str, Any]
    idem_key: str


@dataclass(slots=True)
class TicketStepRecord:
    step_no: int
    action_key: str
    system: str
    object_type: str
    object_id: str
    level: WriteLevel
    idem_key: str
    state: str = STEP_SUCCEEDED
    step_id: UUID = field(default_factory=uuid4)
    executed_at: datetime | None = None
    audit_image_id: UUID | None = None


@dataclass(slots=True)
class TicketRecord:
    ticket_no: str
    tenant_id: UUID
    scene_code: str
    initiator_id: UUID
    business_key: str
    org_id: str
    period: str
    # ``None`` ＝「涉及系统未知」（执行器建的历史工单没有这个信息）。
    # 判可见性时按 fail-closed 处理：未知不等于全部，见 ``_in_scope``。
    systems: tuple[str, ...] | None
    # 执行器建的历史工单可能没有写等级/金额；这类行因 systems 未知而不可见，
    # 但仍要能被 ``get`` 映射出来，好让详情统一走「范围未知」403。
    write_level: WriteLevel | None
    amount: Decimal | None
    touches_fund: bool
    state: TicketState = TicketState.PENDING_APPROVAL
    version: int = 1
    ticket_id: UUID = field(default_factory=uuid4)
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    pending_write: PendingWrite | None = None
    steps: list[TicketStepRecord] = field(default_factory=list)

    @property
    def systems_list(self) -> list[str]:
        return list(self.systems or ())

    def sort_key(self) -> tuple[str, str]:
        return (self.created_at.isoformat(), str(self.ticket_id))


class TicketRepository:
    """按租户保存工单投影；读方法一律先绑租户，再按 scope 过滤与键集分页。

    租户是查询条件而不是后置校验：``get`` 只在该租户的行里查，查不到即
    ``None``（调用方统一转 404），因此不存在「先按 id 取到、再判断归属」的窗口。
    """

    def __init__(self, tickets: Iterable[TicketRecord] = ()) -> None:
        self._rows: dict[UUID, TicketRecord] = {row.ticket_id: row for row in tickets}

    def add(self, ticket: TicketRecord) -> TicketRecord:
        if ticket.ticket_id in self._rows:
            raise ValueError(f"工单 {ticket.ticket_id} 已存在")
        self._rows[ticket.ticket_id] = ticket
        return ticket

    async def create(self, ticket: TicketRecord) -> TicketRecord:
        """建单（与 SQL 实现同名同义）：内存实现没有事件链，只落投影。"""

        return self.add(ticket)

    async def transition(
        self,
        *,
        tenant_id: UUID,
        ticket_id: UUID,
        target: TicketState,
        trigger: str,
        context: TransitionContext | None = None,
        actor_id: UUID | None = None,
        trace_id: str | None = None,
        payload: Mapping[str, Any] | None = None,
        approval_record: Any = None,
    ) -> int:
        """状态推进：与 ``TicketStore.transition`` 同一套校验与语义。

        返回**对外版本号**（建单即 1），与 SQL 适配器一致；内存实现没有事件表，
        因此这里不产生事件，只同步投影。``payload`` 里显式出现的
        ``pending_write`` 会覆盖写计划（与事件重放口径一致）。

        ``approval_record`` 只为与 SQL 适配器同签名而存在：内存模式没有「事务内
        落审批行」这回事，审批记录由服务层在推进后调用 ``remember`` 进缓存。
        """

        row = self._rows.get(ticket_id)
        if row is None or row.tenant_id != tenant_id:
            raise LookupError(f"工单不存在: {ticket_id}")
        resolve_transition(row.state, target, trigger, context)
        row.state = TicketState(target)
        row.version += 1
        if payload is not None and "pending_write" in payload:
            row.pending_write = _pending_from_payload(payload.get("pending_write"))
        row.updated_at = datetime.now(UTC)
        return row.version

    async def record_step(
        self,
        *,
        tenant_id: UUID,
        ticket_id: UUID,
        step: TicketStepRecord,
        request_hash: str,
        trace_id: str | None = None,
    ) -> None:
        row = self._rows.get(ticket_id)
        if row is None or row.tenant_id != tenant_id:
            raise LookupError(f"工单不存在: {ticket_id}")
        row.steps.append(step)
        row.updated_at = datetime.now(UTC)

    async def get(self, *, tenant_id: UUID, ticket_id: UUID) -> TicketRecord | None:
        row = self._rows.get(ticket_id)
        if row is None or row.tenant_id != tenant_id:
            return None
        return row

    async def save(self, ticket: TicketRecord) -> None:
        ticket.updated_at = datetime.now(UTC)
        self._rows[ticket.ticket_id] = ticket

    async def all(self, *, tenant_id: UUID) -> tuple[TicketRecord, ...]:
        return tuple(row for row in self._rows.values() if row.tenant_id == tenant_id)

    async def list(
        self,
        *,
        tenant_id: UUID,
        scope: Scope,
        state: str | None = None,
        scene_code: str | None = None,
        business_key: str | None = None,
        ticket_no: str | None = None,
        initiator_id: UUID | None = None,
        cursor: str | None = None,
        limit: int = 20,
    ) -> tuple[list[TicketRecord], str | None]:
        rows = [
            row
            for row in self._rows.values()
            if row.tenant_id == tenant_id and _in_scope(row, scope)
        ]
        if state is not None:
            rows = [row for row in rows if row.state.value == state]
        if scene_code is not None:
            rows = [row for row in rows if row.scene_code == scene_code]
        if business_key is not None:
            rows = [row for row in rows if row.business_key == business_key]
        if ticket_no is not None:
            rows = [row for row in rows if row.ticket_no == ticket_no]
        if initiator_id is not None:
            rows = [row for row in rows if row.initiator_id == initiator_id]

        rows.sort(key=lambda row: row.sort_key(), reverse=True)
        if cursor is not None:
            payload = decode_cursor(cursor)
            after = (str(payload.get("t", "")), str(payload.get("i", "")))
            rows = [row for row in rows if row.sort_key() < after]

        window = rows[: limit + 1]
        page = window[:limit]
        next_cursor = None
        if len(window) > limit:
            last = page[-1]
            next_cursor = encode_cursor(
                {"t": last.created_at.isoformat(), "i": str(last.ticket_id)}
            )
        return page, next_cursor


def _pending_from_payload(raw: Any) -> PendingWrite | None:
    """事件 payload 里的写计划快照；空对象与 NULL 同义。"""

    if not raw:
        return None
    return PendingWrite(
        action_key=str(raw["action_key"]),
        system=str(raw["system"]),
        payload=dict(raw.get("payload") or {}),
        idem_key=str(raw["idem_key"]),
    )


def _in_scope(ticket: TicketRecord, scope: Scope) -> bool:
    if ticket.org_id != scope.org_id or ticket.period != scope.period:
        return False
    if ticket.systems is None:
        # 涉及系统未知：fail-closed，看不见。若这里返回 True 或做 ``is None or ...``
        # 的短路，NULL 就等于「租户内全部可见」——最坏的一种默认值。
        return False
    return set(ticket.systems) <= set(scope.system_ids)


@dataclass(slots=True)
class ReconBatchRecord:
    batch_id: UUID
    tenant_id: UUID
    org_id: str
    period: str
    system_ids: tuple[str, ...]
    suppliers: tuple[str, ...]
    status: str
    conclusive: bool
    status_reason: str
    metrics: Mapping[str, Any] | None
    missing_items: tuple[Mapping[str, Any], ...]
    result: Any  # apps.recon.models.ReconResult
    created_by: UUID
    created_at: datetime

    @property
    def diff_count(self) -> int:
        return len(self.result.diffs)


class ReconBatchRepository:
    def __init__(self) -> None:
        self._rows: dict[UUID, ReconBatchRecord] = {}

    def add(self, batch: ReconBatchRecord) -> ReconBatchRecord:
        self._rows[batch.batch_id] = batch
        return batch

    def get(
        self, batch_id: UUID, *, tenant_id: UUID | None = None
    ) -> ReconBatchRecord | None:
        """按批次号取；API 读写路径必须传 ``tenant_id``，省略仅用于内部/测试断言。"""

        row = self._rows.get(batch_id)
        if row is None or (tenant_id is not None and row.tenant_id != tenant_id):
            return None
        return row


class ReconDatasetRegistry:
    """已登记的对账数据集：键是供应商对账单文件名（``sources.statement_files``）。"""

    def __init__(self, datasets: Mapping[str, Any] | None = None) -> None:
        self._datasets: dict[str, Any] = dict(datasets or {})

    def register(self, statement_file: str, dataset: Any) -> None:
        self._datasets[statement_file] = dataset

    def lookup(self, statement_files: Iterable[str]) -> Any | None:
        for name in statement_files:
            if name in self._datasets:
                return self._datasets[name]
        return None


__all__ = [
    "PendingWrite",
    "ReconBatchRecord",
    "ReconBatchRepository",
    "ReconDatasetRegistry",
    "TicketRecord",
    "TicketRepository",
    "TicketStepRecord",
]
