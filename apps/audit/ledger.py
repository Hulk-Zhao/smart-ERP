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

from sqlalchemy.exc import IntegrityError

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


@dataclass(frozen=True, slots=True)
class PageVerification:
    """一页链的校验结果；``verification`` 非空即断链。

    ``checked`` 是**本页**实际校验通过的行数（断点之前），``next_cursor`` 是下一页
    的起点，``last_hash`` 是本页通过部分的链尾——扫描任务靠这三个值跨页续扫，
    不需要把整条链读进内存。
    """

    verification: ChainVerification | None
    checked: int
    next_cursor: int
    last_hash: str
    exhausted: bool

    @property
    def ok(self) -> bool:
        return self.verification is None


def _check_row(prev: str, index: int, image: AuditImage) -> ChainVerification | None:
    """单行判定（全链 ``verify`` 与分页巡检共用同一套口径，禁止各写一份）。"""

    if image.prev_hash != prev:
        return ChainVerification(
            tenant_id=image.tenant_id,
            ok=False,
            checked=index,
            broken_at=index,
            detail=f"第 {index} 条 prev_hash 与链尾不连续",
        )
    if compute_image_hash(prev, image.model_dump(mode="python")) != image.hash:
        return ChainVerification(
            tenant_id=image.tenant_id,
            ok=False,
            checked=index,
            broken_at=index,
            detail=f"第 {index} 条记录内容与 hash 不一致（疑似篡改）",
        )
    return None


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
        image_id = image_id or uuid4()
        # 并发追加时两个写入者会读到同一个链尾：唯一约束 (tenant_id, prev_hash)
        # 让后提交的一方拿到 IntegrityError。这里重读一次链尾再试一次，
        # 把「分叉」变成「重试后成功」，而不是把并发换成 500。
        for attempt in range(2):
            prev_hash = await self._store.last_hash(tenant_id)
            payload: dict[str, Any] = {
                "id": image_id,
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
            try:
                await self._store.append(image)
            except IntegrityError:
                if attempt:
                    raise
                continue
            return image
        raise AssertionError("不可达：重试循环必须以 return 或 raise 结束")

    async def list_images(self, query: AuditQuery) -> list[AuditImage]:
        return await self._store.list_images(query)

    async def tenant_ids(self) -> list[UUID]:
        return await self._store.tenant_ids()

    async def verify(self, tenant_id: UUID) -> ChainVerification:
        """按追加顺序重算整条链；返回首个断裂位置。

        注意：这是全链一次性加载（O(N) 内存 + O(N) 哈希），只适合离线/小租户；
        周期巡检走 :meth:`verify_page`，判定口径由 ``_check_row`` 共用。
        """

        images = await self._store.chain(tenant_id)
        prev = ""
        for index, image in enumerate(images):
            broken = _check_row(prev, index, image)
            if broken is not None:
                return broken
            prev = image.hash
        return ChainVerification(tenant_id=tenant_id, ok=True, checked=len(images))

    async def verify_page(
        self,
        tenant_id: UUID,
        *,
        cursor: int,
        prev_hash: str,
        limit: int,
        start_index: int = 0,
    ) -> PageVerification:
        """分页校验一段链：只把一页读进内存，跨页由调用方携带 ``prev_hash``。

        ``start_index`` 是本页第一行在**本轮全链校验**中的序号，仅用于把断裂位置
        定位成「第 N 条」，不影响判定。
        """

        rows, next_cursor = await self._store.chain_page(
            tenant_id, cursor=cursor, limit=limit
        )
        prev = prev_hash
        checked = 0
        for offset, image in enumerate(rows):
            broken = _check_row(prev, start_index + offset, image)
            if broken is not None:
                return PageVerification(
                    verification=broken,
                    checked=checked,
                    next_cursor=next_cursor,
                    last_hash=prev,
                    exhausted=False,
                )
            prev = image.hash
            checked += 1
        return PageVerification(
            verification=None,
            checked=checked,
            next_cursor=next_cursor,
            last_hash=prev,
            exhausted=len(rows) < limit,
        )

    async def assert_intact(self, tenant_id: UUID) -> ChainVerification:
        result = await self.verify(tenant_id)
        if not result.ok:
            raise AuditChainError(result.detail, result)
        return result

    async def anchor_day(self, tenant_id: UUID, day: date) -> AuditAnchor:
        """固化某日锚点（FR-1307）。已存在且与链不符 → 安全事件。

        边界按**链位置**（chain_seq 前缀）划分，不由读取时刻的 ``now()`` 或
        ``created_at`` 重新推导：首次写入时把当前链尾固化成
        ``(record_count, last_hash)``，之后只拿这两个存储值与现链比较。

        历史实现每次调用都用 ``created_at.date() <= day`` 重筛前缀，跨日时钟回拨
        （NTP 校正 / 快照恢复 / 宿主休眠唤醒）会把晚写的行补进旧日前缀，次日重算
        与已存锚点不一致并抛 :class:`AuditChainError`——安全告警被时钟噪声淹没
        （看板 B-ANCHOR：换判据之前不得接调度/日切）。
        """

        existing = await self._store.get_anchor(tenant_id, day)
        if existing is not None:
            images = await self._store.chain(tenant_id)
            if len(images) < existing.record_count:
                raise AuditChainError(
                    f"锚点 {day} 之后链被截断：锚点 {existing.record_count} 条 / 现值 "
                    f"{len(images)} 条"
                )
            anchored_tail = (
                images[existing.record_count - 1].hash if existing.record_count else ""
            )
            if anchored_tail != existing.last_hash:
                raise AuditChainError(
                    f"锚点 {day} 的链尾与现链不一致：锚点 {existing.last_hash[:12]} / 现值 "
                    f"{anchored_tail[:12]}"
                )
            return existing

        images = await self._store.chain(tenant_id)
        anchor = AuditAnchor(
            tenant_id=tenant_id,
            anchor_date=day,
            last_hash=images[-1].hash if images else "",
            record_count=len(images),
        )
        await self._store.record_anchor(anchor)
        return anchor

    async def verify_against_anchor(self, anchor: AuditAnchor) -> ChainVerification:
        """用锚点校验「任意时点完整性」，可发现截断与锚点之后的篡改。

        锚点之后的追加行只要求自身自洽，不能把它们的链尾当成锚点链尾去比——
        否则锚点之后每写一行，历史锚点都会误报 ``记录数/链尾与锚点不符``。
        """

        images = await self._store.chain(anchor.tenant_id)
        seen = 0
        prev = ""
        anchored_tail = ""
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
            anchored_tail = image.hash
            seen += 1
        if seen != anchor.record_count or anchored_tail != anchor.last_hash:
            return ChainVerification(
                anchor.tenant_id,
                False,
                seen,
                None,
                f"记录数/链尾与锚点不符：{seen} 条 / {anchored_tail[:12]} vs "
                f"{anchor.record_count} 条 / {anchor.last_hash[:12]}",
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
