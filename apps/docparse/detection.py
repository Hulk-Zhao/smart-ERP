"""文件类型识别（管线第 1 步）。

真实上传的白名单对齐 FR-401：pdf / jpg / png / xlsx。
``text`` / ``csv`` / ``xml`` 仅用于离线夹具、IM 纯文本接入与供应商系统导出的结构化单据，
不改变真实上传的白名单校验（白名单校验在 API 入参层，本模块负责按内容与扩展名判定实际格式）。
"""

from __future__ import annotations

import re
from enum import Enum
from pathlib import Path


class FileKind(str, Enum):
    TEXT = "text"
    CSV = "csv"
    PDF = "pdf"
    IMAGE = "image"
    SPREADSHEET = "spreadsheet"
    XML = "xml"
    UNSUPPORTED = "unsupported"


_PDF_MAGIC = b"%PDF-"
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_JPEG_MAGIC = b"\xff\xd8\xff"
_GIF_MAGIC = b"GIF8"
_ZIP_MAGIC = b"PK\x03\x04"
_XML_DECLARATION = b"<?xml"

_TEXT_SUFFIXES = {".txt", ".md", ".log"}
_CSV_SUFFIXES = {".csv", ".tsv"}
_IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png"}
_SPREADSHEET_SUFFIXES = {".xlsx", ".xlsm"}
_XML_SUFFIXES = {".xml"}


def detect_file_kind(*, filename: str, head: bytes) -> FileKind:
    """按魔数优先、扩展名兜底识别文件类型。

    ``head`` 为文件前若干字节（建议 ≥16）。无法识别时返回 ``UNSUPPORTED``，
    由调用方按 FR-401 拒绝。
    """

    suffix = Path(filename).suffix.lower()
    head = head or b""

    # 魔数优先：扩展名可伪造，内容不会
    if head.startswith(_PDF_MAGIC):
        return FileKind.PDF
    if head.startswith((_PNG_MAGIC, _JPEG_MAGIC, _GIF_MAGIC)):
        return FileKind.IMAGE
    if head.startswith(_ZIP_MAGIC):
        # xlsx 是 zip 容器；docx 等其他 zip 不在白名单
        return FileKind.SPREADSHEET if suffix in _SPREADSHEET_SUFFIXES else FileKind.UNSUPPORTED

    if suffix in _TEXT_SUFFIXES:
        return FileKind.TEXT
    if suffix in _CSV_SUFFIXES:
        return FileKind.CSV
    if suffix in _XML_SUFFIXES:
        return FileKind.XML
    if head.lstrip(b"\xef\xbb\xbf").startswith(_XML_DECLARATION):
        return FileKind.XML
    if suffix == ".pdf":
        return FileKind.PDF
    if suffix in _IMAGE_SUFFIXES:
        return FileKind.IMAGE
    if suffix in _SPREADSHEET_SUFFIXES:
        return FileKind.SPREADSHEET
    return FileKind.UNSUPPORTED


_PAGE_MARKER = re.compile(r"^第\s*(\d+)\s*页(?:\s*[/／]\s*共\s*(\d+)\s*页)?$")


def page_marker_number(text: str) -> int | None:
    """识别 ``第 N 页 / 共 M 页`` 并返回 N。"""

    matched = _PAGE_MARKER.match(text.strip())
    return int(matched.group(1)) if matched else None


def is_page_marker(text: str) -> bool:
    return page_marker_number(text) is not None
