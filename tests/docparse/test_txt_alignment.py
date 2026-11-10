"""TXT 抽取：多栏对齐、千分位、全角数字、正负号与括号负数。"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from apps.docparse.mapping.fields import (
    parse_date,
    parse_decimal,
    parse_decimal_detailed,
    parse_rate,
    parse_rate_detailed,
    split_tokens,
)
from apps.docparse.models import DocType, ParseStatus


def test_aligned_columns_are_split_on_padded_spaces(pipeline, fixtures_dir):
    result = pipeline.parse_file(fixtures_dir / "aligned_statement.txt")

    assert result.doc_type is DocType.STATEMENT
    assert result.status is ParseStatus.OK
    assert result.header.doc_date.value == date(2026, 3, 1)
    assert result.header.total_amount.value == Decimal("1299.50")

    assert len(result.lines) == 2
    first = result.lines[0]
    assert first.item_code.value == "WL-1001"
    # 单元格内部的单个空格不能被当成分隔符，否则名称会被截断成两列
    assert first.item_name.value == "不锈钢 螺钉M6"
    assert first.quantity.value == Decimal(1000)
    assert first.amount.value == Decimal("850.00")
    assert first.tax_rate.value == Decimal("0.1300")


def test_split_tokens_priority():
    aligned = split_tokens("1    不锈钢 螺钉M6    1,000")
    assert [token.value for token in aligned] == ["1", "不锈钢 螺钉M6", "1,000"]

    piped = split_tokens("1 | 垫片 | 20")
    assert [token.value for token in piped] == ["1", "垫片", "20"]

    comma = split_tokens("序号,物料名称,数量")
    assert [token.value for token in comma] == ["序号", "物料名称", "数量"]


@pytest.mark.parametrize(
    ("raw", "value", "strict"),
    [
        ("1000", Decimal(1000), True),
        ("1,000.50", Decimal("1000.50"), True),
        ("¥1,299.50", Decimal("1299.50"), True),
        ("(1,234.50)", Decimal("-1234.50"), False),
        ("123.00-", Decimal("-123.00"), False),
        ("１２３．４５", Decimal("123.45"), False),
        ("−88.00", Decimal("-88.00"), False),
        ("1,23", Decimal(123), False),
        ("", None, False),
        ("待定", None, False),
    ],
)
def test_parse_decimal_normalizes_dirty_forms(raw, value, strict):
    parsed = parse_decimal_detailed(raw)

    assert parsed.value == value
    assert parsed.strict is strict
    assert parse_decimal(raw) == value


@pytest.mark.parametrize(
    ("raw", "value", "strict"),
    [
        ("13%", Decimal("0.1300"), True),
        ("6％", Decimal("0.0600"), False),
        ("13", Decimal("0.1300"), False),
        ("0.13", Decimal("0.13"), True),
        ("—", None, False),
    ],
)
def test_parse_rate(raw, value, strict):
    parsed = parse_rate_detailed(raw)

    assert parsed.value == value
    assert parsed.strict is strict
    assert parse_rate(raw) == value


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2026-03-05", date(2026, 3, 5)),
        ("2026/3/5", date(2026, 3, 5)),
        ("2026年3月5日", date(2026, 3, 5)),
        ("2026-02-30", None),
        ("2026年", None),
    ],
)
def test_parse_date(raw, expected):
    assert parse_date(raw) == expected
