"""文档解析异常。

区分两类：
- 拒收（文件层）：类型不在白名单 / 文件为空 —— 对应 FR-401「拒绝并提示」；
- 解析器不可用（能力层）：PDF/OCR/XLSX 插件尚未接入 —— 降级为 ``unrecognized`` 结果，
  由上层转「待补录」队列（FR-404）。
"""

from __future__ import annotations


class DocParseError(Exception):
    """文档解析失败基类。"""


class UnsupportedFileTypeError(DocParseError):
    def __init__(self, filename: str, reason: str = "文件类型不在白名单内") -> None:
        self.filename = filename
        self.reason = reason
        super().__init__(f"拒绝文件 {filename!r}：{reason}")


class EmptyDocumentError(DocParseError):
    def __init__(self, filename: str) -> None:
        self.filename = filename
        super().__init__(f"拒绝文件 {filename!r}：文件为空或已损坏")


class ExtractorNotAvailableError(DocParseError):
    """抽取器为桩实现，未接入真实能力。"""

    def __init__(
        self, extractor: str, required_plugin: str, message: str | None = None
    ) -> None:
        self.extractor = extractor
        self.required_plugin = required_plugin
        super().__init__(message or f"解析器未接入（桩）：{extractor}，需要 {required_plugin}")


class PdfSupportUnavailableError(ExtractorNotAvailableError):
    """未安装 ``[pdf]`` extra：明确提示安装方式，不得静默返回空文本。"""

    def __init__(self) -> None:
        super().__init__(
            "pdf.pypdfium2",
            "pypdfium2",
            message=(
                "缺少 PDF 支持：未安装 pypdfium2，请安装 `[pdf]` extra"
                '（pip install -e ".[pdf]"）'
            ),
        )


class PdfTextLayerMissingError(ExtractorNotAvailableError):
    """PDF 没有文本层（通常是扫描件）：需要 OCR，而 OCR 尚未接入。"""

    def __init__(self, pages: int) -> None:
        super().__init__(
            "pdf.pypdfium2",
            "OCR",
            message=(
                f"PDF 共 {pages} 页均无可提取文本层（疑似扫描件），需 OCR；"
                "当前未接入 OCR 能力（PaddleOCR PP-Structure），未用空文本冒充解析成功"
            ),
        )
