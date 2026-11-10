"""主数据就绪检查与单位换算解析（PRD §7.1 / §7.3，DQ-02）。

总原则：宁可不出结论，不可出错结论。任何关键主数据缺失都进缺失清单，
调用方据此输出「无法对账」，不做名称模糊绑定、不按 0 补缺。
"""

from __future__ import annotations

from collections import deque
from decimal import Decimal
from typing import Optional

from .models import (
    DocLine,
    MasterData,
    MissingItem,
    MissingKind,
    ReconRequest,
    dedupe_missing,
)


def conversion_factor(
    master_data: MasterData,
    item_code: Optional[str],
    from_unit: Optional[str],
    to_unit: Optional[str],
) -> Optional[Decimal]:
    """返回 from_unit → to_unit 的乘数；换算关系缺失返回 None（不猜）。"""
    if not from_unit or not to_unit:
        return None
    if from_unit == to_unit:
        return Decimal(1)

    # 1 跳：直接登记 + 反向登记
    direct: dict[tuple[str, str], Decimal] = {}
    neighbors: dict[str, set[str]] = {}
    for conv in master_data.unit_conversions:
        if conv.item != item_code or conv.factor == 0:
            continue
        direct[(conv.from_unit, conv.to_unit)] = conv.factor
        direct.setdefault((conv.to_unit, conv.from_unit), Decimal(1) / conv.factor)
        neighbors.setdefault(conv.from_unit, set()).add(conv.to_unit)
        neighbors.setdefault(conv.to_unit, set()).add(conv.from_unit)

    if (from_unit, to_unit) in direct:
        return direct[(from_unit, to_unit)]

    # 多跳：BFS 最短路径（闭合校验由 DQ-02 体检负责，这里只做可用性解析）
    queue: deque[tuple[str, Decimal]] = deque([(from_unit, Decimal(1))])
    visited = {from_unit}
    while queue:
        unit, factor = queue.popleft()
        for nxt in neighbors.get(unit, ()):
            if nxt in visited:
                continue
            step = direct.get((unit, nxt))
            if step is None:
                continue
            new_factor = factor * step
            if nxt == to_unit:
                return new_factor
            visited.add(nxt)
            queue.append((nxt, new_factor))
    return None


def _line_missing(line: DocLine) -> Optional[str]:
    if not line.item_code:
        return "缺少物料编码"
    if line.qty is None:
        return "缺少数量（缺失≠零）"
    if not line.unit:
        return "缺少计量单位"
    if line.unit_price is None and line.amount is None:
        return "单价与金额同时缺失"
    return None


def check_readiness(request: ReconRequest) -> tuple[MissingItem, ...]:
    """就绪检查：返回缺失清单；非空即代表「无法对账」。"""
    missing: list[MissingItem] = []
    scope = request.scope

    if not scope.org_id:
        missing.append(
            MissingItem(
                MissingKind.SCOPE,
                "org_id",
                "缺少组织范围，无法确定数据边界（G-05）",
                "request.scope",
            )
        )
    if not scope.period:
        missing.append(
            MissingItem(
                MissingKind.SCOPE,
                "period",
                "缺少会计期间，无法确定对账期间（G-11）",
                "request.scope",
            )
        )
    if not scope.system_ids:
        missing.append(
            MissingItem(
                MissingKind.SCOPE,
                "system_ids",
                "缺少系统范围，无法确定取数来源（G-05）",
                "request.scope",
            )
        )
    if not request.suppliers:
        missing.append(
            MissingItem(
                MissingKind.SCOPE,
                "suppliers",
                "未指定供应商集合，拒绝执行对账",
                "request.suppliers",
            )
        )

    master = request.master_data
    for code in request.suppliers:
        if master.supplier(code) is None:
            missing.append(
                MissingItem(
                    MissingKind.SUPPLIER,
                    code,
                    "供应商未在 ERP 主数据中绑定，挂起该供应商（F4）",
                    f"suppliers[{code}]",
                )
            )

    lines = (*request.our_lines, *request.supplier_lines)
    for line in lines:
        if not line.supplier_code or master.supplier(line.supplier_code) is None:
            missing.append(
                MissingItem(
                    MissingKind.SUPPLIER,
                    line.supplier_code or "—",
                    f"行 {line.ref} 的供应商未绑定，禁止按名称自动绑定",
                    line.ref,
                )
            )

        item = master.item(line.item_code)
        if item is None:
            missing.append(
                MissingItem(
                    MissingKind.ITEM,
                    line.item_code or "—",
                    f"行 {line.ref} 的物料未绑定，禁猜测",
                    line.ref,
                )
            )
        elif not item.base_unit:
            missing.append(
                MissingItem(
                    MissingKind.BASE_UNIT,
                    item.code,
                    f"物料 {item.code} 的基础单位为空（DQ-02）",
                    line.ref,
                )
            )

        data_gap = _line_missing(line)
        if data_gap:
            missing.append(
                MissingItem(
                    MissingKind.LINE_DATA,
                    line.ref,
                    f"行 {line.ref} {data_gap}",
                    line.ref,
                )
            )

    # 单位换算：我方与供应商单位不一致时必须有闭合换算关系
    our_units: dict[tuple[str, str], str] = {}
    for line in request.our_lines:
        if line.item_code and line.unit:
            our_units.setdefault((line.supplier_code, line.item_code), line.unit)
    for line in request.supplier_lines:
        key = (line.supplier_code, line.item_code or "")
        our_unit = our_units.get(key)
        if not our_unit or not line.unit or line.unit == our_unit:
            continue
        if conversion_factor(master, line.item_code, line.unit, our_unit) is None:
            missing.append(
                MissingItem(
                    MissingKind.UNIT_CONVERSION,
                    f"{line.item_code}:{line.unit}->{our_unit}",
                    f"行 {line.ref} 缺少单位换算关系，该行不参与金额（PRD §7.1）",
                    line.ref,
                )
            )

    return dedupe_missing(missing)
