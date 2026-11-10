"""审计实体定义（C3 / §3.4.1）。

审计影像是「字段级 diff + 前像/后像指针 + 哈希链」的载体。本模块只定义结构与
哈希算法；落库与只追加约束归 S4（``core/audit``）与迁移基线的职责边界见
``db/migrations/versions/0001_s0_baseline.py``。

哈希口径（冻结，不得随意改）：``sha256(prev_hash + canonical_json(除 hash 外全字段))``。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import datetime
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


def canonical_json(payload: Mapping[str, Any]) -> str:
    """稳定序列化：键排序、紧凑分隔、非 ASCII 不转义。"""

    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    )


def compute_image_hash(prev_hash: str, payload: Mapping[str, Any]) -> str:
    """计算审计影像哈希；``payload`` 必须已排除 ``hash`` 字段本身。

    传入 ``model_dump(mode="python")`` 的结果即可（datetime/UUID 由 ``default=str`` 归一）。
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
