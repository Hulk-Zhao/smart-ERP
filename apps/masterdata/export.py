"""差异清单 CSV 导出。

使用标准库 ``csv``，UTF-8 BOM 编码，保证中文在 Excel 直接双击打开不乱码。
"""

from __future__ import annotations

import csv
import io
from pathlib import Path

from .models import InspectionReport

# (字段名, CSV 表头)；表头用中文，方便业务直接看
CSV_COLUMNS: tuple[tuple[str, str], ...] = (
    ("rule_id", "规则ID"),
    ("rule_name", "规则名称"),
    ("category", "规则类别"),
    ("severity", "严重度"),
    ("object_type", "对象类型"),
    ("object_code", "对象编码"),
    ("object_name", "对象名称"),
    ("field", "字段"),
    ("actual", "原值"),
    ("expected", "期望值"),
    ("source", "证据来源"),
    ("suggestion", "建议动作"),
    ("why", "为什么值得修"),
    ("false_positive", "误报处理"),
    ("supplier_code", "供应商"),
    ("item_code", "物料"),
    ("period", "期间"),
)


def _join(findings: list[str]) -> str:
    return " | ".join(value for value in findings if value)


def _row(report: InspectionReport, index: int) -> dict[str, str]:
    finding = report.findings[index]
    return {
        "rule_id": finding.rule_id,
        "rule_name": finding.rule_name,
        "category": finding.category,
        "severity": finding.severity.value,
        "object_type": finding.object_type.value,
        "object_code": finding.object_code,
        "object_name": finding.object_name,
        "field": _join([item.field for item in finding.evidence]),
        "actual": _join([item.actual for item in finding.evidence]),
        "expected": _join([item.expected for item in finding.evidence]),
        "source": _join([item.source for item in finding.evidence]),
        "suggestion": finding.suggestion,
        "why": finding.why,
        "false_positive": finding.false_positive,
        "supplier_code": finding.supplier_code or "",
        "item_code": finding.item_code or "",
        "period": finding.period or "",
    }


def render_csv(report: InspectionReport) -> str:
    """返回 CSV 文本（不含 BOM；写文件时由 ``write_csv`` 加 BOM）。"""
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer)
    writer.writerow([label for _, label in CSV_COLUMNS])
    for index in range(len(report.findings)):
        row = _row(report, index)
        writer.writerow([row[key] for key, _ in CSV_COLUMNS])
    return buffer.getvalue()


def write_csv(report: InspectionReport, path: str | Path) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8-sig", newline="") as handle:
        handle.write(render_csv(report))
    return target
