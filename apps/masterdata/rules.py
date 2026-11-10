"""S9 主数据体检规则。

规则分五类：单位换算、重复主数据、关键字段缺失、编码形近混淆、期间与账套。
每条规则都在 ``RuleSpec`` 里写明：
- ``why``：为什么它能省钱或防错（一句话，具体到失败路径）；
- ``false_positive``：哪些情况是「提示」而不是「阻断」，人工怎么处理。

所有规则只读快照，不写任何系统；发现的是「数据问题」，修复动作由人执行。
"""

from __future__ import annotations

import re
from collections import defaultdict, deque
from decimal import Decimal
from itertools import combinations

from .models import (
    CurrencyRate,
    Customer,
    Document,
    Evidence,
    Finding,
    InspectionOptions,
    Item,
    MasterDataSnapshot,
    ObjectType,
    RuleSpec,
    Severity,
    Supplier,
    UnitConversion,
)
from .normalize import (
    fold_code,
    name_similarity,
    normalize_bank_account,
    normalize_currency,
    normalize_name,
    normalize_payment_term,
    normalize_tax_code,
    normalize_tax_no,
    normalize_text,
    normalize_unit,
)

_PERIOD_RE = re.compile(r"^(\d{4})-(0[1-9]|1[0-2])$")
# 正反向换算乘积累积允许 0.1% 的录入舍入，超过即视为不闭合
RECIPROCAL_TOLERANCE = Decimal("0.001")
# 汇率、税率允许 0.01% 的配置舍入差
RATE_TOLERANCE = Decimal("0.0001")


def _period_parts(period: str | None) -> tuple[int, int] | None:
    match = _PERIOD_RE.match(period or "")
    if not match:
        return None
    return int(match.group(1)), int(match.group(2))


def _months_between(start: str | None, end: str | None) -> int | None:
    left = _period_parts(start)
    right = _period_parts(end)
    if not left or not right:
        return None
    return (right[0] - left[0]) * 12 + (right[1] - left[1])


def _fmt(value: Decimal) -> str:
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def _median(values: list[Decimal]) -> Decimal:
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / Decimal(2)


def _org_key(org_id: str | None, snapshot: MasterDataSnapshot) -> str:
    return org_id or snapshot.org_id or ""


def _finding(
    spec: RuleSpec,
    severity: Severity,
    object_type: ObjectType,
    object_code: str,
    object_name: str = "",
    *,
    evidence: tuple[Evidence, ...] = (),
    suggestion: str = "",
    supplier_code: str | None = None,
    item_code: str | None = None,
    period: str | None = None,
) -> Finding:
    return Finding(
        rule_id=spec.rule_id,
        rule_name=spec.name,
        category=spec.category,
        severity=severity,
        object_type=object_type,
        object_code=object_code,
        object_name=object_name,
        evidence=evidence,
        suggestion=suggestion,
        why=spec.why,
        false_positive=spec.false_positive,
        supplier_code=supplier_code,
        item_code=item_code,
        period=period,
    )


def _item_name(snapshot: MasterDataSnapshot, code: str) -> str:
    item = snapshot.item(code)
    return item.name if item else ""


def _conversion_edges(
    snapshot: MasterDataSnapshot,
) -> dict[str, dict[str, dict[str, Decimal]]]:
    """按物料建可双向走的换算图；只收合法比例，非法比例由 UOM-001 报。

    必须按物料隔离：不同物料的 PCS/BOX 比例不能互相借用，否则缺换算会被
    别的物料的配置「救活」，这正是体检要避免的静默放行。
    """
    edges: dict[str, dict[str, dict[str, Decimal]]] = defaultdict(
        lambda: defaultdict(dict)
    )
    for conv in snapshot.unit_conversions:
        source = normalize_unit(conv.from_unit)
        target = normalize_unit(conv.to_unit)
        if not source or not target or source == target or conv.factor <= 0:
            continue
        item_edges = edges[conv.item_code]
        item_edges[source].setdefault(target, conv.factor)
        item_edges[target].setdefault(source, Decimal(1) / conv.factor)
    return edges


def _can_convert(
    edges: dict[str, dict[str, Decimal]], source: str, target: str
) -> bool:
    if source == target:
        return True
    visited = {source}
    queue: deque[str] = deque([source])
    while queue:
        unit = queue.popleft()
        for neighbor in edges.get(unit, {}):
            if neighbor in visited:
                continue
            if neighbor == target:
                return True
            visited.add(neighbor)
            queue.append(neighbor)
    return False


# ---------------------------------------------------------------------------
# 单位换算
# ---------------------------------------------------------------------------


def check_multi_conversion(
    snapshot: MasterDataSnapshot,
    options: InspectionOptions,
    spec: RuleSpec,
) -> list[Finding]:
    """同一物料同一单位对出现多条换算式：比例不同即阻断，比例相同只提示清理。"""
    groups: dict[tuple[str, str, str], list[UnitConversion]] = defaultdict(list)
    for conv in snapshot.unit_conversions:
        key = (
            conv.item_code,
            normalize_unit(conv.from_unit),
            normalize_unit(conv.to_unit),
        )
        groups[key].append(conv)

    findings: list[Finding] = []
    for (item_code, source, target), rows in groups.items():
        factors = {row.factor for row in rows}
        invalid = any(
            row.factor <= 0
            or not source
            or not target
            or (source == target and row.factor != 1)
            for row in rows
        )
        if len(factors) > 1:
            severity = Severity.BLOCK
            expected = "同一物料同一单位对只保留一条换算关系"
            suggestion = (
                "确认哪一条比例与实物包装一致，删除其余配置后重跑体检；"
                "未修正前该物料相关行不得自动对账。"
            )
        elif invalid:
            severity = Severity.BLOCK
            expected = "换算比例必须大于 0，同单位换算比例必须为 1"
            suggestion = "修正非法换算比例；未修正前该物料相关行不得自动对账。"
        elif len(rows) > 1:
            severity = Severity.WARN
            expected = "重复配置应清理为一条"
            suggestion = "删除重复配置，避免后续维护时改错其中一条。"
        else:
            continue

        actual = "; ".join(
            f"{source}->{target} factor={_fmt(row.factor)}"
            f" (last_used={row.last_used_period or '从未'}, usage={row.usage_count})"
            for row in rows
        )
        findings.append(
            _finding(
                spec,
                severity,
                ObjectType.CONVERSION,
                item_code,
                _item_name(snapshot, item_code),
                evidence=(
                    Evidence(
                        "factor",
                        actual,
                        expected,
                        f"{snapshot.source}:unit_conversions[{item_code}]",
                    ),
                ),
                suggestion=suggestion,
                item_code=item_code,
            )
        )
    return findings


