"""未接入的抽取能力（桩）。

这些类只声明「需要什么插件」，被调用时抛 ``ExtractorNotAvailableError``，
由管线降级为 ``unrecognized`` 结果，绝不编造字段。

XLSX / CSV / TXT / XML 已用标准库实现（见 ``xlsx.py`` / ``csv.py`` / ``plaintext.py`` /
``xml.py``），PDF 文本层由可选 extra 的 ``pdf.py`` 实现（未装 extra 时降级并提示
安装方式），这里只剩扫描件/图片 OCR 这一类必须依赖外部模型的能力。

- 扫描件 / 图片：需要 OCR 模型。

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


class ImageOcrExtractor(_StubExtractor):
    """真实实现：PaddleOCR PP-Structure 版面分析 + OCR + 表格结构识别。"""

    name = "image.paddleocr.stub"
    required_plugin = "PaddleOCR PP-Structure 版面/OCR 插件"
