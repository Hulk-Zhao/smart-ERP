"""对账批次的可持久化快照（只存计量/审计需要的程序性事实）。

为什么单独一层：对账引擎的 ``ReconResult`` 是内存对象，含大量展示字段；落库只
需要「能重建计量表与按行审计」的最小事实集。快照与引擎解耦后，写入口
（``run_recon``）与读路径（statement）共用同一结构，文件替身/SQL 适配器只需
实现 ``save/load``，不必理解引擎细节。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Any
from uuid import UUID

from apps.api.store import ReconBatchRecord
from apps.recon.models import LineStatus, MatchResultLine


class UnitPriceSource(str, Enum):
    """单价来源的**封闭枚举**（0008）。

    数据库 ``ck_recon_batch_price_snapshot`` 只接受这两个值；读侧用 :meth:`parse`
    校验，未知值直接报错，不允许把自由字符串写进快照（否则「来源」会退化成注释）。
    """

    CONTRACT_PRICE = "contract_price"
    ENV_FALLBACK = "env_fallback"

    @classmethod
    def parse(cls, value: str) -> UnitPriceSource:
        try:
            return cls(value)
        except ValueError as exc:
            allowed = ", ".join(member.value for member in cls)
            raise ValueError(
                f"未知的 unit_price_source: {value!r}；只允许 {allowed}"
            ) from exc


@dataclass(frozen=True, slots=True)
class SnapshotLine:
    line_no: int
    side: str
    supplier_code: str | None
    supplier_ref: str | None
    our_ref: str | None
    status: str
    match_type: str | None
    confidence: str | None
    needs_manual: bool
    diff_types: tuple[str, ...]
    diff_facts: tuple[Mapping[str, Any], ...]
    owner: str | None
    evidence: tuple[Mapping[str, Any], ...]
    source_refs: tuple[str, ...]

    @property
    def auto_released(self) -> bool:
        return self.status == LineStatus.MATCHED.value


@dataclass(frozen=True, slots=True)
class BatchSnapshot:
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
    input_fingerprint: str
    rerun_of: UUID | None
    rerun_reason: str | None
    created_by: UUID
    created_at: datetime
    lines: tuple[SnapshotLine, ...]
    # 批次视图投影（JSON，见 0007_recon_batch_projection）。
    # NULL = 旧版本写入、未投影；非 NULL 时必须带 ``view_version``，读侧遇到未知
    # 版本直接报错，不做 best-effort 解析。该字段是批次产物的一部分，不是可丢弃
    # 缓存：diffs 的金额/建议与 missing_items 无法从 recon_line 重建。
    view: Mapping[str, Any] | None = None
    # 批次取价快照（0008）：四字段**同生共死**。取价失败时全为 None，
    # 不允许「写了价却没写来源」这种半套快照（DB 侧另有 CHECK 兜底）。
    unit_price: Decimal | None = None
    currency: str | None = None
    unit_price_source: UnitPriceSource | None = None
    price_book_id: UUID | None = None

    def in_scope(self, *, org_id: str, period: str, system_ids: tuple[str, ...]) -> bool:
        if self.org_id != org_id or self.period != period:
            return False
        return set(self.system_ids) <= set(system_ids)


def snapshot_from_batch(
    batch: ReconBatchRecord,
    *,
    input_fingerprint: str,
    rerun_of: UUID | None = None,
    rerun_reason: str | None = None,
    view: Mapping[str, Any] | None = None,
) -> BatchSnapshot:
    result = batch.result
    supplier_code_by_ref = {
        diff.ref: diff.supplier_code for diff in result.diffs if diff.supplier_code
    }
    unattributed_by_ref = {item.ref: item for item in result.unattributed}
    lines = tuple(
        _snapshot_line(index, line, supplier_code_by_ref, unattributed_by_ref)
        for index, line in enumerate(result.lines, start=1)
    )
    return BatchSnapshot(
        batch_id=batch.batch_id,
        tenant_id=batch.tenant_id,
        org_id=batch.org_id,
        period=batch.period,
        system_ids=tuple(batch.system_ids),
        suppliers=tuple(batch.suppliers),
        status=batch.status,
        conclusive=batch.conclusive,
        status_reason=batch.status_reason,
        metrics=batch.metrics,
        input_fingerprint=input_fingerprint,
        rerun_of=rerun_of,
        rerun_reason=rerun_reason,
        created_by=batch.created_by,
        created_at=batch.created_at,
        lines=lines,
        view=view,
    )


def _snapshot_line(
    line_no: int,
    line: MatchResultLine,
    supplier_code_by_ref: Mapping[str, str],
    unattributed_by_ref: Mapping[str, Any],
) -> SnapshotLine:
    diffs = list(line.diffs)
    owners = {diff.owner.value for diff in diffs}
    owner = next(iter(owners)) if len(owners) == 1 else ("mixed" if owners else None)
    refs = [ref for ref in (line.our_ref, line.supplier_ref) if ref]
    supplier_code = next(
        (supplier_code_by_ref[ref] for ref in refs if ref in supplier_code_by_ref),
        None,
    )

    facts: list[dict[str, Any]] = []
    evidences: list[Mapping[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for diff in diffs:
        facts.append(
            {
                "type": diff.type.name.lower(),
                "owner": diff.owner.value,
                "confidence": _num(diff.confidence),
                "ref": diff.ref,
                "supplier_code": diff.supplier_code,
            }
        )
        for item in diff.evidence:
            _collect_evidence(item, evidences, seen)
    unattributed = unattributed_by_ref.get(line.supplier_ref or "")
    if unattributed is not None:
        for item in unattributed.evidence:
            _collect_evidence(item, evidences, seen)

    return SnapshotLine(
        line_no=line_no,
        side="supplier" if line.supplier_ref else "our",
        supplier_code=supplier_code,
        supplier_ref=line.supplier_ref,
        our_ref=line.our_ref,
        status=line.status.value,
        match_type=line.match_type.value if line.match_type is not None else None,
        confidence=_num(line.confidence),
        needs_manual=line.needs_manual,
        diff_types=tuple(str(fact["type"]) for fact in facts),
        diff_facts=tuple(facts),
        owner=owner,
        evidence=tuple(evidences),
        source_refs=tuple(refs),
    )


def _collect_evidence(
    item: Any,
    bucket: list[Mapping[str, Any]],
    seen: set[tuple[str, str, str]],
) -> None:
    key = (item.system, item.doc_no, item.field)
    if key in seen:
        return
    seen.add(key)
    payload: dict[str, Any] = {
        "system": item.system,
        "doc_no": item.doc_no,
        "field": item.field,
    }
    if item.coord:
        payload["coord"] = dict(item.coord)
    bucket.append(payload)


def _num(value: Decimal) -> str:
    return format(value.normalize(), "f")


__all__ = [
    "BatchSnapshot",
    "SnapshotLine",
    "UnitPriceSource",
    "snapshot_from_batch",
]
