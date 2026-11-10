"""XLSX 抽取器（仅标准库：``zipfile`` + ``xml.etree``）。

不引入 openpyxl：xlsx 本质是 zip 容器，本模块只读三样东西——
``xl/workbook.xml``（工作表顺序与名称）、``xl/_rels/workbook.xml.rels``（表名→文件）、
``xl/sharedStrings.xml`` 与 ``xl/worksheets/sheetN.xml``（单元格）。

两条避免「表格错位」的规则：

1. **按单元格引用还原列位**：``<c r="D7">`` 的列号来自引用本身，不从「有几个非空
   单元格」推断；空单元格保留为空串，后面列不会被顶上来。
2. **合并单元格只保留左上角值**：覆盖区域保持空列，不做跨列复制——
   复制会让同一个值被映射到多个字段（例如「物料名称」与「规格」同时被填成同一段文字），
   属于编造数据。区域内的空值由字段映射层记为「缺失」，交人工补录。

日期单元格按 ``xl/styles.xml`` 的数字格式识别（内置 14–22/45–47 或自定义格式含 y/d），
把 Excel 序列号还原为 ``YYYY-MM-DD``；识别不出格式时按原值输出，不猜测。
"""

from __future__ import annotations

import io
import zipfile
from datetime import date, timedelta
from xml.etree import ElementTree as ET

from ..detection import FileKind
from .base import DocumentText, TextLine

# Excel 1900 日期系统的序列号原点（含 1900-02-29 的兼容偏移）
_EPOCH_1900 = date(1899, 12, 30)
_EPOCH_1904 = date(1904, 1, 1)
_BUILTIN_DATE_FORMATS = frozenset(
    {14, 15, 16, 17, 18, 19, 20, 21, 22, 45, 46, 47}
)
_NS_REL = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"
_CELL_REF = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"


def _local(tag: str) -> str:
    """去掉 ``{namespace}`` 前缀，只留本地标签名。"""

    return tag.rsplit("}", 1)[-1]


def _children(element: ET.Element, name: str) -> list[ET.Element]:
    return [child for child in element if _local(child.tag) == name]


def _find(element: ET.Element, name: str) -> ET.Element | None:
    for child in element:
        if _local(child.tag) == name:
            return child
    return None


def column_index(reference: str) -> int:
    """``A1`` → 0，``AA10`` → 26。引用非法时返回 0。"""

    letters = "".join(char for char in reference if char.isalpha()).upper()
    if not letters:
        return 0
    index = 0
    for char in letters:
        index = index * 26 + (_CELL_REF.index(char) + 1 if char in _CELL_REF else 0)
    return index - 1


def _shared_strings(archive: zipfile.ZipFile) -> list[str]:
    try:
        raw = archive.read("xl/sharedStrings.xml")
    except KeyError:
        return []
    root = ET.fromstring(raw)
    values: list[str] = []
    for item in root:
        if _local(item.tag) != "si":
            continue
        # 富文本由多个 <r><t> 片段组成，按文档顺序拼接
        parts = [node.text or "" for node in item.iter() if _local(node.tag) == "t"]
        values.append("".join(parts))
    return values


def _style_is_date(archive: zipfile.ZipFile) -> list[bool]:
    """返回「第 i 个单元格样式是否为日期格式」的判定表。"""

    try:
        raw = archive.read("xl/styles.xml")
    except KeyError:
        return []
    root = ET.fromstring(raw)

    custom_formats: dict[int, str] = {}
    num_fmts = _find(root, "numFmts")
    if num_fmts is not None:
        for fmt in _children(num_fmts, "numFmt"):
            fmt_id = fmt.get("numFmtId")
            code = fmt.get("formatCode", "")
            if fmt_id is not None:
                custom_formats[int(fmt_id)] = code

    cell_xfs = _find(root, "cellXfs")
    if cell_xfs is None:
        return []

    result: list[bool] = []
    for xf in _children(cell_xfs, "xf"):
        num_fmt_id = int(xf.get("numFmtId", "0"))
        if num_fmt_id in _BUILTIN_DATE_FORMATS:
            result.append(True)
            continue
        code = custom_formats.get(num_fmt_id, "")
        # 引号内是字面量（如 "第"0"页"），先剔除再判断是否含年月日占位符
        stripped = code.split(";")[0]
        inside_quote = False
        meaningful = []
        for char in stripped:
            if char == '"':
                inside_quote = not inside_quote
                continue
            if not inside_quote and char not in "[]":
                meaningful.append(char.lower())
        result.append(any(char in "yd" for char in meaningful))
    return result


