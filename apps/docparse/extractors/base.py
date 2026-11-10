"""抽取层公共结构：文本页、文本行、坐标估算与可插拔抽取器协议。

真实实现（PyMuPDF / PaddleOCR）只需实现 ``TextExtractor`` 协议并注册到管线，
后续阶段（分类 / 字段映射 / 置信度 / 校验）无需改动。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterator, Protocol, runtime_checkable

from ..detection import FileKind
from ..models import Coordinate

# 离线文本抽取器的坐标估算参数：真实 PDF/图片接入后由版面分析给出真实坐标
CHAR_WIDTH = 8.0
LINE_HEIGHT = 18.0
MARGIN_X = 40.0
MARGIN_Y = 40.0


@dataclass(frozen=True)
class TextLine:
    page: int  # 从 1 开始
    index: int  # 页内行号，从 0 开始
    text: str


@dataclass(frozen=True)
class DocumentText:
    pages: tuple[tuple[TextLine, ...], ...]
    extractor: str
    kind: FileKind

    @property
    def page_count(self) -> int:
        return len(self.pages)

    def iter_lines(self) -> Iterator[TextLine]:
        for page in self.pages:
            yield from page


@runtime_checkable
class TextExtractor(Protocol):
    """文本/表格抽取器协议。"""

    name: str

    def extract(self, source: bytes, *, filename: str = "") -> DocumentText:
        ...


def line_bbox(line: TextLine, start: int = 0, end: int | None = None) -> Coordinate:
    """按字符偏移估算包围盒（离线夹具专用，非真实版面坐标）。"""

    length = len(line.text)
    start = max(0, min(start, length))
    end = length if end is None else max(start + 1, min(end, length))
    x0 = MARGIN_X + start * CHAR_WIDTH
    x1 = MARGIN_X + end * CHAR_WIDTH
    y0 = MARGIN_Y + line.index * LINE_HEIGHT
    y1 = y0 + LINE_HEIGHT
    return Coordinate(page=line.page, bbox=(x0, y0, x1, y1))
