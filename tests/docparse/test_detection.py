"""文件类型识别：魔数优先、扩展名兜底、白名单外返回 unsupported。"""

from __future__ import annotations

import pytest

from apps.docparse.detection import FileKind, detect_file_kind, is_page_marker, page_marker_number


@pytest.mark.parametrize(
    ("filename", "head", "expected"),
    [
        ("a.txt", b"hello", FileKind.TEXT),
        ("a.md", b"# title", FileKind.TEXT),
        ("a.csv", b"a,b\n1,2", FileKind.CSV),
        ("a.tsv", b"a\tb", FileKind.CSV),
        ("a.pdf", b"%PDF-1.7\n", FileKind.PDF),
        ("a.png", b"\x89PNG\r\n\x1a\n", FileKind.IMAGE),
        ("a.jpg", b"\xff\xd8\xff\xe0", FileKind.IMAGE),
        ("a.xlsx", b"PK\x03\x04rest", FileKind.SPREADSHEET),
        # 扩展名是 txt，但内容是 PDF：魔数优先
        ("fake.txt", b"%PDF-1.4\n", FileKind.PDF),
        ("a.exe", b"MZ\x90\x00", FileKind.UNSUPPORTED),
        ("a.docx", b"PK\x03\x04rest", FileKind.UNSUPPORTED),
    ],
)
def test_detect_file_kind(filename, head, expected):
    assert detect_file_kind(filename=filename, head=head) is expected


def test_page_marker():
    assert page_marker_number("第 2 页 / 共 5 页") == 2
    assert page_marker_number("第 1 页") == 1
    assert is_page_marker("第 3 页 共 3 页") is False  # 缺分隔符，不按页码处理
    assert page_marker_number("序号 | 数量") is None