def _format_serial(serial: float, date1904: bool) -> str:
    epoch = _EPOCH_1904 if date1904 else _EPOCH_1900
    try:
        return (epoch + timedelta(days=int(serial))).isoformat()
    except (OverflowError, ValueError):
        return str(serial)


def _cell_text(
    cell: ET.Element,
    shared: list[str],
    date_styles: list[bool],
    date1904: bool,
) -> str:
    cell_type = cell.get("t", "n")
    style_index = int(cell.get("s", "0") or 0)

    if cell_type == "inlineStr":
        inline = _find(cell, "is")
        if inline is None:
            return ""
        return "".join(
            node.text or "" for node in inline.iter() if _local(node.tag) == "t"
        )

    value_node = _find(cell, "v")
    if value_node is None or value_node.text is None:
        return ""
    raw = value_node.text.strip()

    if cell_type == "s":
        try:
            return shared[int(raw)]
        except (ValueError, IndexError):
            return ""
    if cell_type == "b":
        return "TRUE" if raw == "1" else "FALSE"
    if cell_type in ("e", "str"):
        return raw
    if cell_type == "n" and _looks_like_serial(raw):
        is_date = (
            0 <= style_index < len(date_styles) and date_styles[style_index]
        )
        if is_date:
            return _format_serial(float(raw), date1904)
    return raw


def _looks_like_serial(raw: str) -> bool:
    try:
        float(raw)
    except ValueError:
        return False
    return True


def _sheet_targets(archive: zipfile.ZipFile) -> list[tuple[str, str]]:
    """返回 [(工作表名, zip 内路径)]，顺序与 workbook.xml 一致。"""

    try:
        workbook = ET.fromstring(archive.read("xl/workbook.xml"))
    except KeyError as error:
        raise ValueError("xlsx 缺少 xl/workbook.xml") from error

    rel_ids: dict[str, str] = {}
    try:
        rel_root = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
    except KeyError:
        rel_root = None
    if rel_root is not None:
        for rel in rel_root:
            if _local(rel.tag) != "Relationship":
                continue
            rel_id = rel.get("Id")
            target = rel.get("Target", "")
            if not rel_id or not target:
                continue
            if target.startswith("/"):
                path = target.lstrip("/")
            else:
                path = "xl/" + target.removeprefix("xl/")
            rel_ids[rel_id] = path.replace("\\", "/")

    sheets_node = _find(workbook, "sheets")
    targets: list[tuple[str, str]] = []
    if sheets_node is not None:
        for sheet in _children(sheets_node, "sheet"):
            name = sheet.get("name") or f"Sheet{len(targets) + 1}"
            rel_id = sheet.get(_NS_REL)
            path = rel_ids.get(rel_id or "")
            if path is None:
                path = f"xl/worksheets/sheet{len(targets) + 1}.xml"
            targets.append((name, path))
    if not targets:
        targets = [
            (name.rsplit("/", 1)[-1].removesuffix(".xml"), name)
            for name in sorted(archive.namelist())
            if name.startswith("xl/worksheets/") and name.endswith(".xml")
        ]
    return targets


