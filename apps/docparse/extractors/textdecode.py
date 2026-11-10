"""字节 → 文本解码：BOM 优先、UTF-8 次之、GB18030 兜底。

不引入 chardet 之类依赖：企业内网单据的字符集基本落在 UTF-8 / GB18030 / 带 BOM 的
UTF-16 三类。GB18030 是 GBK 的超集且几乎不挑字节，因此必须放在 UTF-8 之后，
否则 UTF-8 中文会被误判。
"""

from __future__ import annotations

import codecs

_BOM_ENCODINGS: tuple[tuple[bytes, str], ...] = (
    (codecs.BOM_UTF8, "utf-8-sig"),
    (codecs.BOM_UTF32_LE, "utf-32"),
    (codecs.BOM_UTF32_BE, "utf-32"),
    (codecs.BOM_UTF16_LE, "utf-16"),
    (codecs.BOM_UTF16_BE, "utf-16"),
)

_FALLBACK_ENCODINGS = ("utf-8", "gb18030")


def decode_bytes(source: bytes, *, filename: str = "") -> str:
    """解码文本类文件；全部失败抛 ``ValueError``（由管线转为「文件损坏」）。"""

    for bom, encoding in _BOM_ENCODINGS:
        if source.startswith(bom):
            try:
                return source.decode(encoding)
            except UnicodeDecodeError:
                break

    for encoding in _FALLBACK_ENCODINGS:
        try:
            return source.decode(encoding)
        except UnicodeDecodeError:
            continue

    raise ValueError(f"无法解码文件 {filename!r}，仅支持 UTF-8 / GB18030 / UTF-16")
