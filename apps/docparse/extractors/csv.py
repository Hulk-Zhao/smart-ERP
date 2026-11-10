"""CSV / TSV 抽取器（标准库 ``csv`` + 自定义分隔符与编码探测）。

与纯文本抽取器的区别：

- 引号内的逗号、换行不会被当成分隔符或换行（``csv.reader`` 负责）；
- 空单元格保留为空串，列位不因空值塌陷（``TextLine.cells``）；
- 自动识别 BOM、字符集与分隔符（``,`` / ``;`` / 制表符 / ``|``）。

分隔符判定按「出现在多少行」优先，再看总出现次数：只按总次数会被引号内的大段逗号
带偏，按行数更稳定；判定前先把引号内的字符排除掉。
"""

from __future__ import annotations

import csv
import io

from ..detection import FileKind
from .base import DocumentText, TextLine
from .textdecode import decode_bytes

_CANDIDATE_DELIMITERS = (",", ";", "\t", "|")
_SAMPLE_LINES = 20


def _split_outside_quotes(line: str, delimiter: str) -> int:
    count = 0
    in_quote = False
    for char in line:
        if char == '"':
            in_quote = not in_quote
        elif char == delimiter and not in_quote:
            count += 1
    return count


def detect_delimiter(text: str) -> str:
    """返回最可能的分隔符；无任何候选命中时按 CSV 默认值 ``，`` 处理。"""

    sample = [line for line in text.splitlines()[:_SAMPLE_LINES] if line.strip()]
    if not sample:
        return ","

    scored: list[tuple[int, int, str]] = []
    for delimiter in _CANDIDATE_DELIMITERS:
        per_line = [_split_outside_quotes(line, delimiter) for line in sample]
        rows_hit = sum(1 for count in per_line if count)
        total = sum(per_line)
        scored.append((rows_hit, total, delimiter))

    best_rows, best_total, best = max(scored)
    if best_rows < 2 or best_total == 0:
        # 单列文件（例如「供应商对账单」标题行）也可能夹带个别逗号，
        # 不足两行命中就不当作表格，交给默认分隔符
        return ","
    return best


class CsvExtractor:
    """CSV/TSV → 每行一条带 ``cells`` 的 ``TextLine``，单页。"""

    name = "csv.stdlib.v1"

    def extract(self, source: bytes, *, filename: str = "") -> DocumentText:
        text = decode_bytes(source, filename=filename).replace("\r\n", "\n").replace("\r", "\n")
        delimiter = detect_delimiter(text)

        reader = csv.reader(io.StringIO(text), delimiter=delimiter, quotechar='"')
        lines: list[TextLine] = []
        for index, row in enumerate(reader):
            cells = tuple(cell.strip() for cell in row)
            lines.append(
                TextLine(
                    page=1,
                    index=index,
                    text=" | ".join(cells).strip(),
                    cells=cells,
                )
            )

        return DocumentText(
            pages=(tuple(lines),), extractor=self.name, kind=FileKind.CSV
        )