def _sheet_rows(
    archive: zipfile.ZipFile,
    path: str,
) -> list[list[str]]:
    """读一个工作表，返回按行号排序的行（每行是保留列位的单元格元组）。"""

    try:
        root = ET.fromstring(archive.read(path))
    except KeyError as error:
        raise ValueError(f"xlsx 缺少工作表 {path}") from error

    # 元素顺序上 mergedCells 在 sheetData 之后，但合并区域只影响「哪些格子视为空」，
    # 这里先收集被覆盖的格子，再在展开行时跳过它们（左上角 anchor 保留值）。
    covered_cells: set[tuple[int, int]] = set()
    for merge in root.iter():
        if _local(merge.tag) != "mergeCell":
            continue
        ref = merge.get("ref", "")
        if ":" not in ref:
            continue
        start, end = ref.split(":", 1)
        start_col, start_row = _split_ref(start)
        end_col, end_row = _split_ref(end)
        for row in range(start_row, end_row + 1):
            for column in range(start_col, end_col + 1):
                if (row, column) == (start_row, start_col):
                    continue
                covered_cells.add((row, column))

    sheet_data = _find(root, "sheetData")
    if sheet_data is None:
        return []

    shared = _shared_strings(archive)
    date_styles = _style_is_date(archive)
    date1904 = _workbook_uses_1904(archive)

    rows: list[tuple[int, list[str]]] = []
    for row_node in _children(sheet_data, "row"):
        row_number = int(row_node.get("r", str(len(rows) + 1)) or len(rows) + 1)
        cells: dict[int, str] = {}
        for cell in _children(row_node, "c"):
            reference = cell.get("r", "")
            column = column_index(reference) if reference else len(cells)
            if (row_number, column) in covered_cells:
                continue
            cells[column] = _cell_text(cell, shared, date_styles, date1904)

        if not cells:
            rows.append((row_number, []))
            continue
        width = max(cells) + 1
        row = [cells.get(column, "") for column in range(width)]
        # 去掉行尾空列，但保留行内的空列（列位不能动）
        while row and not row[-1].strip():
            row.pop()
        rows.append((row_number, row))

    # 合并区域覆盖但没有锚点值的格子：保持空，不补值
    rows.sort(key=lambda item: item[0])
    return [row for _number, row in rows]


def _split_ref(reference: str) -> tuple[int, int]:
    letters = "".join(char for char in reference if char.isalpha())
    digits = "".join(char for char in reference if char.isdigit())
    return column_index(letters), int(digits or 1)


def _workbook_uses_1904(archive: zipfile.ZipFile) -> bool:
    try:
        workbook = ET.fromstring(archive.read("xl/workbook.xml"))
    except KeyError:
        return False
    properties = _find(workbook, "workbookPr")
    return properties is not None and properties.get("date1904") in ("1", "true")


class SpreadsheetExtractor:
    """XLSX 单元格抽取：一个工作表 = 一页，一行 = 一条 ``TextLine``（带 ``cells``）。"""

    name = "xlsx.stdlib.v1"

    def extract(self, source: bytes, *, filename: str = "") -> DocumentText:
        try:
            archive = zipfile.ZipFile(io.BytesIO(source))
        except zipfile.BadZipFile as error:
            raise ValueError(f"xlsx 文件已损坏：{filename!r}") from error

        with archive:
            pages: list[tuple[TextLine, ...]] = []
            for page_number, (_sheet_name, path) in enumerate(
                _sheet_targets(archive), start=1
            ):
                rows = _sheet_rows(archive, path)
                lines = tuple(
                    TextLine(
                        page=page_number,
                        index=index,
                        text=" | ".join(row),
                        cells=tuple(row) if row else None,
                    )
                    for index, row in enumerate(rows)
                )
                pages.append(lines)

        # 全空工作簿也要返回页数，交由管线判为「没有可识别文本」
        if not pages:
            pages = [()]
        return DocumentText(pages=tuple(pages), extractor=self.name, kind=FileKind.SPREADSHEET)
