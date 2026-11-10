"""纯文本抽取器（离线夹具与 IM 纯文本接入用）。

分页规则：
- 换页符 ``\\f`` 视为换页；
- ``第 N 页 / 共 M 页`` 视为分页标记，标记行本身不进入文本行。
  兼容页脚（``第 1 页`` 在页尾）与续页页眉（``第 2 页`` 在页首）两种排版：
  下一页页码取 ``max(当前页 + 1, N)``。

多栏对齐、千分位、全角数字与负数括号等「脏文本」不在这里处理：
抽取器只负责切行，识别与降级归字段映射层（见 ``mapping/fields.py``）。
"""

from __future__ import annotations

from ..detection import FileKind, page_marker_number
from .base import DocumentText, TextLine
from .textdecode import decode_bytes


class PlainTextExtractor:
    """把 UTF-8 / GB18030 / 带 BOM 的 UTF-16 文本按页切成行，不做任何字段理解。"""

    name = "plaintext.v1"

    def extract(self, source: bytes, *, filename: str = "") -> DocumentText:
        text = decode_bytes(source, filename=filename)
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

        meaningful = [
            number
            for number in sorted(pages)
            if any(line.strip() for line in pages[number])
        ]
        if not meaningful:
            meaningful = [min(pages)] if pages else [1]
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
