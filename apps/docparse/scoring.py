"""置信度打分与复核标记（管线第 5 步）。

映射阶段给出的基础置信度（标签命中 0.98 / 内嵌标签 0.90 / 表格单元 0.92）
在这里用「行内算术自洽」做校正：
- 数量 × 单价 ≈ 金额 → 三者各 +0.02；
- 不相等 → 金额置信度压到 0.70，使其进入低置信清单，且由校验阶段报错。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from .models import (
    CRITICAL_HEADER_FIELDS,
    CRITICAL_LINE_FIELDS,
    HEADER_FIELD_LABELS,
    LINE_FIELD_LABELS,
    DocHeader,
    DocLine,
    DocType,
    FieldValue,
    IssueSeverity,
    MissingField,
    ValidationIssue,
)

CENT = Decimal("0.01")
CONFIDENCE_BOOST = 0.02
CONFIDENCE_CAP = 0.99
MISMATCH_CONFIDENCE = 0.70


@dataclass(frozen=True)
class ScoredLines:
    lines: tuple[DocLine, ...]
    issues: tuple[ValidationIssue, ...]


def _with_confidence(field: FieldValue, confidence: float) -> FieldValue:
    return field.model_copy(update={"confidence": confidence})


def score_lines(lines: list[DocLine]) -> ScoredLines:
    scored: list[DocLine] = []
    issues: list[ValidationIssue] = []

    for index, line in enumerate(lines, start=1):
        quantity, unit_price, amount = line.quantity, line.unit_price, line.amount
        if (
            quantity.value is not None
            and unit_price.value is not None
            and amount.value is not None
        ):
            expected = (quantity.value * unit_price.value).quantize(CENT)
            if abs(expected - amount.value) <= CENT:
                line = line.model_copy(
                    update={
                        "quantity": _with_confidence(
                            quantity, min(CONFIDENCE_CAP, quantity.confidence + CONFIDENCE_BOOST)
                        ),
                        "unit_price": _with_confidence(
                            unit_price,
                            min(CONFIDENCE_CAP, unit_price.confidence + CONFIDENCE_BOOST),
                        ),
                        "amount": _with_confidence(
                            amount, min(CONFIDENCE_CAP, amount.confidence + CONFIDENCE_BOOST)
                        ),
                    }
                )
            else:
                line = line.model_copy(
                    update={"amount": _with_confidence(amount, MISMATCH_CONFIDENCE)}
                )
                issues.append(
                    ValidationIssue(
                        code="LINE_AMOUNT_MISMATCH",
                        severity=IssueSeverity.ERROR,
                        message=(
                            f"明细第 {line.line_no or index} 行：数量×单价={expected}，"
                            f"与单据金额 {amount.value} 不一致"
                        ),
                        field_path=f"lines[{index}].amount",
                        expected=expected,
                        actual=amount.value,
                    )
                )
        scored.append(line)

    return ScoredLines(lines=tuple(scored), issues=tuple(issues))


def collect_low_confidence(
    header: DocHeader, lines: list[DocLine]
) -> list[str]:
    """返回置信度低于阈值的字段路径（FR-401：必须人工确认）。"""

    paths: list[str] = []
    for name in HEADER_FIELD_LABELS:
        field = header.field(name)
        if field.is_low_confidence():
            paths.append(f"header.{name}")
    for index, line in enumerate(lines, start=1):
        for name in LINE_FIELD_LABELS:
            if line.field(name).is_low_confidence():
                paths.append(f"lines[{index}].{name}")
    return paths


def collect_missing_fields(
    header: DocHeader, lines: list[DocLine], doc_type: DocType
) -> list[MissingField]:
    """只列出关键缺失字段（补录表单与 FR-404 队列需要的最小集合）。"""

    critical_header = set(CRITICAL_HEADER_FIELDS.get(doc_type, ()))
    missing: list[MissingField] = []

    for name in HEADER_FIELD_LABELS:
        if header.field(name).is_missing and name in critical_header:
            missing.append(
                MissingField(
                    path=f"header.{name}",
                    label=HEADER_FIELD_LABELS[name],
                    critical=True,
                )
            )

    for index, line in enumerate(lines, start=1):
        for name in CRITICAL_LINE_FIELDS:
            if line.field(name).is_missing:
                missing.append(
                    MissingField(
                        path=f"lines[{index}].{name}",
                        label=LINE_FIELD_LABELS[name],
                        critical=False,
                        reason=f"第 {index} 行缺少{LINE_FIELD_LABELS[name]}，该行需补录",
                    )
                )
    return missing
