"""文件通道的私有传输层：定位文件 → 读字节 → 编码/结构解析 → 只读幂等。

只允许 ``apps.connectors.file`` 包内使用（对外能力一律走
``apps.platform.connectors.sdk.BaseConnector``，写入口在 ``WriteGateway``）。

幂等口径：读取结果按 ``(实体, 文件列表, 文件 sha256, 过滤条件, limit)`` 缓存。
文件内容变 → 哈希变 → 键变，不会返回旧的解析结果；同一份内容重复读只解析一次
（下游据此避免重复导入，FR-406 / DQ-10）。
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from apps.connectors.breaker import CircuitBreaker
from apps.connectors.errors import ConnectorError, ErrorKind
from apps.connectors.file.csvsource import (
    CsvPayload,
    ParsedRow,
    RowError,
    decode_csv,
    digest_of,
    parse_csv,
)
from apps.connectors.file.spec import READ_LIMIT_KEY
from apps.connectors.idempotency import IdempotencyStore, IdemStatus, request_hash
from apps.connectors.ratelimit import ActionRateLimiter
from apps.connectors.runtime import ResilientCaller


@dataclass(frozen=True, slots=True)
class FileReadPayload:
    """一次读取的合并结果（多文件时按文件名排序拼接）。"""

    files: tuple[str, ...]
    rows: tuple[ParsedRow, ...]
    row_errors: tuple[RowError, ...]
    digests: Mapping[str, str]
    ignored_columns: tuple[str, ...] = ()


class FileTransport:
    def __init__(
        self,
        *,
        root: Path,
        caller: ResilientCaller,
        idem: IdempotencyStore,
    ) -> None:
        self._root = Path(root)
        self._caller = caller
        self._idem = idem
        self._parse_count = 0
        self._access_count = 0

    @property
    def root(self) -> Path:
        return self._root

    @property
    def breaker(self) -> CircuitBreaker:
        return self._caller.breaker

    @property
    def limiter(self) -> ActionRateLimiter:
        return self._caller.limiter

    @property
    def rate_limit_remaining(self) -> float:
        return self._caller.limiter.remaining_ratio(READ_LIMIT_KEY)

    @property
    def parse_count(self) -> int:
        """真正解析过 CSV 的次数（幂等命中不增加）。"""

        return self._parse_count

    @property
    def access_count(self) -> int:
        """真正读过文件的次数（熔断打开时应保持不变）。"""

        return self._access_count

    async def read(self, entity: str, params: Mapping[str, Any]) -> FileReadPayload:
        return await self._caller.call(
            READ_LIMIT_KEY, lambda: self._read(entity, params), phase="read"
        )

    async def _read(self, entity: str, params: Mapping[str, Any]) -> FileReadPayload:
        paths = self._resolve(params)
        blobs: dict[str, tuple[Path, bytes]] = {}
        for path in paths:
            blobs[path.name] = (path, await self._read_bytes(path))

        digests = {name: digest_of(raw) for name, (_, raw) in blobs.items()}
        key = request_hash(
            {
                "entity": entity,
                "files": list(blobs),
                "digests": digests,
                "filters": dict(params.get("filters") or {}),
                "limit": params.get("limit"),
            }
        )
        decision = await self._idem.begin(key, key)
        if decision.status is IdemStatus.REPLAY and decision.response is not None:
            return _payload_from_stored(decision.response)

        try:
            payloads = [
                parse_csv(decode_csv(raw, name=name), name=name, digest=digests[name])
                for name, (_, raw) in blobs.items()
            ]
        except Exception:
            await self._idem.fail(key)
            raise

        self._parse_count += 1
        payload = _merge(payloads)
        await self._idem.complete(key, _stored(payload))
        return payload

    def _resolve(self, params: Mapping[str, Any]) -> list[Path]:
        raw_name = params.get("file")
        name = str(raw_name).strip() if raw_name is not None else None
        if not name:
            if not self._root.is_dir():
                raise ConnectorError(
                    ErrorKind.NOT_FOUND,
                    f"文件目录不存在: {self._root}",
                    detail={"root": str(self._root)},
                )
            return sorted(
                path
                for path in self._root.iterdir()
                if path.is_file() and path.suffix.lower() == ".csv"
            )

        if Path(name).is_absolute() or ".." in Path(name).parts:
            raise ConnectorError(
                ErrorKind.INVALID,
                f"文件名必须是目录内的相对路径: {name!r}",
                detail={"file": name},
            )
        path = (self._root / name).resolve()
        root = self._root.resolve()
        if path != root and root not in path.parents:
            raise ConnectorError(
                ErrorKind.INVALID,
                f"文件路径越界（不在 {root} 下）: {name!r}",
                detail={"file": name, "root": str(root)},
            )
        if path.suffix.lower() != ".csv":
            raise ConnectorError(
                ErrorKind.INVALID,
                f"文件通道只接受 .csv: {name!r}",
                detail={"file": name},
            )
        if not path.is_file():
            raise ConnectorError(
                ErrorKind.NOT_FOUND,
                f"文件不存在: {name!r}",
                detail={"file": name, "root": str(root)},
            )
        return [path]

    async def _read_bytes(self, path: Path) -> bytes:
        self._access_count += 1
        try:
            return await asyncio.to_thread(read_bytes, path)
        except FileNotFoundError as exc:
            raise ConnectorError(
                ErrorKind.NOT_FOUND, f"文件不存在: {path.name}", detail={"file": path.name}
            ) from exc
        except PermissionError as exc:
            raise ConnectorError(
                ErrorKind.FORBIDDEN,
                f"无权读取文件: {path.name}",
                detail={"file": path.name},
            ) from exc
        except OSError as exc:
            # 共享目录被占用/暂时不可达：可重试。
            raise ConnectorError(
                ErrorKind.TRANSIENT,
                f"读取文件失败: {path.name}: {exc}",
                detail={"file": path.name},
            ) from exc


def read_bytes(path: Path) -> bytes:
    """同步读文件的唯一入口；事件循环不允许同步文件 IO，调用方必须走线程。"""

    return path.read_bytes()


def _merge(payloads: list[CsvPayload]) -> FileReadPayload:
    rows: list[ParsedRow] = []
    errors: list[RowError] = []
    ignored: list[str] = []
    digests: dict[str, str] = {}
    for payload in payloads:
        rows.extend(payload.rows)
        errors.extend(payload.row_errors)
        ignored.extend(payload.ignored_columns)
        digests[payload.file] = payload.digest
    return FileReadPayload(
        files=tuple(payload.file for payload in payloads),
        rows=tuple(rows),
        row_errors=tuple(errors),
        digests=digests,
        ignored_columns=tuple(ignored),
    )


def _stored(payload: FileReadPayload) -> dict[str, Any]:
    return {
        "files": list(payload.files),
        "rows": [[row.file, row.line_no, dict(row.values)] for row in payload.rows],
        "row_errors": [
            [error.file, error.line_no, error.detail] for error in payload.row_errors
        ],
        "digests": dict(payload.digests),
        "ignored_columns": list(payload.ignored_columns),
    }


def _payload_from_stored(stored: Mapping[str, Any]) -> FileReadPayload:
    return FileReadPayload(
        files=tuple(stored.get("files") or ()),
        rows=tuple(
            ParsedRow(file=file, line_no=int(line_no), values=dict(values))
            for file, line_no, values in stored.get("rows") or ()
        ),
        row_errors=tuple(
            RowError(file=file, line_no=int(line_no), detail=detail)
            for file, line_no, detail in stored.get("row_errors") or ()
        ),
        digests=dict(stored.get("digests") or {}),
        ignored_columns=tuple(stored.get("ignored_columns") or ()),
    )
