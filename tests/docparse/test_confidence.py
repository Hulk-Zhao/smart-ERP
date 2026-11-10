"""置信度校准：打分依据可解释，且「脏输入 → 低置信 → 不进自动流程」确实生效。"""

from __future__ import annotations

from decimal import Decimal

import pytest

from apps.docparse.mapping.fields import (
    CONF_CELL_LENIENT,
    CONF_CELL_STRICT,
    CONF_MISALIGNED_SCALE,
    SCORING_RUBRIC,
)
from apps.docparse.models import LOW_CONFIDENCE_THRESHOLD, ParseStatus
from apps.docparse.scoring import CONFIDENCE_BOOST


def test_rubric_covers_three_bases_and_orders_confidence_levels():
    bases = {name for name, _value, _reason in SCORING_RUBRIC}

    # 字段来源（表头位置）、正则命中强度（数字写法）、上下文（算术/错位）三类依据都在
    assert {
        "header.label_at_line_start",
        "header.label_embedded",
        "cell.strict_number",
        "cell.lenient_number",
        "cell.misaligned",
        "cell.derived_amount",
    } == bases
    assert CONF_CELL_LENIENT < LOW_CONFIDENCE_THRESHOLD < CONF_CELL_STRICT
    assert CONF_MISALIGNED_SCALE < 1.0


def test_clean_document_reaches_ok_without_low_confidence(pipeline, fixtures_dir):
    result = pipeline.parse_file(fixtures_dir / "aligned_statement.txt")

    assert result.status is ParseStatus.OK
    assert result.low_confidence_fields == []
    # 行内算术自洽（数量×单价=金额）会给 0.92 再 +0.02
    assert result.lines[0].quantity.confidence > CONF_CELL_STRICT


def test_fullwidth_number_is_low_confidence_but_value_kept(pipeline, fixtures_dir):
    result = pipeline.parse_file(fixtures_dir / "dirty_fullwidth_invoice.txt")

    quantity = result.lines[0].quantity
    assert quantity.value == Decimal(1000)  # 值仍按归一化读出，不丢数据
    assert quantity.confidence < LOW_CONFIDENCE_THRESHOLD
    assert "lines[1].quantity" in result.low_confidence_fields
    assert result.status is ParseStatus.NEEDS_REVIEW


def test_misaligned_row_is_flagged_and_damped(pipeline, fixtures_dir):
    result = pipeline.parse_file(fixtures_dir / "misaligned_rows.txt")

    assert result.status is ParseStatus.NEEDS_REVIEW
    codes = [issue.code for issue in result.issues]
    assert "ROW_TOKENS_MISALIGNED" in codes

    line = result.lines[0]
    for name in ("item_name", "spec", "unit", "quantity", "unit_price"):
        assert line.field(name).confidence < LOW_CONFIDENCE_THRESHOLD
    # 错位打折 0.92×0.8=0.736，行内算术自洽再 +0.02，仍低于阈值
    assert line.amount.confidence == pytest.approx(
        CONF_CELL_STRICT * CONF_MISALIGNED_SCALE + CONFIDENCE_BOOST
    )
    assert line.amount.confidence < LOW_CONFIDENCE_THRESHOLD


def test_amount_mismatch_forces_review_even_when_fields_are_clean(pipeline):
    payload = (
        "增值税专用发票\n"
        "发票号码: 88231188\n"
        "开票日期: 2026-03-20\n"
        "销售方名称: 测试供应商\n"
        "小计: 50.00\n"
        "价税合计: 56.50\n"
        "序号 | 物料名称 | 数量 | 单价 | 金额 | 税率\n"
        "1 | 垫片 | 100 | 0.10 | 50.00 | 13%\n"
    ).encode()

    result = pipeline.parse_bytes(payload, filename="line_mismatch.txt")

    assert result.status is ParseStatus.NEEDS_REVIEW
    assert any(issue.code == "LINE_AMOUNT_MISMATCH" for issue in result.issues)
    assert result.lines[0].amount.confidence < LOW_CONFIDENCE_THRESHOLD


def test_header_total_mismatch_damps_total_confidence(pipeline):
    payload = (
        "增值税专用发票\n"
        "发票号码: 88230001\n"
        "开票日期: 2026-03-05\n"
        "销售方名称: 测试供应商\n"
        "小计: 100.00\n"
        "税额合计: 13.00\n"
        "价税合计: 200.00\n"
        "序号 | 物料名称 | 数量 | 单价 | 金额 | 税率\n"
        "1 | 垫片 | 100 | 1.00 | 100.00 | 13%\n"
    ).encode()

    result = pipeline.parse_bytes(payload, filename="bad_total.txt")

    assert result.status is ParseStatus.NEEDS_REVIEW
    assert result.header.total_amount.confidence < LOW_CONFIDENCE_THRESHOLD
    assert any(issue.code == "TOTAL_MISMATCH" for issue in result.issues)
    # 错在「合计」这一项，明细金额不该被连坐降置信
    assert result.lines[0].amount.confidence > LOW_CONFIDENCE_THRESHOLD
