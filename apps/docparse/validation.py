"""单据内部一致性校验（管线第 6 步）。

只做「文档自身数据是否自洽」的检查，不修改任何原值：
- 明细金额合计 vs 单据小计；
- 小计 + 税额 vs 价税合计。

不一致时输出 ERROR 问题，解析结果降为 ``needs_review``，由人工判断哪边是对的。
"""

from __future__ import annotations

from decimal import Decimal

from .models import DocHeader, DocLine, IssueSeverity, ValidationIssue

CENT = Decimal("0.01")


def validate_consistency(header: DocHeader, lines: list[DocLine]) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []

    line_amounts = [line.amount.value for line in lines]
    # 只有所有明细行都有金额时才可比，避免用部分金额误报
    if lines and header.subtotal.value is not None and all(
        amount is not None for amount in line_amounts
    ):
        expected = sum((amount for amount in line_amounts if amount is not None), Decimal("0"))
        actual = header.subtotal.value
        if abs(expected - actual) > CENT:
            issues.append(
                ValidationIssue(
                    code="SUBTOTAL_MISMATCH",
                    severity=IssueSeverity.ERROR,
                    message=f"明细金额合计 {expected} 与单据小计 {actual} 不一致，未自动修正",
                    field_path="header.subtotal",
                    expected=expected,
                    actual=actual,
                )
            )

    subtotal = header.subtotal.value
    tax = header.tax_amount.value
    total = header.total_amount.value
    if subtotal is not None and tax is not None and total is not None:
        expected_total = subtotal + tax
        if abs(expected_total - total) > CENT:
            issues.append(
                ValidationIssue(
                    code="TOTAL_MISMATCH",
                    severity=IssueSeverity.ERROR,
                    message=f"小计 + 税额 = {expected_total}，与价税合计 {total} 不一致，未自动修正",
                    field_path="header.total_amount",
                    expected=expected_total,
                    actual=total,
                )
            )

    return issues
