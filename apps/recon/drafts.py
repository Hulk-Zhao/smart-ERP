"""差异单草稿生成（FR-106）：按「供应商 × 差异类型」聚合，仅产出草稿，不写 ERP。

G-01：草稿只是 payload，写入前必须走 preview；本模块不做任何写动作。
"""

from __future__ import annotations

from .models import (
    DiffItem,
    DiffType,
    DiscrepancyDraft,
    Money,
    ReconRequest,
)


def build_discrepancy_drafts(
    request: ReconRequest, diffs: list[DiffItem] | tuple[DiffItem, ...]
) -> tuple[DiscrepancyDraft, ...]:
    order: list[tuple[str, str]] = []
    grouped: dict[tuple[str, str], list[DiffItem]] = {}
    for diff in diffs:
        # 舍入容差是已放行的容差调整，不对外发差异单（否则会向供应商重复要钱）
        if diff.type is DiffType.ROUNDING_TOLERANCE:
            continue
        key = (diff.supplier_code or "UNASSIGNED", diff.type.value)
        if key not in grouped:
            grouped[key] = []
            order.append(key)
        grouped[key].append(diff)

    drafts: list[DiscrepancyDraft] = []
    for index, key in enumerate(order, start=1):
        items = grouped[key]
        total: Money = items[0].amount
        for item in items[1:]:
            total = total + item.amount

        evidence = []
        seen: set[tuple[str, str, str]] = set()
        for item in items:
            for entry in item.evidence:
                marker = (entry.system, entry.doc_no, entry.field)
                if marker in seen:
                    continue
                seen.add(marker)
                evidence.append(entry)

        drafts.append(
            DiscrepancyDraft(
                draft_no=f"DQ-{key[0]}-{index:03d}",
                supplier_code=key[0],
                period=request.scope.period or "",
                diff_type=items[0].type,
                amount=total,
                owner=items[0].owner,
                suggestion=items[0].suggestion,
                confidence=min(item.confidence for item in items),
                needs_manual=any(item.needs_manual for item in items),
                refs=tuple(dict.fromkeys(item.ref for item in items)),
                evidence=tuple(evidence),
            )
        )
    return tuple(drafts)
