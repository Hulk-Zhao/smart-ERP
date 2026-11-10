"""纯文本 / CSV 抽取器（离线夹具与 IM 纯文本接入用）。

分页规则：
- 换页符 ``\\f`` 视为换页；
- ``第 N 页 / 共 M 页`` 视为分页标记，标记行本身不进入文本行。
  兼容页脚（``第 1 页`` 在页尾）与续页页眉（``第 2 页`` 在页首）两种排版：
  下一页页码取 ``max(当前页 + 1, N)``。

真实 PDF/图片不经过这里，由 ``stubs`` 中的插件替换。
"""

from __future__ import annotations

from ..detection import FileKind, page_marker_number
from .base import DocumentText, TextLine

_ENCODINGS = ("utf-8-sig", "utf-8", "gb18030")


class PlainTextExtractor:
    """把 UTF-8/GB18030 文本按页切成行，不做任何字段理解。"""

    name = "plaintext.v1"

    def extract(self, source: bytes, *, filename: str = "") -> DocumentText:
        text = self._decode(source, filename)
        normalized = text.replace("\r\n", "\n").replace("\r", "\n")

        current_page = 1
        pages: dict[int, list[str]] = {1: []}

        for chunk_index, chunk in enumerate(normalized.split("\f")):
            if chunk_index > 0:
                current_page += 1
                pages.setdefault(current_page, [])
            for raw_line in chunk.split("\n"):
                declared = page_marker_number(raw_line)
                if declared is not None:
                    current_page = max(current_page + 1, declared)
                    pages.setdefault(current_page, [])
                    continue
                pages[current_page].append(raw_line)

        meaningful = [number for number in sorted(pages) if any(
            line.strip() for line in pages[number]
        )]
        if not meaningful:
            meaningful = [sorted(pages)[0]] if pages else [1]
            pages.setdefault(meaningful[0], [])

        kind = FileKind.CSV if filename.lower().endswith((".csv", ".tsv")) else FileKind.TEXT
        built_pages = tuple(
            tuple(
                TextLine(page=number, index=index, text=line)
                for index, line in enumerate(pages[number])
            )
            for number in meaningful
        )
        return DocumentText(pages=built_pages, extractor=self.name, kind=kind)

    @staticmethod
    def _decode(source: bytes, filename: str) -> str:
        for encoding in _ENCODINGS:
            try:
                return source.decode(encoding)
            except UnicodeDecodeError:
                continue
        raise ValueError(f"无法解码文件 {filename!r}，仅支持 UTF-8 / GB18030 文本")
