"""抽取层公共结构：文本页、文本行、坐标估算与可插拔抽取器协议。

``TextLine.cells`` 是给表格类格式（XLSX/CSV）保留**列位**用的：纯文本行按分隔符
切分会丢掉空单元格，导致「合并单元格 / 缺列」时整行错位；表格抽取器直接给出单元格
元组后，字段映射按列索引取值，空单元格保持为空值而不是把后一列顶上来。

真实实现（pypdfium2 / PaddleOCR）只需实现 ``TextExtractor`` 协议并注册到管线，
后续阶段（分类 / 字段映射 / 置信度 / 校验）无需改动。
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from ..detection import FileKind
from ..models import Coordinate

# 离线文本抽取器的坐标估算参数：真实 PDF/图片接入后由版面分析给出真实坐标
CHAR_WIDTH = 8.0
LINE_HEIGHT = 18.0
MARGIN_X = 40.0
MARGIN_Y = 40.0
# 表格类格式（XLSX/CSV）没有真实坐标，按列序估算；仅为回链占位，不代表版面宽度
COLUMN_WIDTH = 80.0


@dataclass(frozen=True)
class TextLine:
    page: int  # 从 1 开始
    index: int  # 页内行号，从 0 开始
    text: str
    # 表格类格式的单元格；纯文本为 None（由分隔符切分）。空单元格保留为空串。
    cells: tuple[str, ...] | None = None
    # 真实版面坐标（PDF）：与 text 等长，逐字符 (x0, y0, x1, y1)，原点页面左上角，单位 pt。
    # 为 None 时回落到按字符偏移估算（离线文本/表格夹具）。
    char_boxes: tuple[tuple[float, float, float, float], ...] | None = None

    def cell_text(self, column: int) -> str:
        """取第 ``column`` 列的原始文本；非表格行返回空串。"""

        if self.cells is None or column < 0 or column >= len(self.cells):
            return ""
        return self.cells[column]


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
    """取字符区间包围盒：有真实字符框时用真实坐标，否则按字符偏移估算。"""

    length = len(line.text)
    start = max(0, min(start, length))
    end = length if end is None else max(start + 1, min(end, length))

    if line.char_boxes is not None and len(line.char_boxes) == length:
        boxes = line.char_boxes[start:end]
        if boxes:
            return Coordinate(
                page=line.page,
                bbox=(
                    min(box[0] for box in boxes),
                    min(box[1] for box in boxes),
                    max(box[2] for box in boxes),
                    max(box[3] for box in boxes),
                ),
            )

    x0 = MARGIN_X + start * CHAR_WIDTH
    x1 = MARGIN_X + end * CHAR_WIDTH
    y0 = MARGIN_Y + line.index * LINE_HEIGHT
    y1 = y0 + LINE_HEIGHT
    return Coordinate(page=line.page, bbox=(x0, y0, x1, y1))


def cell_bbox(line: TextLine, column: int) -> Coordinate:
    """按列序估算单元格包围盒（表格类格式专用，非真实版面坐标）。"""

    x0 = MARGIN_X + column * COLUMN_WIDTH
    x1 = x0 + COLUMN_WIDTH
    y0 = MARGIN_Y + line.index * LINE_HEIGHT
    y1 = y0 + LINE_HEIGHT
    return Coordinate(page=line.page, bbox=(x0, y0, x1, y1))
