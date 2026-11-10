"""对账批次落库端口：写入口（run_recon）与读路径（statement）共用。

三个实现：
- ``FileReconStore``：追加式 JSON 文件，进程/容器重建后仍在，用于证明耐久性；
- ``InMemoryReconStore``：纯内存（无持久化，仅用于不需要耐久性的调用方）；
- ``SqlReconStore``：PostgreSQL（表结构见 0003_recon_persistence），按
  ``erp.recon_batch`` / ``erp.recon_line`` / ``erp.recon_line_event`` 读写。

事件幂等：``dedupe_key = sha256(tenant|actor|line|event|时间桶)``，唯一约束兜底；
重复提交先按 ``idem_key`` 再按 ``dedupe_key`` 命中已有事件，直接返回原 ``seq``，
绝不为重试新增一条事件（否则「最新事件」不可信）。
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError

from apps.api.recon_snapshot import BatchSnapshot, SnapshotLine

EVENT_WINDOW_SECONDS = 600


class ReconConflict(ValueError):
    """同租户/组织/期间已存在等价批次，必须显式重跑。"""


@dataclass(frozen=True, slots=True)
class NewEvent:
    tenant_id: UUID
    batch_id: UUID
    line_id: UUID
    event: str
    actor: UUID
    reason: str | None = None
    evidence: tuple[Mapping[str, Any], ...] = ()


@dataclass(frozen=True, slots=True)
class ReconEvent:
    seq: int
    id: UUID
    tenant_id: UUID
    batch_id: UUID
    line_id: UUID
    event: str
    actor: UUID
    reason: str | None
    dedupe_key: str
    created_at: datetime


class ReconStore(Protocol):
    async def save(self, snapshot: BatchSnapshot) -> None: ...

    async def load(self, batch_id: UUID, tenant_id: UUID) -> BatchSnapshot | None: ...

    async def append_event(self, event: NewEvent, *, now: datetime | None = None) -> ReconEvent: ...


# --------------------------------------------------------------------------- 序列化


def snapshot_to_dict(snapshot: BatchSnapshot) -> dict[str, Any]:
    return {
        "batch_id": str(snapshot.batch_id),
        "tenant_id": str(snapshot.tenant_id),
        "org_id": snapshot.org_id,
        "period": snapshot.period,
        "system_ids": list(snapshot.system_ids),
        "suppliers": list(snapshot.suppliers),
        "status": snapshot.status,
        "conclusive": snapshot.conclusive,
        "status_reason": snapshot.status_reason,
        "metrics": snapshot.metrics,
        "input_fingerprint": snapshot.input_fingerprint,
        "rerun_of": None if snapshot.rerun_of is None else str(snapshot.rerun_of),
        "rerun_reason": snapshot.rerun_reason,
        "created_by": str(snapshot.created_by),
        "created_at": snapshot.created_at.isoformat(),
        "view": snapshot.view,
        "lines": [
            {
                "line_no": line.line_no,
                "side": line.side,
                "supplier_code": line.supplier_code,
                "supplier_ref": line.supplier_ref,
                "our_ref": line.our_ref,
                "status": line.status,
                "match_type": line.match_type,
                "confidence": line.confidence,
                "needs_manual": line.needs_manual,
                "diff_types": list(line.diff_types),
                "diff_facts": [dict(fact) for fact in line.diff_facts],
                "owner": line.owner,
                "evidence": [dict(item) for item in line.evidence],
                "source_refs": list(line.source_refs),
            }
            for line in snapshot.lines
        ],
    }


def snapshot_from_dict(payload: Mapping[str, Any]) -> BatchSnapshot:
    return BatchSnapshot(
        batch_id=UUID(str(payload["batch_id"])),
        tenant_id=UUID(str(payload["tenant_id"])),
        org_id=str(payload["org_id"]),
        period=str(payload["period"]),
        system_ids=tuple(payload["system_ids"]),
        suppliers=tuple(payload["suppliers"]),
        status=str(payload["status"]),
        conclusive=bool(payload["conclusive"]),
        status_reason=str(payload["status_reason"]),
        metrics=payload.get("metrics"),
        input_fingerprint=str(payload["input_fingerprint"]),
        rerun_of=UUID(str(payload["rerun_of"])) if payload.get("rerun_of") else None,
        rerun_reason=payload.get("rerun_reason"),
        created_by=UUID(str(payload["created_by"])),
        created_at=datetime.fromisoformat(str(payload["created_at"])),
        lines=tuple(_line_from_dict(item) for item in payload["lines"]),
        view=payload.get("view"),
    )


def _line_from_dict(payload: Mapping[str, Any]) -> SnapshotLine:
    return SnapshotLine(
        line_no=int(payload["line_no"]),
        side=str(payload["side"]),
        supplier_code=payload.get("supplier_code"),
        supplier_ref=payload.get("supplier_ref"),
        our_ref=payload.get("our_ref"),
        status=str(payload["status"]),
        match_type=payload.get("match_type"),
        confidence=payload.get("confidence"),
        needs_manual=bool(payload["needs_manual"]),
        diff_types=tuple(payload["diff_types"]),
        diff_facts=tuple(payload["diff_facts"]),
        owner=payload.get("owner"),
        evidence=tuple(payload["evidence"]),
        source_refs=tuple(payload["source_refs"]),
    )


def event_dedupe_key(
    *, tenant_id: UUID, actor: UUID, line_id: UUID, event: str, window: int
) -> str:
    raw = f"{tenant_id}|{actor}|{line_id}|{event}|{window}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- 文件/内存实现


class FileReconStore:
    """追加式 JSON 文件：原子替换写、每次读取都回源，跨容器/进程仍可见。"""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)

    async def save(self, snapshot: BatchSnapshot) -> None:
        payload = self._read()
        existing = payload["batches"].get(str(snapshot.batch_id))
        if existing is not None:
            return
        for other in payload["batches"].values():
            if (
                other["org_id"] == snapshot.org_id
                and other["period"] == snapshot.period
                and other["input_fingerprint"] == snapshot.input_fingerprint
                and other.get("rerun_of") == (
                    None if snapshot.rerun_of is None else str(snapshot.rerun_of)
                )
                and other["tenant_id"] == str(snapshot.tenant_id)
            ):
                raise ReconConflict(
                    "同租户/组织/期间已存在等价批次；重跑必须显式指定 rerun_of"
                )
        payload["batches"][str(snapshot.batch_id)] = snapshot_to_dict(snapshot)
        self._write(payload)

    async def load(self, batch_id: UUID, tenant_id: UUID) -> BatchSnapshot | None:
        row = self._read()["batches"].get(str(batch_id))
        if row is None or row["tenant_id"] != str(tenant_id):
            return None
        return snapshot_from_dict(row)

    async def append_event(
        self, event: NewEvent, *, now: datetime | None = None
    ) -> ReconEvent:
        moment = now or datetime.now(UTC)
        window = int(moment.timestamp()) // EVENT_WINDOW_SECONDS
        dedupe = event_dedupe_key(
            tenant_id=event.tenant_id,
            actor=event.actor,
            line_id=event.line_id,
            event=event.event,
            window=window,
        )
        payload = self._read()
        for row in payload["events"]:
            if row["tenant_id"] == str(event.tenant_id) and row["dedupe_key"] == dedupe:
                return _event_from_dict(row)
        seq = len(payload["events"]) + 1
        record = {
            "seq": seq,
            "id": str(uuid4()),
            "tenant_id": str(event.tenant_id),
            "batch_id": str(event.batch_id),
            "line_id": str(event.line_id),
            "event": event.event,
            "actor": str(event.actor),
            "reason": event.reason,
            "dedupe_key": dedupe,
            "created_at": moment.isoformat(),
        }
        payload["events"].append(record)
        self._write(payload)
        return _event_from_dict(record)

    def _read(self) -> dict[str, Any]:
        if not self._path.exists():
            return {"batches": {}, "events": []}
        return json.loads(self._path.read_text(encoding="utf-8"))

    def _write(self, payload: Mapping[str, Any]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False, sort_keys=True), encoding="utf-8"
        )
        os.replace(tmp, self._path)


class InMemoryReconStore:
    def __init__(self) -> None:
        self._batches: dict[UUID, BatchSnapshot] = {}
        self._events: list[dict[str, Any]] = []

    async def save(self, snapshot: BatchSnapshot) -> None:
        self._batches[snapshot.batch_id] = snapshot

    async def load(self, batch_id: UUID, tenant_id: UUID) -> BatchSnapshot | None:
        snapshot = self._batches.get(batch_id)
        if snapshot is None or snapshot.tenant_id != tenant_id:
            return None
        return snapshot

    async def append_event(
        self, event: NewEvent, *, now: datetime | None = None
    ) -> ReconEvent:
        moment = now or datetime.now(UTC)
        window = int(moment.timestamp()) // EVENT_WINDOW_SECONDS
        dedupe = event_dedupe_key(
            tenant_id=event.tenant_id,
            actor=event.actor,
            line_id=event.line_id,
            event=event.event,
            window=window,
        )
        for row in self._events:
            if row["tenant_id"] == event.tenant_id and row["dedupe_key"] == dedupe:
                return row["event"]
        record = ReconEvent(
            seq=len(self._events) + 1,
            id=uuid4(),
            tenant_id=event.tenant_id,
            batch_id=event.batch_id,
            line_id=event.line_id,
            event=event.event,
            actor=event.actor,
            reason=event.reason,
            dedupe_key=dedupe,
            created_at=moment,
        )
        self._events.append({"tenant_id": event.tenant_id, "dedupe_key": dedupe, "event": record})
        return record


def _event_from_dict(payload: Mapping[str, Any]) -> ReconEvent:
    return ReconEvent(
        seq=int(payload["seq"]),
        id=UUID(str(payload["id"])),
        tenant_id=UUID(str(payload["tenant_id"])),
        batch_id=UUID(str(payload["batch_id"])),
        line_id=UUID(str(payload["line_id"])),
        event=str(payload["event"]),
        actor=UUID(str(payload["actor"])),
        reason=payload.get("reason"),
        dedupe_key=str(payload["dedupe_key"]),
        created_at=datetime.fromisoformat(str(payload["created_at"])),
    )


# --------------------------------------------------------------------------- SQL 实现


class SqlReconStore:
    """PostgreSQL 适配器：首次使用时才建引擎，导入/装配阶段不连库。"""

    def __init__(self, session_factory: Any | None = None) -> None:
        self._session_factory = session_factory

    def _factory(self) -> Any:
        if self._session_factory is None:
            from apps.platform.db.session import build_engine, build_session_factory

            self._session_factory = build_session_factory(build_engine())
        return self._session_factory

    async def save(self, snapshot: BatchSnapshot) -> None:
        factory = self._factory()
        async with factory() as session:
            try:
                inserted = (
                    await session.execute(
                        sa.text(
                            """
                            INSERT INTO erp.recon_batch
                              (id, tenant_id, org_id, period, status, conclusive,
                               status_reason, suppliers, system_ids, metrics,
                               input_fingerprint, rerun_of, rerun_reason,
                               created_by, created_at, result_view)
                            VALUES
                              (:id, :tenant_id, :org_id, :period, :status, :conclusive,
                               :status_reason, :suppliers, :system_ids,
                               CAST(:metrics AS jsonb), :input_fingerprint,
                               :rerun_of, :rerun_reason, :created_by, :created_at,
                               CAST(:result_view AS jsonb))
                            ON CONFLICT (id) DO NOTHING
                            RETURNING id
                            """
                        ),
                        {
                            "id": snapshot.batch_id,
                            "tenant_id": snapshot.tenant_id,
                            "org_id": snapshot.org_id,
                            "period": snapshot.period,
                            "status": snapshot.status,
                            "conclusive": snapshot.conclusive,
                            "status_reason": snapshot.status_reason,
                            "suppliers": list(snapshot.suppliers),
                            "system_ids": list(snapshot.system_ids),
                            "metrics": None
                            if snapshot.metrics is None
                            else json.dumps(snapshot.metrics, ensure_ascii=False),
                            "input_fingerprint": snapshot.input_fingerprint,
                            "rerun_of": snapshot.rerun_of,
                            "rerun_reason": snapshot.rerun_reason,
                            "created_by": snapshot.created_by,
                            "created_at": snapshot.created_at,
                            "result_view": None
                            if snapshot.view is None
                            else json.dumps(snapshot.view, ensure_ascii=False),
                        },
                    )
                ).first()
                if inserted is None:
                    await session.rollback()
                    return
                for line in snapshot.lines:
                    await session.execute(
                        sa.text(
                            """
                            INSERT INTO erp.recon_line
                              (id, batch_id, tenant_id, side, line_no,
                               supplier_code, supplier_ref, our_ref, status,
                               match_type, confidence, needs_manual, diffs, evidence)
                            VALUES
                              (:id, :batch_id, :tenant_id, :side, :line_no,
                               :supplier_code, :supplier_ref, :our_ref, :status,
                               :match_type, :confidence, :needs_manual,
                               CAST(:diffs AS jsonb), CAST(:evidence AS jsonb))
                            """
                        ),
                        {
                            "id": uuid4(),
                            "batch_id": snapshot.batch_id,
                            "tenant_id": snapshot.tenant_id,
                            "side": line.side,
                            "line_no": line.line_no,
                            "supplier_code": line.supplier_code,
                            "supplier_ref": line.supplier_ref,
                            "our_ref": line.our_ref,
                            "status": line.status,
                            "match_type": line.match_type,
                            "confidence": line.confidence,
                            "needs_manual": line.needs_manual,
                            "diffs": json.dumps(
                                [dict(fact) for fact in line.diff_facts], ensure_ascii=False
                            ),
                            "evidence": json.dumps(
                                [dict(item) for item in line.evidence], ensure_ascii=False
                            ),
                        },
                    )
                await session.commit()
            except IntegrityError as exc:
                await session.rollback()
                raise ReconConflict(
                    "同租户/组织/期间已存在等价批次；重跑必须显式指定 rerun_of"
                ) from exc

    async def load(self, batch_id: UUID, tenant_id: UUID) -> BatchSnapshot | None:
        factory = self._factory()
        async with factory() as session:
            batch = (
                await session.execute(
                    sa.text(
                        """
                        SELECT id, tenant_id, org_id, period, status, conclusive,
                               status_reason, suppliers, system_ids, metrics,
                               input_fingerprint, rerun_of, rerun_reason,
                               created_by, created_at, result_view
                        FROM erp.recon_batch
                        WHERE id = :id AND tenant_id = :tenant_id
                        """
                    ),
                    {"id": batch_id, "tenant_id": tenant_id},
                )
            ).mappings().first()
            if batch is None:
                return None
            rows = (
                await session.execute(
                    sa.text(
                        """
                        SELECT side, line_no, supplier_code, supplier_ref, our_ref,
                               status, match_type, confidence, needs_manual,
                               diffs, evidence
                        FROM erp.recon_line
                        WHERE batch_id = :batch_id AND tenant_id = :tenant_id
                        ORDER BY line_no, side
                        """
                    ),
                    {"batch_id": batch_id, "tenant_id": tenant_id},
                )
            ).mappings().all()
        lines = tuple(_line_from_row(row) for row in rows)
        return BatchSnapshot(
            batch_id=batch["id"],
            tenant_id=batch["tenant_id"],
            org_id=batch["org_id"],
            period=batch["period"],
            system_ids=tuple(batch["system_ids"]),
            suppliers=tuple(batch["suppliers"]),
            status=batch["status"],
            conclusive=batch["conclusive"],
            status_reason=batch["status_reason"],
            metrics=batch["metrics"],
            input_fingerprint=batch["input_fingerprint"],
            rerun_of=batch["rerun_of"],
            rerun_reason=batch["rerun_reason"],
            created_by=batch["created_by"],
            created_at=batch["created_at"],
            lines=lines,
            view=batch["result_view"],
        )

    async def append_event(
        self, event: NewEvent, *, now: datetime | None = None
    ) -> ReconEvent:
        moment = now or datetime.now(UTC)
        window = int(moment.timestamp()) // EVENT_WINDOW_SECONDS
        dedupe = event_dedupe_key(
            tenant_id=event.tenant_id,
            actor=event.actor,
            line_id=event.line_id,
            event=event.event,
            window=window,
        )
        factory = self._factory()
        async with factory() as session:
            row = (
                await session.execute(
                    sa.text(
                        """
                        INSERT INTO erp.recon_line_event
                          (tenant_id, batch_id, line_id, event, actor, reason,
                           evidence, dedupe_key)
                        VALUES
                          (:tenant_id, :batch_id, :line_id, :event, :actor,
                           :reason, CAST(:evidence AS jsonb), :dedupe_key)
                        ON CONFLICT (tenant_id, dedupe_key) DO NOTHING
                        RETURNING seq, id, created_at
                        """
                    ),
                    {
                        "tenant_id": event.tenant_id,
                        "batch_id": event.batch_id,
                        "line_id": event.line_id,
                        "event": event.event,
                        "actor": event.actor,
                        "reason": event.reason,
                        "evidence": json.dumps(
                            [dict(item) for item in event.evidence], ensure_ascii=False
                        ),
                        "dedupe_key": dedupe,
                    },
                )
            ).mappings().first()
            if row is None:
                row = (
                    await session.execute(
                        sa.text(
                            """
                            SELECT seq, id, created_at FROM erp.recon_line_event
                            WHERE tenant_id = :tenant_id AND dedupe_key = :dedupe_key
                            """
                        ),
                        {"tenant_id": event.tenant_id, "dedupe_key": dedupe},
                    )
                ).mappings().one()
            await session.commit()
        return ReconEvent(
            seq=int(row["seq"]),
            id=row["id"],
            tenant_id=event.tenant_id,
            batch_id=event.batch_id,
            line_id=event.line_id,
            event=event.event,
            actor=event.actor,
            reason=event.reason,
            dedupe_key=dedupe,
            created_at=row["created_at"],
        )


def _line_from_row(row: Mapping[str, Any]) -> SnapshotLine:
    diffs = row["diffs"] or []
    return SnapshotLine(
        line_no=int(row["line_no"]),
        side=str(row["side"]),
        supplier_code=row["supplier_code"],
        supplier_ref=row["supplier_ref"],
        our_ref=row["our_ref"],
        status=str(row["status"]),
        match_type=row["match_type"],
        confidence=_confidence(row["confidence"]),
        needs_manual=bool(row["needs_manual"]),
        diff_types=tuple(str(item["type"]) for item in diffs),
        diff_facts=tuple(diffs),
        owner=_owner_of(diffs),
        evidence=tuple(row["evidence"] or []),
        source_refs=tuple(
            ref for ref in (row["our_ref"], row["supplier_ref"]) if ref
        ),
    )


def _confidence(value: Any) -> str | None:
    """与快照层 ``_num`` 同口径：Decimal 归一化，保证 SQL/File 读回逐字段一致。"""

    if value is None:
        return None
    if isinstance(value, Decimal):
        return format(value.normalize(), "f")
    return str(value)


def _owner_of(diffs: list[Mapping[str, Any]]) -> str | None:
    owners = {str(item.get("owner")) for item in diffs if item.get("owner")}
    if not owners:
        return None
    return next(iter(owners)) if len(owners) == 1 else "mixed"


__all__ = [
    "EVENT_WINDOW_SECONDS",
    "FileReconStore",
    "InMemoryReconStore",
    "NewEvent",
    "ReconConflict",
    "ReconEvent",
    "ReconStore",
    "SqlReconStore",
    "event_dedupe_key",
    "snapshot_from_dict",
    "snapshot_to_dict",
]
