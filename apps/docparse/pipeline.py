"""解析管线编排。

阶段（对应任务拆解）：
1. ``detect_file_kind``     文件类型识别（拒绝白名单外类型）
2. ``TextExtractor``        文本/表格抽取（PDF/图片/Excel 为可插拔桩）
3. ``DocClassifier``        单据分类（< 0.8 要求人工选类型）
4. ``FieldMapper``          字段映射（单据头 + 明细行）
5. ``score_lines``          置信度打分与低置信标记
6. ``validate_consistency`` 内部一致性校验
7. 状态判定                 成功 / 需人工确认 / 无法识别（列缺失字段）

任何一步取不到值都留空并说明原因，绝不补默认值。
"""

from __future__ import annotations

from pathlib import Path
from typing import Mapping

from .detection import FileKind, detect_file_kind
from .errors import EmptyDocumentError, ExtractorNotAvailableError, UnsupportedFileTypeError
from .extractors import (
    ImageOcrExtractor,
    PdfTextExtractor,
    PlainTextExtractor,
    SpreadsheetExtractor,
    TextExtractor,
)
from .extractors.base import DocumentText
from .mapping.classifier import (
    Classification,
    DocClassifier,
    RuleBasedClassifier,
)
from .mapping.fields import FieldMapper, RuleBasedFieldMapper
from .models import (
    LOW_CONFIDENCE_THRESHOLD,
    DocLine,
    DocType,
    IssueSeverity,
    MissingField,
    ParseResult,
    ParseStatus,
    ValidationIssue,
)
from .scoring import collect_low_confidence, collect_missing_fields, score_lines
from .validation import validate_consistency

PARSER_ID = "docparse.rule.v1"


def default_extractors() -> dict[FileKind, TextExtractor]:
    text_extractor = PlainTextExtractor()
    return {
        FileKind.TEXT: text_extractor,
        FileKind.CSV: text_extractor,
        FileKind.PDF: PdfTextExtractor(),
        FileKind.IMAGE: ImageOcrExtractor(),
        FileKind.SPREADSHEET: SpreadsheetExtractor(),
    }


class DocParsePipeline:
    """离线可跑的规则管线；通过构造参数注入真实抽取器/映射器即可替换实现。"""

    def __init__(
        self,
        *,
        extractors: Mapping[FileKind, TextExtractor] | None = None,
        classifier: DocClassifier | None = None,
        mapper: FieldMapper | None = None,
    ) -> None:
        self._extractors: dict[FileKind, TextExtractor] = default_extractors()
        if extractors:
            self._extractors.update(extractors)
        self._classifier = classifier or RuleBasedClassifier()
        self._mapper = mapper or RuleBasedFieldMapper()

    def parse_file(
        self, path: str | Path, *, doc_type_hint: DocType | None = None
    ) -> ParseResult:
        source = Path(path)
        return self.parse_bytes(
            source.read_bytes(), filename=source.name, doc_type_hint=doc_type_hint
        )

    def parse_bytes(
        self,
        data: bytes,
        *,
        filename: str,
        doc_type_hint: DocType | None = None,
    ) -> ParseResult:
        kind = detect_file_kind(filename=filename, head=data[:16])
        if kind is FileKind.UNSUPPORTED:
            raise UnsupportedFileTypeError(filename)
        if not data.strip():
            raise EmptyDocumentError(filename)

        extractor = self._extractors.get(kind)
        if extractor is None:
            return self._unrecognized(filename, kind, "没有为该文件类型注册解析器")

        try:
            text = extractor.extract(data, filename=filename)
        except ExtractorNotAvailableError as error:
            return self._unrecognized(filename, kind, str(error))
        except ValueError as error:
            raise EmptyDocumentError(filename) from error

        if text.page_count == 0 or not any(
            line.text.strip() for line in text.iter_lines()
        ):
            return self._unrecognized(
                filename, kind, "文档中没有可识别文本（可能是空白页或损坏文件）"
            )

        if doc_type_hint is not None and doc_type_hint is not DocType.UNKNOWN:
            classification = Classification(doc_type=doc_type_hint, confidence=1.0)
        else:
            classification = self._classifier.classify(text)

        if classification.doc_type is DocType.UNKNOWN:
            return ParseResult(
                file_name=filename,
                file_kind=kind.value,
                doc_type=DocType.UNKNOWN,
                doc_type_confidence=classification.confidence,
                status=ParseStatus.UNRECOGNIZED,
                message=(
                    "无法识别：单据类型置信度不足，请人工选择类型后重试"
                    f"（当前 {classification.confidence:.2f}）"
                ),
                pages=text.page_count,
                parser=f"{text.extractor}+{self._classifier.name}",
            )

        mapped = self._mapper.map(text, classification.doc_type)
        scored = score_lines(list(mapped.lines))
        lines = list(scored.lines)

        issues: list[ValidationIssue] = [
            *mapped.issues,
            *scored.issues,
            *validate_consistency(mapped.header, lines),
        ]
        missing = collect_missing_fields(mapped.header, lines, classification.doc_type)
        low_confidence = collect_low_confidence(mapped.header, lines)

        status, message = self._decide(missing, low_confidence, issues, lines)

        return ParseResult(
            file_name=filename,
            file_kind=kind.value,
            doc_type=classification.doc_type,
            doc_type_confidence=classification.confidence,
            status=status,
            message=message,
            header=mapped.header,
            lines=lines,
            missing_fields=missing,
            low_confidence_fields=low_confidence,
            issues=issues,
            pages=text.page_count,
            parser=f"{extractor.name}+{self._mapper.name}",
        )

    @staticmethod
    def _decide(
        missing: list[MissingField],
        low_confidence: list[str],
        issues: list[ValidationIssue],
        lines: list[DocLine],
    ) -> tuple[ParseStatus, str | None]:
        critical_missing = [field for field in missing if field.critical]
        if critical_missing or not lines:
            reasons: list[str] = []
            if critical_missing:
                reasons.append("缺少关键字段 " + "、".join(field.label for field in critical_missing))
            if not lines:
                reasons.append("未识别到明细行")
            return ParseStatus.UNRECOGNIZED, "无法识别：" + "；".join(reasons)

        errors = [issue for issue in issues if issue.severity is IssueSeverity.ERROR]
        if low_confidence or errors or missing:
            reasons = []
            if low_confidence:
                reasons.append(f"{len(low_confidence)} 个字段置信度低于 {LOW_CONFIDENCE_THRESHOLD:.2f}")
            if errors:
                reasons.append(f"{len(errors)} 项一致性校验未通过")
            if missing:
                reasons.append(f"{len(missing)} 个明细字段缺失")
            return ParseStatus.NEEDS_REVIEW, "需人工确认：" + "；".join(reasons)

        return ParseStatus.OK, None

    @staticmethod
    def _unrecognized(filename: str, kind: FileKind, message: str) -> ParseResult:
        return ParseResult(
            file_name=filename,
            file_kind=kind.value,
            doc_type=DocType.UNKNOWN,
            doc_type_confidence=0.0,
            status=ParseStatus.UNRECOGNIZED,
            message=message,
            parser=PARSER_ID,
        )


__all__ = ["DocParsePipeline", "PARSER_ID", "default_extractors"]
