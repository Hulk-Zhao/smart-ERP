"""S5 文档解析：输出契约 + 规则管线骨架（离线可跑）。

对外入口：
- ``ParseResult`` / ``DocHeader`` / ``DocLine`` / ``FieldValue``：解析结果契约；
- ``DocParsePipeline.parse_file``：文件路径入参；
- ``DocParsePipeline.parse_bytes``：上传字节流入参（IM/邮件接入用）。
"""

from __future__ import annotations

from .errors import (
    DocParseError,
    EmptyDocumentError,
    ExtractorNotAvailableError,
    UnsupportedFileTypeError,
)
from .models import (
    CLASSIFICATION_THRESHOLD,
    CONTRACT_VERSION,
    CRITICAL_HEADER_FIELDS,
    LOW_CONFIDENCE_THRESHOLD,
    Coordinate,
    DocHeader,
    DocLine,
    DocType,
    FieldSource,
    FieldValue,
    IssueSeverity,
    MissingField,
    ParseResult,
    ParseStatus,
    ValidationIssue,
)
from .pipeline import DocParsePipeline
from .statement_csv import (
    StatementCsvError,
    StatementRowIssue,
    SupplierStatement,
    map_supplier_statement_csv,
)

__all__ = [
    "CLASSIFICATION_THRESHOLD",
    "CONTRACT_VERSION",
    "CRITICAL_HEADER_FIELDS",
    "LOW_CONFIDENCE_THRESHOLD",
    "Coordinate",
    "DocHeader",
    "DocLine",
    "DocParseError",
    "DocParsePipeline",
    "DocType",
    "EmptyDocumentError",
    "ExtractorNotAvailableError",
    "FieldSource",
    "FieldValue",
    "IssueSeverity",
    "MissingField",
    "ParseResult",
    "ParseStatus",
    "StatementCsvError",
    "StatementRowIssue",
    "SupplierStatement",
    "UnsupportedFileTypeError",
    "ValidationIssue",
    "map_supplier_statement_csv",
]
