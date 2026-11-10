"""PDF 文本层抽取（pypdfium2，可选 ``[pdf]`` extra）。

- 依赖延迟导入：核心安装不含 PDF 依赖，未装 extra 时抛
  ``PdfSupportUnavailableError``，由管线降级并提示安装方式，绝不返回空文本冒充成功。
- 逐字符取 ``get_charbox``，把 PDF 左下原点坐标换算成契约要求的左上原点，
  写入 ``TextLine.char_boxes``，字段坐标由此得到真实字符框（不是按字符宽度估算）。
- 无文本层（通常是扫描件）抛 ``PdfTextLayerMissingError``，明确报出「需 OCR」。
"""

from __future__ import annotations

from typing import Any

from ..detection import FileKind
from ..errors import PdfSupportUnavailableError, PdfTextLayerMissingError
from .base import DocumentText, TextLine

# 行尾空白裁剪与换行处理：PDFium 的文本范围用 \r\n 分隔文本行
_NEWLINE_CHARS = frozenset("\r\n")


def _import_pdfium() -> Any:
    """延迟导入可选依赖；未安装时给出可执行的安装指引。"""

    try:
        import pypdfium2
    except ImportError as exc:
        raise PdfSupportUnavailableError() from exc
    return pypdfium2


def _flush_line(
    chars: list[str],
    boxes: list[tuple[float, float, float, float]],
    page: int,
    index: int,
) -> TextLine | None:
    """把一段字符收成一行：裁掉首尾空白，并同步裁剪字符框。"""

    if not chars:
        return None
    raw = "".join(chars)
    lead = len(raw) - len(raw.lstrip())
    text = raw.strip()
    if not text:
        return None
    kept = boxes[lead : lead + len(text)]
    return TextLine(page=page, index=index, text=text, char_boxes=tuple(kept))


def _page_lines(textpage: Any, page: int, page_height: float) -> list[TextLine]:
    lines: list[TextLine] = []
    chars: list[str] = []
    boxes: list[tuple[float, float, float, float]] = []

    for offset in range(textpage.count_chars()):
        char = textpage.get_text_range(offset, 1)
        if char in _NEWLINE_CHARS:
            line = _flush_line(chars, boxes, page, len(lines))
            if line is not None:
                lines.append(line)
            chars = []
            boxes = []
            continue
        # PDFium: (left, bottom, right, top)，原点左下；契约要求原点左上
        left, bottom, right, top = textpage.get_charbox(offset, loose=False)
        chars.append(char)
        boxes.append((left, page_height - top, right, page_height - bottom))

    line = _flush_line(chars, boxes, page, len(lines))
    if line is not None:
        lines.append(line)
    return lines


class PdfTextExtractor:
    """真实 PDF 文本层抽取器（pypdfium2 / PDFium）。"""

    name = "pdf.pypdfium2.v1"

    def extract(self, source: bytes, *, filename: str = "") -> DocumentText:
        pdfium = _import_pdfium()
        try:
            document = pdfium.PdfDocument(source)
        except Exception as exc:  # PDFium 对损坏/加密文件抛 PdfiumError
            raise ValueError(f"PDF 无法打开（损坏或加密）：{exc}") from exc

        pages: list[tuple[TextLine, ...]] = []
        text_chars = 0
        try:
            for page_number, page in enumerate(document, start=1):
                page_height = page.get_size()[1]
                textpage = page.get_textpage()
                try:
                    lines = tuple(_page_lines(textpage, page_number, page_height))
                finally:
                    textpage.close()
                pages.append(lines)
                text_chars += sum(
                    1 for line in lines for ch in line.text if not ch.isspace()
                )
        finally:
            document.close()

        if text_chars == 0:
            raise PdfTextLayerMissingError(len(pages))

        return DocumentText(
            pages=tuple(pages), extractor=self.name, kind=FileKind.PDF
        )