def check_reciprocal_conversion(
    snapshot: MasterDataSnapshot,
    options: InspectionOptions,
    spec: RuleSpec,
) -> list[Finding]:
    """A→B 与 B→A 都已登记时，乘积必须为 1；否则两个方向会得出两套金额。"""
    pairs: dict[str, dict[tuple[str, str], set[Decimal]]] = defaultdict(
        lambda: defaultdict(set)
    )
    for conv in snapshot.unit_conversions:
        source = normalize_unit(conv.from_unit)
        target = normalize_unit(conv.to_unit)
        if not source or not target or source == target or conv.factor <= 0:
            continue
        pairs[conv.item_code][(source, target)].add(conv.factor)

    findings: list[Finding] = []
    for item_code, item_pairs in pairs.items():
        for (source, target), forward in item_pairs.items():
            if source >= target:
                continue
            backward = item_pairs.get((target, source))
            if not backward:
                continue
            # 同向已有多套比例时由 UOM-001 处理，这里不重复报
            if len(forward) > 1 or len(backward) > 1:
                continue
            factor_forward = next(iter(forward))
            factor_backward = next(iter(backward))
            product = factor_forward * factor_backward
            if abs(product - Decimal(1)) <= RECIPROCAL_TOLERANCE:
                continue
            findings.append(
                _finding(
                    spec,
                    Severity.BLOCK,
                    ObjectType.CONVERSION,
                    item_code,
                    _item_name(snapshot, item_code),
                    evidence=(
                        Evidence(
                            "factor",
                            (
                                f"{source}->{target}={_fmt(factor_forward)}; "
                                f"{target}->{source}={_fmt(factor_backward)}; "
                                f"乘积={_fmt(product)}"
                            ),
                            "乘积必须为 1（容差 0.001）",
                            f"{snapshot.source}:unit_conversions[{item_code}]",
                        ),
                    ),
                    suggestion=(
                        "核对两个方向的换算比例，保留与实物一致的一侧并修正另一侧；"
                        "未修正前该物料相关行不得自动对账。"
                    ),
                    item_code=item_code,
                )
            )
    return findings


