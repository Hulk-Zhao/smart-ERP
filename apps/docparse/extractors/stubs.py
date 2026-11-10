"""未接入的抽取能力（桩）。

这些类只声明「需要什么插件」，被调用时抛 ``ExtractorNotAvailableError``，
由管线降级为 ``unrecognized`` 结果，绝不编造字段。

替换方式：实现 ``TextExtractor.extract`` 返回 ``DocumentText``，并在构造管线时
通过 ``DocParsePipeline(extractors={FileKind.PDF: YourExtractor()})`` 覆盖即可。
"""

from __future__ import annotations

from ..errors import ExtractorNotAvailableError
from .base import DocumentText


class _StubExtractor:
    name = "stub"
    required_plugin = "未声明的插件"

    def extract(self, source: bytes, *, filename: str = "") -> DocumentText:
        raise ExtractorNotAvailableError(self.name, self.required_plugin)


class PdfTextExtractor(_StubExtractor):
    """真实实现：PyMuPDF 提取文本层；无文本层时转 OCR。"""

    name = "pdf.pymupdf.stub"
    required_plugin = "PyMuPDF 文本层抽取插件"


class ImageOcrExtractor(_StubExtractor):
    """真实实现：PaddleOCR PP-Structure 版面分析 + OCR + 表格结构识别。"""

    name = "image.paddleocr.stub"
    required_plugin = "PaddleOCR PP-Structure 版面/OCR 插件"


class SpreadsheetExtractor(_StubExtractor):
    """真实实现：openpyxl 读取单元格并保留行列坐标。"""

    name = "xlsx.openpyxl.stub"
    required_plugin = "XLSX 表格抽取插件"
