"""仓库文件内容摘要：先归一 EOL，再取 sha256。

仓库统一 LF（``.gitattributes`` 的 ``* text=auto eol=lf``），但 Windows 工作树里
由脚本直接写出的文件仍可能是 CRLF。若直接对 ``read_bytes()`` 取摘要，同一提交
在不同平台会得到不同 sha256（契约 sha 已踩过一次，金标准基线是第二次）。

凡对仓库文件取内容摘要，一律走这里，不要各自 ``hashlib.sha256(path.read_bytes())``。
"""

from __future__ import annotations

import hashlib
from pathlib import Path


def normalize_eol(data: bytes) -> bytes:
    """CRLF → LF；已是 LF 的原样返回。"""
    return data.replace(b"\r\n", b"\n")


def sha256_file(path: Path) -> str:
    """文件内容的 sha256，按 LF 归一后计算，跨平台一致。"""
    return hashlib.sha256(normalize_eol(path.read_bytes())).hexdigest()