def check_peer_factor_outlier(
    snapshot: MasterDataSnapshot,
    options: InspectionOptions,
    spec: RuleSpec,
) -> list[Finding]:
    """同类物料同一单位对的比例偏离中位数：1.5 倍提示，3 倍以上阻断。"""
    grouped: dict[tuple[str, str, str], dict[str, list[Decimal]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for conv in snapshot.unit_conversions:
        item = snapshot.item(conv.item_code)
        if item is None or not item.category or conv.factor <= 0:
            continue
        source = normalize_unit(conv.from_unit)
        target = normalize_unit(conv.to_unit)
        if not source or not target:
            continue
        key = (normalize_text(item.category), source, target)
        grouped[key][item.code].append(conv.factor)

    findings: list[Finding] = []
    for (category, source, target), per_item in grouped.items():
        # 一个物料在同一单位对有多套比例时由 UOM-001 处理，不参与中位数
        eligible = {
            code: values[0]
            for code, values in per_item.items()
            if len(set(values)) == 1
        }
        if len(eligible) < options.min_peer_items:
            continue
        median = _median(list(eligible.values()))
        if median <= 0:
            continue
        for item_code, factor in eligible.items():
            low = min(factor, median)
            high = max(factor, median)
            if low <= 0:
                continue
            ratio = high / low
            if ratio >= options.factor_outlier_block_ratio:
                severity = Severity.BLOCK
                suggestion = (
                    "核对包装规格与历史单据；修正前该物料相关行阻断自动对账。"
                )
            elif ratio > options.factor_outlier_ratio:
                severity = Severity.WARN
                suggestion = (
                    "人工确认是否为新包装规格；确认前该物料对账结果需复核。"
                )
            else:
                continue
            findings.append(
                _finding(
                    spec,
                    severity,
                    ObjectType.CONVERSION,
                    item_code,
                    _item_name(snapshot, item_code),
                    evidence=(
                        Evidence(
                            "factor",
                            f"{source}->{target} factor={_fmt(factor)}，偏离 {_fmt(ratio)} 倍",
                            (
                                f"同类（{category}）{len(eligible)} 个物料的中位数 "
                                f"{_fmt(median)}"
                            ),
                            f"{snapshot.source}:unit_conversions[{item_code}]",
                        ),
                    ),
                    suggestion=suggestion,
                    item_code=item_code,
                )
            )
    return findings


def check_unused_conversion(
    snapshot: MasterDataSnapshot,
    options: InspectionOptions,
    spec: RuleSpec,
) -> list[Finding]:
    """有换算但一个完整会计年度未被使用：只提示清理，避免误删新物料配置。"""
    documents_by_item: dict[str, list[Document]] = defaultdict(list)
    for doc in snapshot.documents:
        if doc.item_code:
            documents_by_item[doc.item_code].append(doc)

    findings: list[Finding] = []
    for conv in snapshot.unit_conversions:
        if conv.factor <= 0:
            continue
        docs = documents_by_item.get(conv.item_code, [])
        periods = [doc.doc_period for doc in docs if _period_parts(doc.doc_period)]
        if not periods:
            # 物料没有任何单据，无法判断「长期未使用」，不报
            continue
        earliest = min(periods)
        active_months = _months_between(earliest, snapshot.as_of_period)
        if active_months is None or active_months < options.unused_conversion_months:
            # 物料启用不满一个周期，不能把新配置当历史遗留
            continue
        if conv.last_used_period:
            gap = _months_between(conv.last_used_period, snapshot.as_of_period)
            if gap is None or gap < options.unused_conversion_months:
                continue
        else:
            gap = active_months
        findings.append(
            _finding(
                spec,
                Severity.WARN,
                ObjectType.CONVERSION,
                conv.item_code,
                _item_name(snapshot, conv.item_code),
                evidence=(
                    Evidence(
                        "last_used_period",
                        (
                            f"last_used={conv.last_used_period or '从未'}, "
                            f"usage_count={conv.usage_count}, "
                            f"物料最早单据期间={earliest}, 未使用约 {gap} 个月"
                        ),
                        (
                            f"近 {options.unused_conversion_months} 个月至少使用一次，"
                            "否则停用或删除该换算"
                        ),
                        f"{snapshot.source}:unit_conversions[{conv.item_code}]",
                    ),
                ),
                suggestion=(
                    "确认该换算是否仍有业务场景；没有则停用/删除，"
                    "有则补录最近使用单据，避免下次选单位时误用。"
                ),
                item_code=conv.item_code,
            )
        )
    return findings


def check_missing_conversion(
    snapshot: MasterDataSnapshot,
    options: InspectionOptions,
    spec: RuleSpec,
) -> list[Finding]:
    """单据单位不是基本单位且没有闭合换算路径：该物料相关行无法换算金额。"""
    edges = _conversion_edges(snapshot)
    grouped: dict[tuple[str, str, str], list[Document]] = defaultdict(list)
    for doc in snapshot.documents:
        if not doc.item_code or not doc.unit:
            continue
        item = snapshot.item(doc.item_code)
        if item is None or not (item.base_unit or "").strip():
            # 基本单位缺失由 MISS-003 报，这里不重复
            continue
        unit = normalize_unit(doc.unit)
        base_unit = normalize_unit(item.base_unit)
        if not unit or unit == base_unit:
            continue
        if _can_convert(edges.get(doc.item_code, {}), unit, base_unit):
            continue
        grouped[(doc.item_code, unit, base_unit)].append(doc)

    findings: list[Finding] = []
    for (item_code, unit, base_unit), docs in grouped.items():
        doc_numbers = sorted({doc.doc_no for doc in docs})
        suppliers = {doc.supplier_code for doc in docs if doc.supplier_code}
        periods = {doc.doc_period for doc in docs if doc.doc_period}
        supplier_code = next(iter(suppliers)) if len(suppliers) == 1 else None
        period = next(iter(periods)) if len(periods) == 1 else None
        examples = ", ".join(doc_numbers[:3])
        if len(doc_numbers) > 3:
            examples += " ..."
        findings.append(
            _finding(
                spec,
                Severity.BLOCK,
                ObjectType.CONVERSION,
                item_code,
                _item_name(snapshot, item_code),
                evidence=(
                    Evidence(
                        "unit",
                        (
                            f"{len(doc_numbers)} 张单据使用 {unit}，"
                            f"基本单位 {base_unit}，无换算路径（示例：{examples}）"
                        ),
                        "单据单位等于基本单位，或存在闭合的单位换算路径",
                        f"{snapshot.source}:documents[{doc_numbers[0]}]",
                    ),
                ),
                suggestion=(
                    "补齐换算关系或修正单据单位；未补齐前该物料相关行不得自动对账。"
                ),
                supplier_code=supplier_code,
                item_code=item_code,
                period=period,
            )
        )
    return findings


# ---------------------------------------------------------------------------
# 关键字段缺失
# ---------------------------------------------------------------------------


def check_missing_supplier_tax(
    snapshot: MasterDataSnapshot,
    options: InspectionOptions,
    spec: RuleSpec,
) -> list[Finding]:
    findings: list[Finding] = []
    for supplier in snapshot.suppliers:
        if normalize_tax_no(supplier.tax_no):
            continue
        findings.append(
            _finding(
                spec,
                Severity.WARN,
                ObjectType.SUPPLIER,
                supplier.code,
                supplier.name,
                evidence=(
                    Evidence(
                        "tax_no",
                        f"tax_no={supplier.tax_no!r}",
                        "统一社会信用代码/税号非空",
                        f"{snapshot.source}:suppliers[{supplier.code}].tax_no",
                    ),
                ),
                suggestion=(
                    "由采购/财务在 ERP 补录税号；补录前该供应商的发票三单匹配转人工。"
                ),
                supplier_code=supplier.code,
            )
        )
    return findings


def check_missing_supplier_bank(
    snapshot: MasterDataSnapshot,
    options: InspectionOptions,
    spec: RuleSpec,
) -> list[Finding]:
    findings: list[Finding] = []
    for supplier in snapshot.suppliers:
        if normalize_bank_account(supplier.bank_account):
            continue
        findings.append(
            _finding(
                spec,
                Severity.WARN,
                ObjectType.SUPPLIER,
                supplier.code,
                supplier.name,
                evidence=(
                    Evidence(
                        "bank_account",
                        f"bank_account={supplier.bank_account!r}",
                        "银行账号非空",
                        f"{snapshot.source}:suppliers[{supplier.code}].bank_account",
                    ),
                ),
                suggestion=(
                    "由财务在 ERP 补录银行账号；本工具只读，不提供任何写回路径。"
                ),
                supplier_code=supplier.code,
            )
        )
    return findings


def check_missing_item_base_unit(
    snapshot: MasterDataSnapshot,
    options: InspectionOptions,
    spec: RuleSpec,
) -> list[Finding]:
    findings: list[Finding] = []
    for item in snapshot.items:
        if (item.base_unit or "").strip():
            continue
        findings.append(
            _finding(
                spec,
                Severity.BLOCK,
                ObjectType.ITEM,
                item.code,
                item.name,
                evidence=(
                    Evidence(
                        "base_unit",
                        f"base_unit={item.base_unit!r}",
                        "基本单位非空",
                        f"{snapshot.source}:items[{item.code}].base_unit",
                    ),
                ),
                suggestion=(
                    "在 ERP 补录基本单位；补录前该物料相关行不得自动对账，"
                    "不存在「先按 1 处理」的降级。"
                ),
                item_code=item.code,
            )
        )
    return findings


# ---------------------------------------------------------------------------
# 重复主数据
# ---------------------------------------------------------------------------


def check_duplicate_supplier_tax(
    snapshot: MasterDataSnapshot,
    options: InspectionOptions,
    spec: RuleSpec,
) -> list[Finding]:
    """同一组织内同一税号对应多个供应商编码（PRD DQ-01）。"""
    groups: dict[tuple[str, str], list[Supplier]] = defaultdict(list)
    for supplier in snapshot.suppliers:
        tax_no = normalize_tax_no(supplier.tax_no)
        if not tax_no:
            continue
        groups[(_org_key(supplier.org_id, snapshot), tax_no)].append(supplier)

    findings: list[Finding] = []
    for (_, tax_no), group in groups.items():
        by_code = {supplier.code: supplier for supplier in group}
        if len(by_code) < 2:
            continue
        codes = sorted(by_code)
        for code in codes:
            supplier = by_code[code]
            others = [item for item in codes if item != code]
            findings.append(
                _finding(
                    spec,
                    Severity.BLOCK,
                    ObjectType.SUPPLIER,
                    code,
                    supplier.name,
                    evidence=(
                        Evidence(
                            "tax_no",
                            f"税号 {tax_no} 同时对应编码 {', '.join(codes)}",
                            "同一组织内一个税号只对应一个有效供应商编码",
                            (
                                f"{snapshot.source}:suppliers[{code}].tax_no"
                                f"（另有 {', '.join(others)}）"
                            ),
                        ),
                    ),
                    suggestion=(
                        "合并重复供应商或停用其中一个编码；修复前该供应商的对账与付款转人工。"
                    ),
                    supplier_code=code,
                )
            )
    return findings


def check_duplicate_supplier_other(
    snapshot: MasterDataSnapshot,
    options: InspectionOptions,
    spec: RuleSpec,
) -> list[Finding]:
    """银行账号相同或名称归一后高度相似：提示级，允许共享账户等合理场景。"""
    by_org: dict[str, list[Supplier]] = defaultdict(list)
    for supplier in snapshot.suppliers:
        by_org[_org_key(supplier.org_id, snapshot)].append(supplier)

    findings: list[Finding] = []
    for entries in by_org.values():
        by_code = {supplier.code: supplier for supplier in entries}
        values = list(by_code.values())
        reasons: dict[str, set[str]] = defaultdict(set)
        for left, right in combinations(values, 2):
            tax_left = normalize_tax_no(left.tax_no)
            tax_right = normalize_tax_no(right.tax_no)
            # 同税号由 DUP-001 阻断，这里不重复报
            if tax_left and tax_left == tax_right:
                continue
            why: list[str] = []
            bank_left = normalize_bank_account(left.bank_account)
            bank_right = normalize_bank_account(right.bank_account)
            if bank_left and bank_left == bank_right:
                why.append("银行账号相同")
            name_left = normalize_name(left.name)
            name_right = normalize_name(right.name)
            if name_left and name_left == name_right:
                why.append("名称归一后相同")
            elif len(name_left) >= 6 and len(name_right) >= 6:
                ratio = name_similarity(left.name, right.name)
                if Decimal(str(ratio)) >= options.duplicate_name_threshold:
                    why.append(f"名称相似度 {ratio:.2f}")
            if not why:
                continue
            for source, target in ((left, right), (right, left)):
                reasons[source.code].add(
                    f"与 {target.code}（{target.name}）: {'、'.join(why)}"
                )

        for code, detail in reasons.items():
            supplier = by_code[code]
            findings.append(
                _finding(
                    spec,
                    Severity.WARN,
                    ObjectType.SUPPLIER,
                    code,
                    supplier.name,
                    evidence=(
                        Evidence(
                            "name/bank_account",
                            "; ".join(sorted(detail)),
                            "同一组织内不重复建档；共用收款账号需财务确认",
                            f"{snapshot.source}:suppliers[{code}]",
                        ),
                    ),
                    suggestion=(
                        "确认是否为同一主体或共享收款账户；合并/停用前该供应商付款需人工复核。"
                    ),
                    supplier_code=code,
                )
            )
    return findings


def check_duplicate_item(
    snapshot: MasterDataSnapshot,
    options: InspectionOptions,
    spec: RuleSpec,
) -> list[Finding]:
    """物料名称、规格、基本单位归一后完全相同，视为重复编码。"""
    groups: dict[tuple[str, str, str, str], list[Item]] = defaultdict(list)
    for item in snapshot.items:
        name = normalize_name(item.name)
        if not name:
            continue
        key = (
            _org_key(item.org_id, snapshot),
            name,
            normalize_name(item.spec),
            normalize_unit(item.base_unit),
        )
        groups[key].append(item)

    findings: list[Finding] = []
    for group in groups.values():
        by_code = {item.code: item for item in group}
        if len(by_code) < 2:
            continue
        codes = sorted(by_code)
        for code in codes:
            item = by_code[code]
            others = [entry for entry in codes if entry != code]
            findings.append(
                _finding(
                    spec,
                    Severity.BLOCK,
                    ObjectType.ITEM,
                    code,
                    item.name,
                    evidence=(
                        Evidence(
                            "name/spec/base_unit",
                            (
                                f"名称={item.name or '—'}，规格={item.spec or '—'}，"
                                f"基本单位={item.base_unit or '—'}，编码 {', '.join(codes)}"
                            ),
                            "同一组织内同一物料只保留一个编码",
                            (
                                f"{snapshot.source}:items[{code}]"
                                f"（另有 {', '.join(others)}）"
                            ),
                        ),
                    ),
                    suggestion=(
                        "确认是否为同一物料；合并编码或停用其中一个，"
                        "修复前两条编码的对账结果都需人工复核。"
                    ),
                    item_code=code,
                )
            )
    return findings


# ---------------------------------------------------------------------------
# 编码形近混淆
# ---------------------------------------------------------------------------


def _code_collisions(
    entries: list[tuple[str, str, str | None, str | None]],
    object_type: ObjectType,
    snapshot: MasterDataSnapshot,
    spec: RuleSpec,
) -> list[Finding]:
    groups: dict[str, list[tuple[str, str, str | None, str | None]]] = defaultdict(
        list
    )
    for entry in entries:
        folded = fold_code(entry[0])
        if not folded:
            continue
        groups[folded].append(entry)

    findings: list[Finding] = []
    for folded, members in groups.items():
        by_code = {member[0]: member for member in members}
        if len(by_code) < 2:
            continue
        codes = sorted(by_code)
        for code in codes:
            _, name, period, supplier_code = by_code[code]
            others = [entry for entry in codes if entry != code]
            findings.append(
                _finding(
                    spec,
                    Severity.BLOCK,
                    object_type,
                    code,
                    name,
                    evidence=(
                        Evidence(
                            "code",
                            (
                                f"{code} 与 {', '.join(others)} "
                                f"在 0/O、1/I 折叠后同为 {folded}"
                            ),
                            "编码在形近字符折叠后仍唯一",
                            f"{snapshot.source}:{object_type.value}[{code}]",
                        ),
                    ),
                    suggestion=(
                        "核对真实编码；修正编码或建立显式映射后再对账，"
                        "不要靠人工记忆区分 0/O、1/I。"
                    ),
                    supplier_code=supplier_code,
                    item_code=code if object_type is ObjectType.ITEM else None,
                    period=period,
                )
            )
    return findings


def check_confusable_codes(
    snapshot: MasterDataSnapshot,
    options: InspectionOptions,
    spec: RuleSpec,
) -> list[Finding]:
    item_entries = [(item.code, item.name, None, None) for item in snapshot.items]
    documents: dict[str, Document] = {}
    for doc in snapshot.documents:
        documents.setdefault(doc.doc_no, doc)
    doc_entries = [
        (doc.doc_no, doc.doc_type, doc.doc_period, doc.supplier_code)
        for doc in documents.values()
    ]
    return _code_collisions(
        item_entries, ObjectType.ITEM, snapshot, spec
    ) + _code_collisions(doc_entries, ObjectType.DOCUMENT, snapshot, spec)


# ---------------------------------------------------------------------------
# 期间与账套
# ---------------------------------------------------------------------------


def check_unclosed_period(
    snapshot: MasterDataSnapshot,
    options: InspectionOptions,
    spec: RuleSpec,
) -> list[Finding]:
    if _period_parts(snapshot.as_of_period) is None:
        return []
    findings: list[Finding] = []
    for period in snapshot.periods:
        if period.closed or _period_parts(period.period) is None:
            continue
        # 当前期间允许未结；早于当前期间仍未结账才提示
        if period.period >= snapshot.as_of_period:
            continue
        findings.append(
            _finding(
                spec,
                Severity.WARN,
                ObjectType.PERIOD,
                period.period,
                "未结账",
                evidence=(
                    Evidence(
                        "closed",
                        "false",
                        f"早于当前期间 {snapshot.as_of_period} 的期间应已结账",
                        f"{snapshot.source}:periods[{period.period}]",
                    ),
                ),
                suggestion=(
                    "由财务在 ERP 完成结账；本工具只读，不执行任何结账动作。"
                ),
                period=period.period,
            )
        )
    return findings


def check_cross_period(
    snapshot: MasterDataSnapshot,
    options: InspectionOptions,
    spec: RuleSpec,
) -> list[Finding]:
    findings: list[Finding] = []
    seen: set[str] = set()
    for doc in snapshot.documents:
        if doc.doc_no in seen:
            continue
        seen.add(doc.doc_no)
        if not doc.doc_period or not doc.posting_period:
            continue
        if _period_parts(doc.doc_period) is None:
            continue
        if _period_parts(doc.posting_period) is None:
            continue
        if doc.doc_period == doc.posting_period:
            continue
        findings.append(
            _finding(
                spec,
                Severity.WARN,
                ObjectType.DOCUMENT,
                doc.doc_no,
                doc.doc_type,
                evidence=(
                    Evidence(
                        "period",
                        (
                            f"业务期间 {doc.doc_period}，"
                            f"记账期间 {doc.posting_period}"
                        ),
                        "业务期间与记账期间一致，或经暂估/补记审批",
                        f"{snapshot.source}:documents[{doc.doc_no}]",
                    ),
                ),
                suggestion=(
                    "人工确认归属期间；跨期可能是合理暂估，但必须能解释，"
                    "否则会表现为对账缺单。"
                ),
                supplier_code=doc.supplier_code,
                item_code=doc.item_code,
                period=doc.doc_period,
            )
        )
    return findings


def check_ledger_org(
    snapshot: MasterDataSnapshot,
    options: InspectionOptions,
    spec: RuleSpec,
) -> list[Finding]:
    """单据记错账套阻断；主数据账套归属不符只提示，允许下次维护时修正。"""
    ledgers = {ledger.ledger_id: ledger for ledger in snapshot.ledgers}
    findings: list[Finding] = []
    seen: set[str] = set()

    for doc in snapshot.documents:
        if doc.doc_no in seen:
            continue
        seen.add(doc.doc_no)
        ledger = ledgers.get(doc.ledger_id)
        if ledger is None:
            actual = f"单据账套 {doc.ledger_id or '空'} 不存在"
        elif doc.org_id and ledger.org_id != doc.org_id:
            actual = (
                f"单据组织 {doc.org_id}，账套 {doc.ledger_id} "
                f"属于组织 {ledger.org_id}"
            )
        else:
            continue
        findings.append(
            _finding(
                spec,
                Severity.BLOCK,
                ObjectType.DOCUMENT,
                doc.doc_no,
                doc.doc_type,
                evidence=(
                    Evidence(
                        "ledger_id",
                        actual,
                        "单据账套必须存在且与单据组织一致",
                        f"{snapshot.source}:documents[{doc.doc_no}]",
                    ),
                ),
                suggestion=(
                    "冲销/重记到正确账套后再对账；本工具只读，不执行写回。"
                ),
                supplier_code=doc.supplier_code,
                item_code=doc.item_code,
                period=doc.doc_period,
            )
        )

    for supplier in snapshot.suppliers:
        if not supplier.ledger_id:
            continue
        ledger = ledgers.get(supplier.ledger_id)
        if ledger is None:
            actual = f"账套 {supplier.ledger_id} 不存在"
        elif supplier.org_id and ledger.org_id != supplier.org_id:
            actual = (
                f"供应商组织 {supplier.org_id}，账套 {supplier.ledger_id} "
                f"属于组织 {ledger.org_id}"
            )
        else:
            continue
        findings.append(
            _finding(
                spec,
                options.ledger_master_severity,
                ObjectType.SUPPLIER,
                supplier.code,
                supplier.name,
                evidence=(
                    Evidence(
                        "ledger_id",
                        actual,
                        "主数据账套归属应与组织一致",
                        f"{snapshot.source}:suppliers[{supplier.code}].ledger_id",
                    ),
                ),
                suggestion="在下次主数据维护时修正账套归属，修复前该供应商数据需人工确认。",
                supplier_code=supplier.code,
            )
        )

    for item in snapshot.items:
        if not item.ledger_id:
            continue
        ledger = ledgers.get(item.ledger_id)
        if ledger is None:
            actual = f"账套 {item.ledger_id} 不存在"
        elif item.org_id and ledger.org_id != item.org_id:
            actual = (
                f"物料组织 {item.org_id}，账套 {item.ledger_id} "
                f"属于组织 {ledger.org_id}"
            )
        else:
            continue
        findings.append(
            _finding(
                spec,
                options.ledger_master_severity,
                ObjectType.ITEM,
                item.code,
                item.name,
                evidence=(
                    Evidence(
                        "ledger_id",
                        actual,
                        "主数据账套归属应与组织一致",
                        f"{snapshot.source}:items[{item.code}].ledger_id",
                    ),
                ),
                suggestion="在下次主数据维护时修正账套归属，修复前该物料数据需人工确认。",
                item_code=item.code,
            )
        )
    return findings


# ---------------------------------------------------------------------------
# 币种与汇率
# ---------------------------------------------------------------------------


def _rate_for(
    snapshot: MasterDataSnapshot,
    currency: str,
    period: str,
    ledger_id: str | None,
) -> CurrencyRate | None:
    """按「币种 + 期间」取账套专用汇率；没有专用配置时退回全局汇率。"""
    specific = None
    fallback = None
    for rate in snapshot.currency_rates:
        if normalize_currency(rate.currency) != currency:
            continue
        if rate.period != period:
            continue
        if ledger_id and rate.ledger_id == ledger_id:
            specific = rate
        elif not rate.ledger_id:
            fallback = rate
    return specific or fallback


def check_missing_currency_rate(
    snapshot: MasterDataSnapshot,
    options: InspectionOptions,
    spec: RuleSpec,
) -> list[Finding]:
    """外币单据找不到汇率配置或单据没填汇率：金额无法折算，直接阻断。"""
    base = normalize_currency(snapshot.base_currency)
    findings: list[Finding] = []
    seen: set[str] = set()
    for doc in snapshot.documents:
        if doc.doc_no in seen:
            continue
        seen.add(doc.doc_no)
        currency = normalize_currency(doc.currency)
        if not currency or currency == base:
            continue
        rate = _rate_for(snapshot, currency, doc.doc_period, doc.ledger_id)
        if rate is not None and doc.exchange_rate is not None:
            continue
        problems: list[str] = []
        if rate is None:
            problems.append(
                f"{currency} 在期间 {doc.doc_period or '空'}、"
                f"账套 {doc.ledger_id or '全局'} 无汇率配置"
            )
        if doc.exchange_rate is None:
            problems.append("单据未填 exchange_rate")
        findings.append(
            _finding(
                spec,
                Severity.BLOCK,
                ObjectType.DOCUMENT,
                doc.doc_no,
                doc.doc_type,
                evidence=(
                    Evidence(
                        "currency/exchange_rate",
                        "；".join(problems),
                        "外币单据必须有该币种、期间、账套的汇率，且单据填写汇率",
                        f"{snapshot.source}:documents[{doc.doc_no}]",
                    ),
                ),
                suggestion=(
                    "由财务补录汇率或修正单据币种；修正前该单据相关行不得自动对账。"
                ),
                supplier_code=doc.supplier_code,
                item_code=doc.item_code,
                period=doc.doc_period or None,
            )
        )
    return findings


def check_currency_rate_mismatch(
    snapshot: MasterDataSnapshot,
    options: InspectionOptions,
    spec: RuleSpec,
) -> list[Finding]:
    """单据汇率与账套配置不一致：同一笔外币会被折成两个本位币金额。"""
    base = normalize_currency(snapshot.base_currency)
    findings: list[Finding] = []
    seen: set[str] = set()
    for doc in snapshot.documents:
        if doc.doc_no in seen:
            continue
        seen.add(doc.doc_no)
        currency = normalize_currency(doc.currency)
        if not currency or currency == base or doc.exchange_rate is None:
            continue
        rate = _rate_for(snapshot, currency, doc.doc_period, doc.ledger_id)
        if rate is None or abs(doc.exchange_rate - rate.rate) <= RATE_TOLERANCE:
            continue
        findings.append(
            _finding(
                spec,
                Severity.BLOCK,
                ObjectType.DOCUMENT,
                doc.doc_no,
                doc.doc_type,
                evidence=(
                    Evidence(
                        "exchange_rate",
                        (
                            f"单据汇率 {_fmt(doc.exchange_rate)}，"
                            f"账套汇率 {_fmt(rate.rate)}"
                        ),
                        "单据汇率必须等于该币种、期间、账套的配置汇率",
                        f"{snapshot.source}:documents[{doc.doc_no}]",
                    ),
                ),
                suggestion=(
                    "核对汇率来源并统一为账套配置值；修正前该单据相关行不得自动对账。"
                ),
                supplier_code=doc.supplier_code,
                item_code=doc.item_code,
                period=doc.doc_period or None,
            )
        )
    return findings


def check_stale_currency_rate(
    snapshot: MasterDataSnapshot,
    options: InspectionOptions,
    spec: RuleSpec,
) -> list[Finding]:
    """汇率长期未维护：只提示，避免把稳定币种当成错误阻断。"""
    findings: list[Finding] = []
    for rate in snapshot.currency_rates:
        reference = rate.updated_period or rate.period
        gap = _months_between(reference, snapshot.as_of_period)
        if gap is None or gap < options.stale_rate_months:
            continue
        findings.append(
            _finding(
                spec,
                Severity.WARN,
                ObjectType.CURRENCY,
                normalize_currency(rate.currency),
                f"{rate.period}/{rate.ledger_id or '全局'}",
                evidence=(
                    Evidence(
                        "updated_period",
                        (
                            f"最近维护期间 {reference}，"
                            f"距 {snapshot.as_of_period} 约 {gap} 个月"
                        ),
                        f"近 {options.stale_rate_months} 个月至少维护一次",
                        f"{snapshot.source}:currency_rates[{rate.currency}/{rate.period}]",
                    ),
                ),
                suggestion=(
                    "核对当前汇率并更新配置；未更新前该币种单据需人工复核折算金额。"
                ),
            )
        )
    return findings


# ---------------------------------------------------------------------------
# 税码与税率
# ---------------------------------------------------------------------------


def check_invalid_tax_code(
    snapshot: MasterDataSnapshot,
    options: InspectionOptions,
    spec: RuleSpec,
) -> list[Finding]:
    """供应商/客户/物料引用了税码目录里不存在的税码。"""
    if not snapshot.tax_codes:
        return []
    valid = {normalize_tax_code(entry.code) for entry in snapshot.tax_codes}
    entries = [
        (ObjectType.SUPPLIER, s.code, s.name, s.tax_code, s.code, None)
        for s in snapshot.suppliers
    ] + [
        (ObjectType.CUSTOMER, c.code, c.name, c.tax_code, None, None)
        for c in snapshot.customers
    ] + [
        (ObjectType.ITEM, i.code, i.name, i.tax_code, None, i.code)
        for i in snapshot.items
    ]
    findings: list[Finding] = []
    for object_type, code, name, tax_code, supplier_code, item_code in entries:
        normalized = normalize_tax_code(tax_code)
        if not normalized or normalized in valid:
            continue
        findings.append(
            _finding(
                spec,
                Severity.BLOCK,
                object_type,
                code,
                name,
                evidence=(
                    Evidence(
                        "tax_code",
                        f"tax_code={tax_code!r} 不在税码目录（有效：{', '.join(sorted(valid))}）",
                        "引用的税码必须存在于税码目录",
                        f"{snapshot.source}:{object_type.value}[{code}].tax_code",
                    ),
                ),
                suggestion=(
                    "在 ERP 修正税码或补齐税码目录；修正前该主体相关发票与对账行不得自动处理。"
                ),
                supplier_code=supplier_code,
                item_code=item_code,
            )
        )
    return findings


def check_tax_rate_mismatch(
    snapshot: MasterDataSnapshot,
    options: InspectionOptions,
    spec: RuleSpec,
) -> list[Finding]:
    """税码的开票税率与申报口径税率不一致：开票税额和申报税额会对不上。"""
    findings: list[Finding] = []
    for entry in snapshot.tax_codes:
        if abs(entry.rate - entry.filing_rate) <= RATE_TOLERANCE:
            continue
        findings.append(
            _finding(
                spec,
                Severity.BLOCK,
                ObjectType.TAX_CODE,
                entry.code,
                entry.name,
                evidence=(
                    Evidence(
                        "rate/filing_rate",
                        (
                            f"开票税率 {_fmt(entry.rate)}，"
                            f"申报口径税率 {_fmt(entry.filing_rate)}"
                        ),
                        "同一税码的开票税率必须与申报口径一致",
                        f"{snapshot.source}:tax_codes[{entry.code}]",
                    ),
                ),
                suggestion=(
                    "由税务/财务确认正确税率并统一目录；修正前该税码相关单据不得自动入账。"
                ),
            )
        )
    return findings


# ---------------------------------------------------------------------------
# 付款条件
# ---------------------------------------------------------------------------


def check_missing_payment_term(
    snapshot: MasterDataSnapshot,
    options: InspectionOptions,
    spec: RuleSpec,
) -> list[Finding]:
    """供应商/客户未维护付款条件：到期日只能临时判断，容易早付或逾期。"""
    findings: list[Finding] = []
    for supplier in snapshot.suppliers:
        if normalize_payment_term(supplier.payment_term):
            continue
        findings.append(
            _finding(
                spec,
                Severity.WARN,
                ObjectType.SUPPLIER,
                supplier.code,
                supplier.name,
                evidence=(
                    Evidence(
                        "payment_term",
                        f"payment_term={supplier.payment_term!r}",
                        "付款条件非空（如 NET30）",
                        f"{snapshot.source}:suppliers[{supplier.code}].payment_term",
                    ),
                ),
                suggestion=(
                    "由采购/财务补录合同约定的付款条件；补录前该供应商付款需人工确认到期日。"
                ),
                supplier_code=supplier.code,
            )
        )
    for customer in snapshot.customers:
        if normalize_payment_term(customer.payment_term):
            continue
        findings.append(
            _finding(
                spec,
                Severity.WARN,
                ObjectType.CUSTOMER,
                customer.code,
                customer.name,
                evidence=(
                    Evidence(
                        "payment_term",
                        f"payment_term={customer.payment_term!r}",
                        "付款条件非空（如 NET30）",
                        f"{snapshot.source}:customers[{customer.code}].payment_term",
                    ),
                ),
                suggestion=(
                    "由销售/财务补录合同约定的付款条件；补录前该客户回款计划需人工确认。"
                ),
            )
        )
    return findings


def check_payment_term_conflict(
    snapshot: MasterDataSnapshot,
    options: InspectionOptions,
    spec: RuleSpec,
) -> list[Finding]:
    """主数据付款条件与合同约定不一致：按主数据付款会偏离合同账期。"""
    findings: list[Finding] = []
    for contract in snapshot.payment_contracts:
        party_type = normalize_text(contract.party_type)
        if party_type == "供应商":
            party = snapshot.supplier(contract.party_code)
            object_type = ObjectType.SUPPLIER
        elif party_type == "客户":
            party = snapshot.customer(contract.party_code)
            object_type = ObjectType.CUSTOMER
        else:
            continue
        if party is None:
            continue
        master = normalize_payment_term(party.payment_term)
        agreed = normalize_payment_term(contract.payment_term)
        if not master or not agreed or master == agreed:
            continue
        findings.append(
            _finding(
                spec,
                Severity.WARN,
                object_type,
                party.code,
                party.name,
                evidence=(
                    Evidence(
                        "payment_term",
                        (
                            f"主数据付款条件 {party.payment_term}，"
                            f"合同 {contract.contract_no or '—'} 约定 {contract.payment_term}"
                        ),
                        "主数据付款条件应与有效合同口径一致",
                        f"{snapshot.source}:payment_contracts[{contract.party_code}]",
                    ),
                ),
                suggestion=(
                    "核对合同最新版本并修正主数据；修正前按合同口径人工复核账期。"
                ),
                supplier_code=party.code if object_type is ObjectType.SUPPLIER else None,
            )
        )
    return findings


# ---------------------------------------------------------------------------
# 客户主数据重复
# ---------------------------------------------------------------------------


def check_duplicate_customer_tax(
    snapshot: MasterDataSnapshot,
    options: InspectionOptions,
    spec: RuleSpec,
) -> list[Finding]:
    """同一组织内同一税号对应多个客户编码。"""
    groups: dict[tuple[str, str], list[Customer]] = defaultdict(list)
    for customer in snapshot.customers:
        tax_no = normalize_tax_no(customer.tax_no)
        if not tax_no:
            continue
        groups[(_org_key(customer.org_id, snapshot), tax_no)].append(customer)

    findings: list[Finding] = []
    for (_, tax_no), group in groups.items():
        by_code = {customer.code: customer for customer in group}
        if len(by_code) < 2:
            continue
        codes = sorted(by_code)
        for code in codes:
            customer = by_code[code]
            others = [entry for entry in codes if entry != code]
            findings.append(
                _finding(
                    spec,
                    Severity.BLOCK,
                    ObjectType.CUSTOMER,
                    code,
                    customer.name,
                    evidence=(
                        Evidence(
                            "tax_no",
                            f"税号 {tax_no} 同时对应编码 {', '.join(codes)}",
                            "同一组织内一个税号只对应一个有效客户编码",
                            (
                                f"{snapshot.source}:customers[{code}].tax_no"
                                f"（另有 {', '.join(others)}）"
                            ),
                        ),
                    ),
                    suggestion=(
                        "合并重复客户或停用其中一个编码；修复前订单、开票与回款需人工复核。"
                    ),
                )
            )
    return findings


def check_duplicate_customer_name(
    snapshot: MasterDataSnapshot,
    options: InspectionOptions,
    spec: RuleSpec,
) -> list[Finding]:
    """同一组织内客户名称归一后完全相同：提示级，避免把同名不同主体直接判错。"""
    groups: dict[tuple[str, str], list[Customer]] = defaultdict(list)
    for customer in snapshot.customers:
        name = normalize_name(customer.name)
        if not name:
            continue
        groups[(_org_key(customer.org_id, snapshot), name)].append(customer)

    findings: list[Finding] = []
    for group in groups.values():
        by_code = {customer.code: customer for customer in group}
        if len(by_code) < 2:
            continue
        codes = sorted(by_code)
        for code in codes:
            customer = by_code[code]
            others = [entry for entry in codes if entry != code]
            findings.append(
                _finding(
                    spec,
                    Severity.WARN,
                    ObjectType.CUSTOMER,
                    code,
                    customer.name,
                    evidence=(
                        Evidence(
                            "name",
                            f"名称归一后相同，编码 {', '.join(codes)}",
                            "同一组织内不重复建档",
                            (
                                f"{snapshot.source}:customers[{code}].name"
                                f"（另有 {', '.join(others)}）"
                            ),
                        ),
                    ),
                    suggestion=(
                        "确认是否为同一客户；合并/停用前该客户订单与回款需人工复核。"
                    ),
                )
            )
    return findings


RULES: tuple[RuleSpec, ...] = (
    RuleSpec(
        rule_id="UOM-001",
        name="同一物料多套换算关系",
        category="单位换算",
        default_severity=Severity.BLOCK,
        why=(
            "同一物料同一单位对出现两个比例时，对账会随机命中其中一个，"
            "数量差可能放大 10 倍以上且不报错。"
        ),
        false_positive=(
            "重复但比例完全一致的配置只提示不阻断；比例不同一律阻断，"
            "直到人工确认保留哪一条。"
        ),
        check=check_multi_conversion,
    ),
    RuleSpec(
        rule_id="UOM-002",
        name="正反向换算不闭合",
        category="单位换算",
        default_severity=Severity.BLOCK,
        why=(
            "A→B 与 B→A 乘积不等于 1 时，我方与供应商各用一边就会得出两套金额，"
            "差异被误判成单价问题。"
        ),
        false_positive=(
            "仅当两个方向都已登记时才检查；只登记单向视为可推导，不提示。"
            "容差 0.1% 用于吸收录入舍入。"
        ),
        check=check_reciprocal_conversion,
    ),
    RuleSpec(
        rule_id="UOM-003",
        name="换算比例偏离同类物料",
        category="单位换算",
        default_severity=Severity.BLOCK,
        why=(
            "同类物料包装规格通常稳定，比例突然差 3 倍以上多半是把 100 写成 10，"
            "提前发现可避免整批金额差一个数量级。"
        ),
        false_positive=(
            "同类别样本少于 3 个不判断；偏离 1.5–3 倍只提示（可能是新包装规格），"
            "≥3 倍阻断并要求人工确认。"
        ),
        check=check_peer_factor_outlier,
    ),
    RuleSpec(
        rule_id="UOM-004",
        name="换算关系长期未使用",
        category="单位换算",
        default_severity=Severity.WARN,
        why=(
            "一年以上没被任何单据用过的换算关系多半是历史遗留，"
            "留着会在下一次选单位时被误用，清理比事后追账便宜。"
        ),
        false_positive=(
            "物料没有单据、或启用不满 12 个月的不提示，避免把新物料当脏数据。"
        ),
        check=check_unused_conversion,
    ),
    RuleSpec(
        rule_id="UOM-005",
        name="缺少闭合换算关系",
        category="单位换算",
        default_severity=Severity.BLOCK,
        why=(
            "单据单位不是基本单位却找不到闭合换算时，该行无法换算成金额，"
            "继续对账只能靠猜，猜错就是十倍级差异。"
        ),
        false_positive=(
            "单据只用基本单位的不报；换算缺失只阻断该物料相关行，不阻断整批。"
        ),
        check=check_missing_conversion,
    ),
    RuleSpec(
        rule_id="MISS-001",
        name="供应商缺税号",
        category="关键字段缺失",
        default_severity=Severity.WARN,
        why=(
            "没有税号就无法做发票三单匹配，也无法确认两家供应商是不是同一法人，"
            "重复付款风险留到付款环节才暴露。"
        ),
        false_positive=(
            "仅提示不阻断对账；境外供应商等确实无中国税号的场景由人工确认后忽略。"
        ),
        check=check_missing_supplier_tax,
    ),
    RuleSpec(
        rule_id="MISS-002",
        name="供应商缺银行账号",
        category="关键字段缺失",
        default_severity=Severity.WARN,
        why=(
            "银行账号缺失时付款只能临时补录，临时补录正是打款到错误账户的高发环节。"
        ),
        false_positive=(
            "仅提示；账号由财务在 ERP 补录，本工具只读，不提供任何写回路径。"
        ),
        check=check_missing_supplier_bank,
    ),
    RuleSpec(
        rule_id="MISS-003",
        name="物料缺基本单位",
        category="关键字段缺失",
        default_severity=Severity.BLOCK,
        why=(
            "没有基本单位就无法判断供应商报的箱、件、公斤该乘多少，"
            "任何数量结论都不可靠。"
        ),
        false_positive=(
            "一律阻断该物料相关行；不存在「先按 1 处理」的降级，缺失就是缺失。"
        ),
        check=check_missing_item_base_unit,
    ),
    RuleSpec(
        rule_id="DUP-001",
        name="供应商税号重复",
        category="重复主数据",
        default_severity=Severity.BLOCK,
        why=(
            "同一税号挂两个供应商编码，对账、付款、发票会各走一条记录，"
            "重复付款和漏对账都从这里开始。"
        ),
        false_positive=(
            "按组织范围比较，跨组织同税号不报；同一组织内一律阻断，"
            "需合并或停用其中一个编码。"
        ),
        check=check_duplicate_supplier_tax,
    ),
    RuleSpec(
        rule_id="DUP-002",
        name="供应商账号或名称疑似重复",
        category="重复主数据",
        default_severity=Severity.WARN,
        why=(
            "名称只差一个空格或全角括号、或共用同一个收款账号时，"
            "采购很容易把订单下到另一个编码，账对得上但钱付错对象。"
        ),
        false_positive=(
            "同税号的重复由 DUP-001 阻断，这里不重复报；"
            "仅共用账号或名称高度相似时提示，共享收款账户经财务确认后可忽略。"
        ),
        check=check_duplicate_supplier_other,
    ),
    RuleSpec(
        rule_id="DUP-003",
        name="物料重复编码",
        category="重复主数据",
        default_severity=Severity.BLOCK,
        why=(
            "同一物料建两个编码后，采购和仓库各用一个，"
            "库存与对账数量被拆成两半，永远对不平。"
        ),
        false_positive=(
            "名称、规格、基本单位归一后完全相同才阻断；"
            "只是名称相似的不报，避免把同系列不同型号误判为重复。"
        ),
        check=check_duplicate_item,
    ),
    RuleSpec(
        rule_id="CODE-001",
        name="形近编码冲突",
        category="编码混淆",
        default_severity=Severity.BLOCK,
        why=(
            "0/O、1/I 只差一个字符的两个编码在人工录入和 OCR 里极易互串，"
            "一旦串号，对账会把两笔不同业务当成同一笔。"
        ),
        false_positive=(
            "只报「折叠后撞车」的编码对，不报单个含 0/O 的编码；"
            "确认是历史误建后修正编码或建立显式映射即可消除。"
        ),
        check=check_confusable_codes,
    ),
    RuleSpec(
        rule_id="PERIOD-001",
        name="期间未结账",
        category="期间与账套",
        default_severity=Severity.WARN,
        why=(
            "旧期间一直开着，后续单据可以继续记进去，月底对账的期间边界会漂移，"
            "差异无法归属到正确月份。"
        ),
        false_positive=(
            "当前期间（等于 as_of_period）允许未结；只提示不阻断，"
            "结账动作仍由财务在 ERP 完成。"
        ),
        check=check_unclosed_period,
    ),
    RuleSpec(
        rule_id="PERIOD-002",
        name="跨期记账",
        category="期间与账套",
        default_severity=Severity.WARN,
        why=(
            "业务期间和记账期间不一致时，同一批货可能被我方记在 9 月、"
            "供应商开在 10 月，对账时表现为莫名其妙的缺单。"
        ),
        false_positive=(
            "仅提示；跨期本身可能是合理的暂估或补记，"
            "需人工确认归属期间，不直接判错。"
        ),
        check=check_cross_period,
    ),
    RuleSpec(
        rule_id="LEDGER-001",
        name="账套与组织不匹配",
        category="期间与账套",
        default_severity=Severity.BLOCK,
        why=(
            "单据记进别的组织的账套，金额会出现在错误的报表主体上，"
            "对账范围再准也取不到这笔数。"
        ),
        false_positive=(
            "单据级不匹配阻断（已经记错账）；主数据级账套归属不符只提示，"
            "允许在下次主数据维护时修正。"
        ),
        check=check_ledger_org,
    ),
    RuleSpec(
        rule_id="CUR-001",
        name="外币单据缺汇率",
        category="币种与汇率",
        default_severity=Severity.BLOCK,
        why=(
            "外币单据找不到对应期间/账套的汇率，或单据本身没填汇率时，"
            "金额只能按 1:1 或旧汇率硬折，整批本位币金额都会错。"
        ),
        false_positive=(
            "本位币单据不报；只有币种非本位币且缺配置或缺单据汇率才阻断。"
            "汇率是否合理由 CUR-002/CUR-003 分别判断，不在这里猜。"
        ),
        check=check_missing_currency_rate,
    ),
    RuleSpec(
        rule_id="CUR-002",
        name="单据汇率与账套不一致",
        category="币种与汇率",
        default_severity=Severity.BLOCK,
        why=(
            "同一外币单据若按单据汇率和账套汇率各折一次，会得到两个本位币金额，"
            "对账差异会被误判成单价问题。"
        ),
        false_positive=(
            "仅当单据和账套都填了汇率且差异超过 0.01% 才阻断；"
            "缺少任一侧由 CUR-001 处理，不重复报。"
        ),
        check=check_currency_rate_mismatch,
    ),
    RuleSpec(
        rule_id="CUR-003",
        name="汇率长期未更新",
        category="币种与汇率",
        default_severity=Severity.WARN,
        why=(
            "汇率连续两个季度没维护，折算金额会慢慢偏离真实汇率；"
            "提示后由财务确认是否仍适用，比月底集中调账便宜。"
        ),
        false_positive=(
            "仅提示不阻断；联系汇率稳定或已锁定汇率的场景，人工确认后可忽略。"
            "未记录 updated_period 时按 period 计算，不把「没记录」当「很旧」。"
        ),
        check=check_stale_currency_rate,
    ),
    RuleSpec(
        rule_id="TAX-001",
        name="引用无效税码",
        category="税码与税率",
        default_severity=Severity.BLOCK,
        why=(
            "税码在目录里不存在时，开票和申报会各按一套默认逻辑处理，"
            "轻则税额算错，重则发票作废重开。"
        ),
        false_positive=(
            "税码目录为空时不做判断，避免把「未提供目录」误报成全部无效；"
            "只有非空且不在目录里的税码才阻断。"
        ),
        check=check_invalid_tax_code,
    ),
    RuleSpec(
        rule_id="TAX-002",
        name="税率与申报口径不符",
        category="税码与税率",
        default_severity=Severity.BLOCK,
        why=(
            "同一税码的开票税率和申报口径税率不一致时，账面销项/进项税额"
            "与申报表永远差一截，月底只能靠人工调账。"
        ),
        false_positive=(
            "允许 0.01% 的配置舍入差；超过才阻断。"
            "税率本身是否适用由税务判断，本规则只比对两个口径是否一致。"
        ),
        check=check_tax_rate_mismatch,
    ),
    RuleSpec(
        rule_id="PAY-001",
        name="付款条件缺失",
        category="付款条件",
        default_severity=Severity.WARN,
        why=(
            "没有付款条件时，到期日只能临时按经验判断，"
            "早付占用现金、晚付产生逾期，都是可避免的成本。"
        ),
        false_positive=(
            "仅提示不阻断；一次性供应商等确实无需账期的场景，"
            "人工确认后可忽略。"
        ),
        check=check_missing_payment_term,
    ),
    RuleSpec(
        rule_id="PAY-002",
        name="付款条件与合同口径冲突",
        category="付款条件",
        default_severity=Severity.WARN,
        why=(
            "主数据付款条件与合同约定不一致时，系统会按错误账期安排付款，"
            "可能提前付款或构成违约，且对账时看不出原因。"
        ),
        false_positive=(
            "合同未覆盖或主数据未维护付款条件的不报（缺失由 PAY-001 提示）；"
            "合同变更未同步时只提示，需人工核对最新合同版本。"
        ),
        check=check_payment_term_conflict,
    ),
    RuleSpec(
        rule_id="CUST-001",
        name="客户税号重复",
        category="客户主数据重复",
        default_severity=Severity.BLOCK,
        why=(
            "同一税号挂多个客户编码，订单、开票、回款会分散到不同编码，"
            "应收账龄和信用额度都会被拆开，重复催收或漏催收都从这里开始。"
        ),
        false_positive=(
            "按组织范围比较，跨组织同税号不报；同一组织内一律阻断，"
            "需合并或停用其中一个编码。"
        ),
        check=check_duplicate_customer_tax,
    ),
    RuleSpec(
        rule_id="CUST-002",
        name="客户名称疑似重复",
        category="客户主数据重复",
        default_severity=Severity.WARN,
        why=(
            "名称只差一个空格或全角括号时，销售很容易把订单下到另一个编码，"
            "账能对上但回款归属会错。"
        ),
        false_positive=(
            "同税号的重复由 CUST-001 阻断，这里不重复报；"
            "仅名称归一后完全相同才提示，同名不同主体经人工确认后可忽略。"
        ),
        check=check_duplicate_customer_name,
    ),
)

RULES_BY_ID: dict[str, RuleSpec] = {rule.rule_id: rule for rule in RULES}
