"""产品 API 的内存仓储（工单投影 / 对账批次 / 数据集登记）。

本层只做「接口需要的读取与状态投影」，不复制状态机：推进仍走
``apps.agents.state_machine.resolve_transition``，写入仍走 ``AccessGate`` +
``WriteGateway``。PG 落库由各流负责，这里的实现只保证 API 语义可测、可替换。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

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
    systems: tuple[str, ...]
    write_level: WriteLevel
    amount: Decimal
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
        return list(self.systems)

    def sort_key(self) -> tuple[str, str]:
        return (self.created_at.isoformat(), str(self.ticket_id))


class TicketRepository:
    """按租户保存工单投影；列表一律先按 scope 过滤，再做键集分页。"""

    def __init__(self, tickets: Iterable[TicketRecord] = ()) -> None:
        self._rows: dict[UUID, TicketRecord] = {row.ticket_id: row for row in tickets}

    def add(self, ticket: TicketRecord) -> TicketRecord:
        if ticket.ticket_id in self._rows:
            raise ValueError(f"工单 {ticket.ticket_id} 已存在")
        self._rows[ticket.ticket_id] = ticket
        return ticket

    def get(self, ticket_id: UUID) -> TicketRecord | None:
        return self._rows.get(ticket_id)

    def save(self, ticket: TicketRecord) -> None:
        ticket.updated_at = datetime.now(UTC)
        self._rows[ticket.ticket_id] = ticket

    def all(self) -> tuple[TicketRecord, ...]:
        return tuple(self._rows.values())

    def list(
        self,
        *,
        scope: Scope,
        state: str | None = None,
        scene_code: str | None = None,
        business_key: str | None = None,
        ticket_no: str | None = None,
        initiator_id: UUID | None = None,
        cursor: str | None = None,
        limit: int = 20,
    ) -> tuple[list[TicketRecord], str | None]:
        rows = [row for row in self._rows.values() if _in_scope(row, scope)]
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
            next_cursor = encode_cursor({"t": last.created_at.isoformat(), "i": str(last.ticket_id)})
        return page, next_cursor


def _in_scope(ticket: TicketRecord, scope: Scope) -> bool:
    if ticket.org_id != scope.org_id or ticket.period != scope.period:
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

    def get(self, batch_id: UUID) -> ReconBatchRecord | None:
        return self._rows.get(batch_id)


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
