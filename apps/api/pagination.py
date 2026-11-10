"""游标分页：游标对前端不透明，服务端只依赖「排序键 + 主键」。

两类列表用同一种编码：

- 工单/差异：键集分页（``ts`` + ``id``），避免 offset 抖动；
- 审计影像：台账接口本身按 ``offset`` 检索，游标里存 offset。

游标非法一律 ``E-PARSE-FAILED``（422），不做「猜一个默认值」。
"""

from __future__ import annotations

import base64
import json
from collections.abc import Mapping
from typing import Any

from apps.platform.api.errors import ProblemError
from apps.platform.contracts.errors import ErrorCode

DEFAULT_LIMIT = 20
MAX_LIMIT = 100


def encode_cursor(payload: Mapping[str, Any]) -> str:
    raw = json.dumps(dict(payload), sort_keys=True, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_cursor(cursor: str) -> dict[str, Any]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
    except Exception as exc:
        raise ProblemError(
            ErrorCode.PARSE_FAILED, f"cursor 非法，无法解析: {exc}"
        ) from exc
    if not isinstance(payload, dict):
        raise ProblemError(ErrorCode.PARSE_FAILED, "cursor 非法：期望 JSON 对象")
    return payload


def offset_cursor(offset: int) -> str:
    return encode_cursor({"o": offset})


def read_offset(cursor: str | None) -> int:
    if cursor is None:
        return 0
    payload = decode_cursor(cursor)
    offset = payload.get("o")
    if not isinstance(offset, int) or offset < 0:
        raise ProblemError(ErrorCode.PARSE_FAILED, "cursor 非法：offset 必须是非负整数")
    return offset


def page_bounds(limit: int | None) -> int:
    if limit is None:
        return DEFAULT_LIMIT
    if limit < 1 or limit > MAX_LIMIT:
        raise ProblemError(
            ErrorCode.PARSE_FAILED, f"limit 必须在 1..{MAX_LIMIT} 之间，收到 {limit}"
        )
    return limit


__all__ = [
    "DEFAULT_LIMIT",
    "MAX_LIMIT",
    "decode_cursor",
    "encode_cursor",
    "offset_cursor",
    "page_bounds",
    "read_offset",
]
