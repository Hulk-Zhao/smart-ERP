"""金额与小计不一致：保留原值 + 报校验错误 + 降级为需人工确认。"""

from __future__ import annotations

from decimal import Decimal

from apps.docparse import DocType, IssueSeverity, ParseStatus


def test_subtotal_mismatch_needs_review(pipeline, fixtures_dir):
    result = pipeline.parse_file(fixtures_dir / "amount_mismatch_invoice.txt")

    assert result.doc_type is DocType.INVOICE
    assert result.status is ParseStatus.NEEDS_REVIEW
    assert result.message and "需人工确认" in result.message

    issues = {issue.code: issue for issue in result.issues}
    assert "SUBTOTAL_MISMATCH" in issues
    issue = issues["SUBTOTAL_MISMATCH"]
    assert issue.severity is IssueSeverity.ERROR
    assert issue.expected == Decimal("3030.00")  # 明细行金额合计
    assert issue.actual == Decimal("3000.00")  # 单据小计

    # 原值不被自动修正，交人工判断
    assert result.header.subtotal.value == Decimal("3000.00")
    assert result.header.total_amount.value == Decimal("3390.00")


def test_amount_mismatch_keeps_line_values(pipeline, fixtures_dir):
    result = pipeline.parse_file(fixtures_dir / "amount_mismatch_invoice.txt")

    assert [line.amount.value for line in result.lines] == [
        Decimal("120.00"),
        Decimal("550.00"),
        Decimal("2360.00"),
    ]
    assert all(line.amount.source.value == "rule" for line in result.lines)
    assert not any(issue.code == "LINE_AMOUNT_MISMATCH" for issue in result.issues)
