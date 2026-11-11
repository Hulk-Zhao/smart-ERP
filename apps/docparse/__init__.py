"""S5 文档解析：输出契约 + 规则管线骨架（离线可跑）。

对外入口：
- ``ParseResult`` / ``DocHeader`` / ``DocLine`` / ``FieldValue``：解析结果契约；
- ``DocParsePipeline.parse_file``：文件路径入参；
- ``DocParsePipeline.parse_bytes``：上传字节流入参（IM/邮件接入用）；
- ``match_document``：解析结果 → 主数据 Top3 候选与待人工绑定清单（纯库，未接线）。
"""

from __future__ import annotations

from .errors import (
    DocParseError,
    EmptyDocumentError,
    ExtractorNotAvailableError,
    UnsupportedFileTypeError,
)
from .master_match import (
    Candidate,
    EntityMatch,
    MasterMatchResult,
    MatchIssue,
    MatchKind,
    MatchStatus,
    PendingBinding,
    match_document,
    match_item,
    match_supplier,
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
    "Candidate",
    "Coordinate",
    "DocHeader",
    "DocLine",
    "DocParseError",
    "DocParsePipeline",
    "DocType",
    "EmptyDocumentError",
    "EntityMatch",
    "ExtractorNotAvailableError",
    "FieldSource",
    "FieldValue",
    "IssueSeverity",
    "MasterMatchResult",
    "MatchIssue",
    "MatchKind",
    "MatchStatus",
    "MissingField",
    "ParseResult",
    "ParseStatus",
    "PendingBinding",
    "StatementCsvError",
    "StatementRowIssue",
    "SupplierStatement",
    "UnsupportedFileTypeError",
    "ValidationIssue",
    "map_supplier_statement_csv",
    "match_document",
    "match_item",
    "match_supplier",
]
