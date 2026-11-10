"""归一化工具：只做「同义写法归一」，不做业务猜测。

重点处理真实脏数据里最常见的三种差异：
- 多一个空格、全角空格；
- 全角括号 / 全角标点（NFKC 先转半角，再去掉分隔符）；
- 税号/银行账号里混入空格、横线。
"""

from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher

# 名称归一：去掉空白、括号、常见中英文标点与连接符。
# 不去掉「有限公司」等主体词，避免把不同法律主体误合并。
_NAME_NOISE = re.compile(
    r"[\s()\[\]{}<>《》【】\-_/\\.,，。·、:：;；'\"“”‘’]+"
)
_TAX_NOISE = re.compile(r"[^0-9A-Z]")
_NON_DIGIT = re.compile(r"\D")
_CODE_FOLD = str.maketrans({"O": "0", "o": "0", "I": "1", "i": "1"})


def normalize_text(value: str | None) -> str:
    """NFKC + 去首尾空白；用于分类等需要保留语义的字段。"""
    return unicodedata.normalize("NFKC", value or "").strip()


def normalize_name(value: str | None) -> str:
    """供应商/物料名称归一：全半角统一、忽略大小写、空格与括号标点。"""
    text = unicodedata.normalize("NFKC", value or "").casefold()
    return _NAME_NOISE.sub("", text)


def normalize_tax_no(value: str | None) -> str:
    """税号归一：只保留字母数字并大写；空值返回空串。"""
    text = unicodedata.normalize("NFKC", value or "").upper()
    return _TAX_NOISE.sub("", text)


def normalize_bank_account(value: str | None) -> str:
    """银行账号归一：只保留数字；空值返回空串。"""
    return _NON_DIGIT.sub("", value or "")


def normalize_unit(value: str | None) -> str:
    """单位归一：只去空白与大小写差异，不把「箱/件」当等价。"""
    return normalize_text(value).upper()


def normalize_currency(value: str | None) -> str:
    """币种归一：去空白并大写，不做币种映射。"""
    return normalize_text(value).upper()


def normalize_tax_code(value: str | None) -> str:
    """税码归一：去空白并大写。"""
    return normalize_text(value).upper()


def normalize_payment_term(value: str | None) -> str:
    """付款条件归一：去空白、横线与斜杠并大写（NET 30 / net-30 视为同一口径）。"""
    text = normalize_text(value).upper()
    return re.sub(r"[\s\-_/]+", "", text)


def fold_code(value: str | None) -> str:
    """把形近字符折叠成同一形态，用于发现「0/O、1/I」撞车。

    只折叠 O/o→0、I/i→1，不改大小写、不删分隔符，尽量少制造误报。
    """
    return normalize_text(value).translate(_CODE_FOLD)


def name_similarity(left: str, right: str) -> float:
    """归一后名称的相似度（0–1），仅用于提示级重复判断。"""
    a = normalize_name(left)
    b = normalize_name(right)
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()
