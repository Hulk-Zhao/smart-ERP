"""审计台账：前像/后像落对象存储，字段级 diff 与哈希链落 PG。

哈希口径冻结为 ``sha256(prev_hash + canonical_json(除 hash 外全字段))``，直接复用
``apps.platform.contracts.audit``。本模块只做「追加」，不提供任何 update/delete
入口；存储层的分叉追加也会被拒绝。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol
from uuid import UUID, uuid4

from apps.audit.store import (
    AuditAnchor,
    AuditQuery,
    AuditStore,
    AuditStoreError,
    InMemoryAuditStore,
    _jsonable,
)
from apps.platform.contracts.audit import (
    AuditImage,
    FieldDiffEntry,
    canonical_json,
    compute_image_hash,
)


class AuditChainError(Exception):
    """哈希链/锚点校验失败：按安全事件处理，不自动修复。"""

    def __init__(self, message: str, verification: ChainVerification | None = None) -> None:
        super().__init__(message)
        self.verification = verification


@dataclass(frozen=True, slots=True)
class ChainVerification:
    tenant_id: UUID
    ok: bool
    checked: int
    broken_at: int | None = None
    detail: str = ""


class SnapshotStore(Protocol):
    """前像/后像快照存储（生产为 MinIO/WORM，测试为内存）。"""

    def put(
        self,
        *,
        tenant_id: UUID,
        ticket_id: UUID,
        kind: str,
        payload: Mapping[str, Any],
    ) -> str: ...

    def get(self, ref: str) -> Mapping[str, Any]: ...


class InMemorySnapshotStore:
    """内容寻址的内存对象存储：同一快照重复写入得到同一 ref。"""

    def __init__(self) -> None:
        self._objects: dict[str, dict[str, Any]] = {}

    def put(
        self,
        *,
        tenant_id: UUID,
        ticket_id: UUID,
        kind: str,
        payload: Mapping[str, Any],
    ) -> str:
        body = _jsonable(dict(payload))
        encoded = canonical_json(body).encode("utf-8")
        digest = hashlib.sha256(encoded).hexdigest()
        ref = f"mem://{tenant_id}/{kind}/{digest}"
        self._objects[ref] = body
        return ref

    def get(self, ref: str) -> Mapping[str, Any]:
        try:
            return deepcopy(self._objects[ref])
        except KeyError as exc:
            raise AuditChainError(f"快照不存在: {ref}") from exc


def compute_field_diff(
    before: Mapping[str, Any] | None, after: Mapping[str, Any] | None
) -> list[FieldDiffEntry]:
    """逐字段 diff；数值字段给出 delta，缺失侧按「未出现」而非 0 处理。"""

    if before is None or after is None:
        return []
    entries: list[FieldDiffEntry] = []
    for field in sorted(set(before) | set(after)):
        before_value = _jsonable(before.get(field))
        after_value = _jsonable(after.get(field))
        if before_value == after_value:
            continue
        entries.append(
            FieldDiffEntry(
                field=field,
                before=before_value,
                after=after_value,
                delta=_delta(before_value, after_value),
            )
        )
    return entries


def _delta(before: Any, after: Any) -> str | None:
    if isinstance(before, bool) or isinstance(after, bool):
        return None
    if not (isinstance(before, (int, float, str)) and isinstance(after, (int, float, str))):
        return None
    try:
        return str(Decimal(str(after)) - Decimal(str(before)))
    except (InvalidOperation, ValueError):
        return None


class AuditLedger:
    """唯一审计写入口。"""

    def __init__(
        self,
        *,
        store: AuditStore | None = None,
        snapshots: SnapshotStore | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._store = store or InMemoryAuditStore()
        self._snapshots = snapshots or InMemorySnapshotStore()
        self._clock = clock or (lambda: datetime.now(UTC))

    @property
    def store(self) -> AuditStore:
        return self._store

    @property
    def snapshots(self) -> SnapshotStore:
        return self._snapshots

    async def append_image(
        self,
        *,
        tenant_id: UUID,
        ticket_id: UUID,
        step_id: UUID,
        system: str,
        object_type: str,
        object_id: str,
        before: Mapping[str, Any] | None = None,
        after: Mapping[str, Any] | None = None,
        before_ref: str | None = None,
        after_ref: str | None = None,
        field_diff: Sequence[FieldDiffEntry] | None = None,
        diff_missing: bool = False,
        created_at: datetime | None = None,
        image_id: UUID | None = None,
    ) -> AuditImage:
        """追加一条审计影像。

        前像缺失仍可落库（FR-1301 异常分支：``diff_missing=True``），但写路径上
        ``WriteGateway`` 已在 preview 阶段阻断无前像的写入，这里只是审计侧兜底。
        """

        if before_ref is None and before is not None:
            before_ref = self._snapshots.put(
                tenant_id=tenant_id,
                ticket_id=ticket_id,
                kind="before",
                payload=before,
            )
        if after_ref is None and after is not None:
            after_ref = self._snapshots.put(
                tenant_id=tenant_id,
                ticket_id=ticket_id,
                kind="after",
                payload=after,
            )

        if field_diff is None:
            try:
                field_diff = compute_field_diff(before, after)
            except Exception:  # noqa: BLE001 - diff 失败不得阻断影像落库
                field_diff = []
                diff_missing = True
        if before is None or after is None:
            diff_missing = True
        normalized_diff = _normalize_diff(field_diff)

        created = _as_utc(created_at or self._clock())
        prev_hash = await self._store.last_hash(tenant_id)
        payload: dict[str, Any] = {
            "id": image_id or uuid4(),
            "tenant_id": tenant_id,
            "ticket_id": ticket_id,
            "step_id": step_id,
            "system": system,
            "object_type": object_type,
            "object_id": object_id,
            "before_ref": before_ref,
            "after_ref": after_ref,
            "field_diff": normalized_diff,
            "diff_missing": diff_missing,
            "prev_hash": prev_hash,
            "hash": "0" * 64,
            "created_at": created,
        }
        image = AuditImage.model_validate(payload)
        digest = compute_image_hash(prev_hash, image.model_dump(mode="python"))
        image = image.model_copy(update={"hash": digest})
        await self._store.append(image)
        return image

    async def list_images(self, query: AuditQuery) -> list[AuditImage]:
        return await self._store.list_images(query)

    async def verify(self, tenant_id: UUID) -> ChainVerification:
        """按追加顺序重算整条链；返回首个断裂位置。"""

        images = await self._store.chain(tenant_id)
        prev = ""
        for index, image in enumerate(images):
            if image.prev_hash != prev:
                return ChainVerification(
                    tenant_id=tenant_id,
                    ok=False,
                    checked=index,
                    broken_at=index,
                    detail=f"第 {index} 条 prev_hash 与链尾不连续",
                )
            if compute_image_hash(prev, image.model_dump(mode="python")) != image.hash:
                return ChainVerification(
                    tenant_id=tenant_id,
                    ok=False,
                    checked=index,
                    broken_at=index,
                    detail=f"第 {index} 条记录内容与 hash 不一致（疑似篡改）",
                )
            prev = image.hash
        return ChainVerification(tenant_id=tenant_id, ok=True, checked=len(images))

    async def assert_intact(self, tenant_id: UUID) -> ChainVerification:
        result = await self.verify(tenant_id)
        if not result.ok:
            raise AuditChainError(result.detail, result)
        return result

    async def anchor_day(self, tenant_id: UUID, day: date) -> AuditAnchor:
        """固化某日锚点（FR-1307）。已存在且不一致 → 安全事件。"""

        images = await self._store.chain(tenant_id)
        prefix = [
            image
            for image in images
            if _as_utc(image.created_at).date() <= day
        ]
        anchor = AuditAnchor(
            tenant_id=tenant_id,
            anchor_date=day,
            last_hash=prefix[-1].hash if prefix else "",
            record_count=len(prefix),
        )
        existing = await self._store.get_anchor(tenant_id, day)
        if existing is not None:
            if existing != anchor:
                raise AuditChainError(
                    f"锚点 {day} 与链不一致：已存 {existing.record_count} 条 / 现值 "
                    f"{anchor.record_count} 条"
                )
            return existing
        await self._store.record_anchor(anchor)
        return anchor

    async def verify_against_anchor(self, anchor: AuditAnchor) -> ChainVerification:
        """用锚点校验「任意时点完整性」，可发现截断与锚点之后的篡改。"""

        images = await self._store.chain(anchor.tenant_id)
        seen = 0
        prev = ""
        for index, image in enumerate(images):
            if seen >= anchor.record_count:
                if compute_image_hash(prev, image.model_dump(mode="python")) != image.hash:
                    return ChainVerification(
                        anchor.tenant_id, False, seen, index, "锚点之后出现断链"
                    )
                prev = image.hash
                continue
            if image.prev_hash != prev:
                return ChainVerification(
                    anchor.tenant_id, False, seen, index, "锚点范围内 prev_hash 断裂"
                )
            if compute_image_hash(prev, image.model_dump(mode="python")) != image.hash:
                return ChainVerification(
                    anchor.tenant_id, False, seen, index, "锚点范围内记录被篡改"
                )
            prev = image.hash
            seen += 1
        if seen != anchor.record_count or prev != anchor.last_hash:
            return ChainVerification(
                anchor.tenant_id,
                False,
                seen,
                None,
                f"记录数/链尾与锚点不符：{seen} vs {anchor.record_count}",
            )
        return ChainVerification(anchor.tenant_id, True, seen)


def _as_utc(value: datetime) -> datetime:
    """哈希含 created_at 的字符串形态，必须统一到 UTC，避免 PG 往返后时区漂移。"""

    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _normalize_diff(
    entries: Sequence[FieldDiffEntry | Mapping[str, Any]],
) -> list[FieldDiffEntry]:
    """把 diff 值统一成 JSON 可存形态，保证「落库后重算 hash」与写入时一致。"""

    normalized: list[FieldDiffEntry] = []
    for entry in entries:
        if isinstance(entry, FieldDiffEntry):
            normalized.append(
                FieldDiffEntry(
                    field=entry.field,
                    before=_jsonable(entry.before),
                    after=_jsonable(entry.after),
                    delta=_jsonable(entry.delta),
                )
            )
        else:
            normalized.append(
                FieldDiffEntry(
                    field=entry["field"],
                    before=_jsonable(entry.get("before")),
                    after=_jsonable(entry.get("after")),
                    delta=_jsonable(entry.get("delta")),
                )
            )
    return normalized


def dumps_anchor(anchor: AuditAnchor) -> str:
    """锚点导出（可写入外部时间戳/归档）。"""

    return json.dumps(
        {
            "tenant_id": str(anchor.tenant_id),
            "anchor_date": anchor.anchor_date.isoformat(),
            "last_hash": anchor.last_hash,
            "record_count": anchor.record_count,
        },
        sort_keys=True,
        ensure_ascii=False,
    )


__all__ = [
    "AuditChainError",
    "AuditLedger",
    "AuditQuery",
    "AuditStore",
    "AuditStoreError",
    "ChainVerification",
    "InMemorySnapshotStore",
    "SnapshotStore",
    "compute_field_diff",
]
