"""输出契约的机器可校验约束。"""

from __future__ import annotations

import json
from decimal import Decimal

import pytest
from pydantic import ValidationError

from apps.docparse.models import (
    Coordinate,
    DecimalField,
    DocHeader,
    ParseResult,
    ParseStatus,
    DocType,
)


def test_missing_field_must_have_zero_confidence():
    with pytest.raises(ValidationError):
        DecimalField(value=None, confidence=0.9)


def test_present_field_must_have_positive_confidence():
    with pytest.raises(ValidationError):
        DecimalField(value=Decimal("1"), confidence=0.0)


def test_missing_is_none_not_zero():
    missing = DecimalField(value=None, confidence=0.0)
    assert missing.is_missing
    assert missing.value is None

    # 0 是合法值，与「缺失」不同
    zero = DecimalField(value=Decimal("0"), confidence=0.9)
    assert not zero.is_missing
    assert zero.value == Decimal("0")


def test_low_confidence_threshold():
    assert DecimalField(value=Decimal("1"), confidence=0.84).is_low_confidence()
    assert not DecimalField(value=Decimal("1"), confidence=0.85).is_low_confidence()


@pytest.mark.parametrize(
    "bbox",
    [(10.0, 10.0, 5.0, 20.0), (10.0, 10.0, 20.0, 5.0), (-1.0, 10.0, 20.0, 20.0)],
)
def test_coordinate_rejects_invalid_bbox(bbox):
    with pytest.raises(ValidationError):
        Coordinate(page=1, bbox=bbox)


def test_coordinate_rejects_page_zero():
    with pytest.raises(ValidationError):
        Coordinate(page=0, bbox=(0.0, 0.0, 10.0, 10.0))


def test_header_defaults_are_missing():
    header = DocHeader()
    assert header.doc_no.is_missing
    assert header.total_amount.value is None
    assert header.total_amount.raw_text is None


def test_parse_result_json_roundtrip(pipeline, fixtures_dir):
    result = pipeline.parse_file(fixtures_dir / "clear_invoice.txt")
    payload = result.model_dump_json()
    restored = ParseResult.model_validate_json(payload)

    assert restored == result
    assert restored.status is ParseStatus.OK
    assert restored.doc_type is DocType.INVOICE
    # 缺失字段在 JSON 里是 null，而不是 0
    assert json.loads(payload)["header"]["tax_rate"]["value"] is None
