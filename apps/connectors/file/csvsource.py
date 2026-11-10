"""CSV 读取与编码判定（只读导入）。

只接受两种编码：UTF-8 与 UTF-8 BOM（``utf-8-sig``）。其余编码按
``schema_changed`` 拒绝——编码猜错会把金额/单号读成乱码，比直接报错危险得多。
"""

from __future__ import annotations

import csv
import hashlib
import io
from collections.abc import Mapping
from dataclasses import dataclass

from apps.connectors.errors import ConnectorError, ErrorKind
from apps.connectors.file.spec import KNOWN_COLUMNS, REQUIRED_COLUMNS

UTF8_BOM = b"\xef\xbb\xbf"


@dataclass(frozen=True, slots=True)
class ParsedRow:
    """一行数据。``line_no`` 是文件里的真实行号（含表头），便于回指原始文件。"""

    file: str
    line_no: int
    values: Mapping[str, str]


@dataclass(frozen=True, slots=True)
class RowError:
    file: str
    line_no: int
    detail: str


@dataclass(frozen=True, slots=True)
class CsvPayload:
    file: str
    digest: str
    rows: tuple[ParsedRow, ...]
    row_errors: tuple[RowError, ...] = ()
    ignored_columns: tuple[str, ...] = ()


def digest_of(raw: bytes) -> str:
    """文件哈希（FR-406 / DQ-10 的重复检测口径）。"""

    return "sha256:" + hashlib.sha256(raw).hexdigest()


def decode_csv(raw: bytes, *, name: str) -> str:
    """UTF-8 / UTF-8 BOM → 文本；其它编码明确拒绝。"""

    try:
        if raw.startswith(UTF8_BOM):
            return raw.decode("utf-8-sig")
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ConnectorError(
            ErrorKind.SCHEMA_CHANGED,
            f"{name} 不是 UTF-8/UTF-8 BOM 编码（{exc.reason}），拒绝按猜测编码解析",
            detail={"file": name, "encoding_error": exc.reason},
        ) from exc


def parse_csv(text: str, *, name: str, digest: str) -> CsvPayload:
    """解析 CSV 文本；表头缺列/空文件属于结构漂移，整文件拒绝。"""

    try:
        records = list(csv.reader(io.StringIO(text)))
    except csv.Error as exc:
        raise ConnectorError(
            ErrorKind.SCHEMA_CHANGED,
            f"{name} CSV 结构损坏: {exc}",
            detail={"file": name},
        ) from exc

    if not records:
        raise ConnectorError(
            ErrorKind.SCHEMA_CHANGED, f"{name} 是空文件（无表头）", detail={"file": name}
        )

    header = [_normalize(cell) for cell in records[0]]
    if not any(header):
        raise ConnectorError(
            ErrorKind.SCHEMA_CHANGED, f"{name} 表头为空", detail={"file": name}
        )
    duplicated = sorted({column for column in header if header.count(column) > 1})
    if duplicated:
        raise ConnectorError(
            ErrorKind.SCHEMA_CHANGED,
            f"{name} 表头存在重复列: {', '.join(duplicated)}",
            detail={"file": name, "duplicated": duplicated},
        )

    missing = [column for column in REQUIRED_COLUMNS if column not in header]
    if missing:
        raise ConnectorError(
            ErrorKind.SCHEMA_CHANGED,
            f"{name} 缺少必需列: {', '.join(missing)}",
            detail={"file": name, "missing_columns": missing},
        )

    rows: list[ParsedRow] = []
    row_errors: list[RowError] = []
    for offset, raw in enumerate(records[1:], start=2):
        if not any(cell.strip() for cell in raw):
            continue  # 空行不是数据，不计入缺失
        if len(raw) != len(header):
            # 列数不符时按位置解析会整行错位，宁可跳过并报出真实行号。
            row_errors.append(
                RowError(
                    file=name,
                    line_no=offset,
                    detail=f"列数不符：表头 {len(header)} 列，本行 {len(raw)} 列",
                )
            )
            continue
        rows.append(
            ParsedRow(
                file=name,
                line_no=offset,
                values={header[index]: raw[index].strip() for index in range(len(header))},
            )
        )

    ignored = tuple(
        column for column in header if column and column not in KNOWN_COLUMNS
    )
    return CsvPayload(
        file=name,
        digest=digest,
        rows=tuple(rows),
        row_errors=tuple(row_errors),
        ignored_columns=ignored,
    )


def _normalize(cell: str) -> str:
    return cell.replace("\ufeff", "").strip().lower()
