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

    def __init__(self, extractor: str, required_plugin: str) -> None:
        self.extractor = extractor
        self.required_plugin = required_plugin
        super().__init__(
            f"解析器未接入（桩）：{extractor}，需要 {required_plugin}"
        )
