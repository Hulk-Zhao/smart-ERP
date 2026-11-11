"""审计实体定义（C3 / §3.4.1）。

审计影像是「字段级 diff + 前像/后像指针 + 哈希链」的载体。本模块只定义结构与
哈希算法；落库与只追加约束归 S4（``core/audit``）与迁移基线的职责边界见
``db/migrations/versions/0001_s0_baseline.py``。

哈希口径（冻结，不得随意改）：``sha256(prev_hash + canonical_json(除 hash 外全字段))``。

时间字段的规范形式（冻结）：参与哈希的 ``datetime`` 必须先归一到 UTC，再按
``YYYY-MM-DDTHH:MM:SS.ffffff+00:00`` 序列化——固定 6 位微秒、后缀 ``+00:00``
（不用 ``Z``）、不使用空格分隔；naive datetime 按 UTC 解释。
**禁止依赖 ``str(datetime)``**：它的输出由对象的 tzinfo 决定，同一行数据在写入时的
内存对象与从 PG 读回的 ``timestamptz`` 会得到不同字符串，哈希必然不匹配（真实数据
会被误判为「疑似篡改」）。``field_diff`` 等嵌套结构里的时间戳走同一规则。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class FieldDiffEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    field: str = Field(min_length=1)
    before: Any = None
    after: Any = None
    delta: Any = None


class AuditImage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: UUID
    tenant_id: UUID
    ticket_id: UUID
    step_id: UUID
    system: str = Field(min_length=1)
    object_type: str = Field(min_length=1)
    object_id: str = Field(min_length=1)
    before_ref: str | None = None
    after_ref: str | None = None
    field_diff: list[FieldDiffEntry]
    diff_missing: bool = False
    prev_hash: str
    hash: str
    created_at: datetime


def format_hash_datetime(value: datetime) -> str:
    """哈希口径的时间字符串：UTC、固定 6 位微秒、后缀 ``+00:00``。"""

    if value.tzinfo is None or value.utcoffset() is None:
        value = value.replace(tzinfo=UTC)
    else:
        value = value.astimezone(UTC)
    return value.isoformat(timespec="microseconds")


def _hash_json_default(value: Any) -> Any:
    """``json.dumps`` 兜底：时间先归一 UTC，其余保持旧行为（UUID → str）。"""

    if isinstance(value, datetime):
        return format_hash_datetime(value)
    if isinstance(value, UUID):
        return str(value)
    return str(value)


def canonical_json(payload: Mapping[str, Any]) -> str:
    """稳定序列化：键排序、紧凑分隔、非 ASCII 不转义、时间归一 UTC。"""

    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=_hash_json_default,
    )


def compute_image_hash(prev_hash: str, payload: Mapping[str, Any]) -> str:
    """计算审计影像哈希；``payload`` 必须已排除 ``hash`` 字段本身。

    传入 ``model_dump(mode="python")`` 的结果即可；其中 ``datetime`` 由
    :func:`format_hash_datetime` 归一为 UTC 固定格式，与对象的 tzinfo 无关。
    """

    body = {key: value for key, value in payload.items() if key != "hash"}
    return hashlib.sha256((prev_hash + canonical_json(body)).encode("utf-8")).hexdigest()


def verify_chain(images: list[AuditImage]) -> bool:
    """按给定顺序校验哈希链是否连续。"""

    prev = ""
    for image in images:
        if compute_image_hash(prev, image.model_dump(mode="python")) != image.hash:
            return False
        prev = image.hash
    return True
