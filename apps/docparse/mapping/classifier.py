"""单据分类（管线第 3 步）：规则版，后续可替换为版面/模型分类器。

输出置信度 < 0.8 时返回 ``DocType.UNKNOWN``，由上层要求人工选类型（FR-401）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from ..extractors.base import DocumentText
from ..models import CLASSIFICATION_THRESHOLD, DocType

# 标题词：命中即高置信（文档第一屏的显式类型名）
_TITLE_RULES: tuple[tuple[DocType, tuple[str, ...]], ...] = (
    (DocType.INVOICE, ("增值税专用发票", "增值税普通发票", "增值税电子普通发票", "机动车销售统一发票")),
    (DocType.DELIVERY_NOTE, ("送货单", "送货清单")),
    (DocType.STATEMENT, ("对账单", "对帐单", "供应商对账")),
    (DocType.PICKING_SLIP, ("领料单", "领料出库单")),
)

# 关键词：标题不明显时按命中数打分
_KEYWORD_RULES: dict[DocType, tuple[str, ...]] = {
    DocType.INVOICE: ("发票代码", "发票号码", "价税合计", "开票日期", "税率", "销售方", "购买方"),
    DocType.DELIVERY_NOTE: ("送货单号", "收货单位", "签收", "送货日期", "送货数量"),
    DocType.STATEMENT: ("对账期间", "期初余额", "期末余额", "对账日期", "对账"),
    DocType.PICKING_SLIP: ("领料部门", "领料人", "领用", "领料"),
}

# 标题词命中但类型仅是泛称时的置信度
_GENERIC_TITLE_CONFIDENCE = 0.90
_TITLE_CONFIDENCE = 0.97


@dataclass(frozen=True)
class Classification:
    doc_type: DocType
    confidence: float


class DocClassifier(Protocol):
    name: str

    def classify(self, text: DocumentText) -> Classification:
        ...


class RuleBasedClassifier:
    """标题优先、关键词兜底；类型接近时判为未知，避免猜错单据类型。"""

    name = "rule-classifier.v1"

    def classify(self, text: DocumentText) -> Classification:
        lines = [line.text.strip() for line in text.iter_lines() if line.text.strip()]
        head = lines[:8]

        # 1) 标题词
        title_hits = [
            (doc_type, keyword, 0.0)
            for doc_type, keywords in _TITLE_RULES
            for keyword in keywords
            if any(keyword in line for line in head)
        ]
        if title_hits:
            doc_type, keyword, _ = max(title_hits, key=lambda item: len(item[1]))
            # 泛称「发票」不作为标题词，避免把「发票号码」误当标题
            return Classification(doc_type=doc_type, confidence=_TITLE_CONFIDENCE)

        # 2) 关键词命中数
        body = "\n".join(lines)
        scores: list[tuple[DocType, float]] = []
        for doc_type, keywords in _KEYWORD_RULES.items():
            hits = sum(1 for keyword in keywords if keyword in body)
            if hits:
                scores.append((doc_type, min(0.90, 0.50 + 0.08 * hits)))
        if not scores:
            return Classification(doc_type=DocType.UNKNOWN, confidence=0.0)

        scores.sort(key=lambda item: item[1], reverse=True)
        best_type, best_score = scores[0]
        if len(scores) > 1 and best_score - scores[1][1] < 0.10:
            # 两类关键词接近 → 不自动选，交人工
            return Classification(doc_type=DocType.UNKNOWN, confidence=best_score)
        if best_score < CLASSIFICATION_THRESHOLD:
            return Classification(doc_type=DocType.UNKNOWN, confidence=best_score)
        return Classification(doc_type=best_type, confidence=best_score)
