"""主数据匹配 Top3 与首次出现人工绑定（FR-403 / FR-1404）。

**能力边界（先看这里）**：本模块是纯函数库，只做「解析结果 → 主数据候选」，
不写数据库、不调 API、不改解析管线、不持久化绑定关系。运行期是否可用的判据
不是「函数能跑」，而是「有没有调用方与绑定存储」——当前都没有，见 README。

匹配口径（fail-closed）：

- **稳定标识才自动解析**：物料编码（``item_code``）或供应商税号（``tax_no``）
  在主数据中唯一命中时，直接给出 ``auto_matched``；
- **名称永远只给候选**：名称相等/相似只生成 Top3，不自动绑定；没有已确认绑定
  的名称一律视为「首次出现」，状态 ``needs_binding``，必须人工确认；
- **歧义不判**：Top1 与 Top2 分差小于 ``TIE_GAP``（或主数据存在重复编码/税号）
  只给候选，不自动通过；
- **缺失不补**：名称为 ``None`` 就是缺失，不拿空串参与相似度，不补 0；
- **未知编码不猜**：编码在主数据里查不到时照实报 ``E_ITEM_CODE_UNKNOWN``，
  不按名称静默改绑到另一个编码；
- **冲突不吞**：编码与已确认绑定指向不同主数据、或绑定配置自身冲突时，
  报错并停止解析，不按「更像的那个」猜。

行定位：``DocLine.page`` / ``DocLine.line_no`` 原样带出。注意 ``line_no`` 来自
解析时的「序号列」或数据行序号（见 ``mapping/fields.py``），**不保证等于物理
文件行号**；拿不到时为 ``None``，错误信息写「行号缺失」，不编造。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum

from apps.masterdata import (
    Item,
    MasterDataSnapshot,
    Supplier,
    name_similarity,
    normalize_name,
    normalize_tax_no,
)

from .models import (
    LOW_CONFIDENCE_THRESHOLD,
    DocHeader,
    DocLine,
    FieldValue,
    IssueSeverity,
    ParseResult,
)

# 每行最多给出的候选数
TOP_N = 3
# 低于该相似度的名称不进入候选，避免把「什么都不像」硬凑成候选
MIN_CANDIDATE_SIMILARITY = Decimal("0.60")
# Top1 与 Top2 分差小于该值视为并列歧义，不自动判定
TIE_GAP = Decimal("0.02")
# 稳定标识（编码/税号/已确认绑定）的唯一命中分
STABLE_KEY_SCORE = Decimal("1.00")
# 名称归一后完全相等：仍不是身份，只给 0.95，且不自动绑定
NAME_EXACT_SCORE = Decimal("0.95")
# 模糊相似度上限，保证与「名称完全相等」可区分
FUZZY_SCORE_CAP = Decimal("0.94")


class MatchKind(str, Enum):
    ITEM = "item"
    SUPPLIER = "supplier"


class MatchStatus(str, Enum):
    """匹配状态。``auto_resolved`` 只覆盖前两种。"""

    AUTO_MATCHED = "auto_matched"
    BOUND = "bound"
    NEEDS_BINDING = "needs_binding"
    AMBIGUOUS = "ambiguous"
    UNMATCHED = "unmatched"
    MISSING_INPUT = "missing_input"


AUTO_STATUSES = frozenset({MatchStatus.AUTO_MATCHED, MatchStatus.BOUND})

_KIND_LABEL = {MatchKind.ITEM: "物料", MatchKind.SUPPLIER: "供应商"}
_CODE_LABEL = {MatchKind.ITEM: "编码", MatchKind.SUPPLIER: "税号"}


@dataclass(frozen=True)
class Candidate:
    """一个主数据候选：编码/名称/分值/依据。"""

    code: str
    name: str
    score: Decimal
    # code_exact | tax_no_exact | name_exact | name_similar | binding
    basis: str


@dataclass(frozen=True)
class MatchIssue:
    """匹配问题。``location`` 为 ``lines[i].item_name`` 之类的路径。"""

    code: str
    severity: IssueSeverity
    message: str
    location: str
    page: int | None = None
    line_no: int | None = None


@dataclass(frozen=True)
class EntityMatch:
    """单个实体（供应商头或一条明细物料）的匹配结果。"""

    kind: MatchKind
    status: MatchStatus
    query_name: str | None
    query_code: str | None
    candidates: tuple[Candidate, ...]
    resolved_code: str | None
    location: str
    page: int | None = None
    line_no: int | None = None
    low_confidence_input: bool = False
    notes: tuple[str, ...] = ()
    issues: tuple[MatchIssue, ...] = ()

    @property
    def auto_resolved(self) -> bool:
        return self.status in AUTO_STATUSES


@dataclass(frozen=True)
class PendingBinding:
    """一次人工绑定动作：同名多次出现只列一条。"""

    kind: MatchKind
    name: str
    normalized_name: str
    status: MatchStatus
    locations: tuple[str, ...]
    candidates: tuple[Candidate, ...]

    @property
    def occurrences(self) -> int:
        return len(self.locations)


@dataclass(frozen=True)
class MasterMatchResult:
    """一份解析结果的主数据匹配汇总。"""

    supplier: EntityMatch
    line_matches: tuple[EntityMatch, ...]
    pending_bindings: tuple[PendingBinding, ...]
    issues: tuple[MatchIssue, ...] = field(default_factory=tuple)
    master_source: str = ""

    @property
    def requires_manual_binding(self) -> bool:
        return bool(self.pending_bindings)

    @property
    def resolved_count(self) -> int:
        return sum(1 for match in self.line_matches if match.auto_resolved)

    @property
    def unresolved_count(self) -> int:
        return len(self.line_matches) - self.resolved_count

    @property
    def is_fully_resolved(self) -> bool:
        if not self.line_matches or not self.supplier.auto_resolved:
            return False
        return self.unresolved_count == 0


@dataclass(frozen=True)
class _BindingIndex:
    """归一化后的「名称 → 编码」绑定；``conflicted`` 中的名称禁止解析。"""

    mapping: Mapping[str, str]
    conflicted: frozenset[str]


def _text(field_value: FieldValue[str] | None) -> str | None:
    """取字段值；``None`` 或空白都按缺失处理，不拿空串参与匹配。"""

    if field_value is None or field_value.value is None:
        return None
    text = field_value.value.strip()
    return text or None


def _is_low_confidence(*fields: FieldValue[str] | None) -> bool:
    return any(
        item is not None
        and item.value is not None
        and item.confidence < LOW_CONFIDENCE_THRESHOLD
        for item in fields
    )


def _where(location: str, page: int | None, line_no: int | None) -> str:
    parts: list[str] = []
    if page is not None:
        parts.append(f"第 {page} 页")
    if line_no is not None:
        parts.append(f"行号 {line_no}")
    elif page is not None:
        parts.append("行号缺失")
    suffix = f"（{' / '.join(parts)}）" if parts else ""
    return f"{location}{suffix}"


def _issue(
    code: str,
    severity: IssueSeverity,
    message: str,
    location: str,
    page: int | None = None,
    line_no: int | None = None,
) -> MatchIssue:
    return MatchIssue(
        code=code,
        severity=severity,
        message=f"{_where(location, page, line_no)}：{message}",
        location=location,
        page=page,
        line_no=line_no,
    )


def _error(
    code: str, message: str, location: str, page: int | None, line_no: int | None
) -> MatchIssue:
    return _issue(code, IssueSeverity.ERROR, message, location, page, line_no)


def _warning(
    code: str, message: str, location: str, page: int | None, line_no: int | None
) -> MatchIssue:
    return _issue(code, IssueSeverity.WARNING, message, location, page, line_no)


def _build_bindings(
    bindings: Mapping[str, str] | None, location_prefix: str
) -> tuple[_BindingIndex, list[MatchIssue]]:
    """把「名称 → 编码」绑定归一化；空别名/空目标/同键不同值都 fail-closed。"""

    mapping: dict[str, str] = {}
    conflicted: set[str] = set()
    issues: list[MatchIssue] = []

    for raw_name, raw_code in (bindings or {}).items():
        location = f"{location_prefix}.bindings[{raw_name!r}]"
        key = normalize_name(raw_name)
        code = (raw_code or "").strip()
        if not key:
            issues.append(
                _error("E_BINDING_ALIAS_EMPTY", "绑定别名为空，无法匹配", location, None, None)
            )
            continue
        if not code:
            issues.append(
                _error(
                    "E_BINDING_TARGET_EMPTY",
                    f"绑定「{raw_name}」的目标编码为空",
                    location,
                    None,
                    None,
                )
            )
            continue
        if key in conflicted:
            continue
        if key in mapping and mapping[key] != code:
            previous = mapping.pop(key)
            conflicted.add(key)
            issues.append(
                _error(
                    "E_BINDING_CONFLICT",
                    f"绑定「{raw_name}」归一后与其它绑定同键但目标不同"
                    f"（{previous} vs {code}），拒绝解析",
                    location,
                    None,
                    None,
                )
            )
            continue
        mapping[key] = code

    return _BindingIndex(mapping=mapping, conflicted=frozenset(conflicted)), issues


def _name_candidates(
    name: str, entries: Sequence[Item] | Sequence[Supplier]
) -> list[Candidate]:
    """按名称生成候选，已排序并截断到 TopN。"""

    normalized = normalize_name(name)
    if not normalized:
        return []

    candidates: list[Candidate] = []
    for entry in entries:
        other = normalize_name(entry.name)
        if not other:
            continue
        if normalized == other:
            score = NAME_EXACT_SCORE
            basis = "name_exact"
        else:
            ratio = Decimal(str(round(name_similarity(name, entry.name), 4)))
            if ratio < MIN_CANDIDATE_SIMILARITY:
                continue
            score = min(ratio, FUZZY_SCORE_CAP)
            basis = "name_similar"
        candidates.append(
            Candidate(code=entry.code, name=entry.name, score=score, basis=basis)
        )

    candidates.sort(key=lambda item: (-item.score, item.code))
    return candidates[:TOP_N]


def _merge_candidates(*groups: Iterable[Candidate]) -> tuple[Candidate, ...]:
    """按编码去重（保留最高分），再按分数降序、编码升序截断 TopN。"""

    merged: dict[str, Candidate] = {}
    for group in groups:
        for candidate in group:
            current = merged.get(candidate.code)
            if current is None or candidate.score > current.score:
                merged[candidate.code] = candidate
    ordered = sorted(merged.values(), key=lambda item: (-item.score, item.code))
    return tuple(ordered[:TOP_N])


def _exact_entries(
    entries: Sequence[Item] | Sequence[Supplier],
    *,
    code: str | None = None,
    tax_no: str | None = None,
) -> list[Item] | list[Supplier]:
    """按稳定标识找主数据；重复编码/税号原样返回，由调用方判歧义。"""

    if code is not None:
        return [entry for entry in entries if entry.code == code]
    target = normalize_tax_no(tax_no)
    if not target:
        return []
    return [
        entry
        for entry in entries
        if normalize_tax_no(getattr(entry, "tax_no", None)) == target
    ]


def _resolve(
    *,
    kind: MatchKind,
    name: str | None,
    code: str | None,
    master_entries: Sequence[Item] | Sequence[Supplier],
    bindings: _BindingIndex,
    location: str,
    page: int | None,
    line_no: int | None,
    low_confidence: bool,
    stable_basis: str,
    unknown_code_issue: str,
    duplicate_issue: str,
) -> EntityMatch:
    """供应商/物料共用的匹配主流程。"""

    issues: list[MatchIssue] = []
    notes: list[str] = []

    if name is None and code is None:
        issue = _error(
            "E_MISSING_INPUT",
            f"{_KIND_LABEL[kind]}名称与{_CODE_LABEL[kind]}均为空，无法匹配（缺失≠零）",
            location,
            page,
            line_no,
        )
        return EntityMatch(
            kind=kind,
            status=MatchStatus.MISSING_INPUT,
            query_name=None,
            query_code=None,
            candidates=(),
            resolved_code=None,
            location=location,
            page=page,
            line_no=line_no,
            low_confidence_input=low_confidence,
            notes=("名称为 None 即缺失，不按空串参与相似度",),
            issues=(issue,),
        )

    # ① 稳定标识：编码或税号
    stable_entries: list[Item] | list[Supplier] = []
    if kind is MatchKind.ITEM and code is not None:
        stable_entries = _exact_entries(master_entries, code=code)
        if not stable_entries:
            issues.append(
                _error(
                    unknown_code_issue,
                    f"物料编码「{code}」未在主数据中找到；不按名称静默改绑到其它编码",
                    location,
                    page,
                    line_no,
                )
            )
        elif len(stable_entries) > 1:
            issues.append(
                _error(
                    duplicate_issue,
                    f"物料编码「{code}」在主数据中出现 {len(stable_entries)} 次，"
                    "无法确定唯一主体",
                    location,
                    page,
                    line_no,
                )
            )
    elif kind is MatchKind.SUPPLIER and code is not None:
        # 供应商的稳定标识是税号，沿用 code 参数传税号
        stable_entries = _exact_entries(master_entries, tax_no=code)
        if not stable_entries:
            issues.append(
                _error(
                    unknown_code_issue,
                    f"供应商税号「{code}」未在主数据中找到",
                    location,
                    page,
                    line_no,
                )
            )
        elif len(stable_entries) > 1:
            issues.append(
                _error(
                    duplicate_issue,
                    f"供应商税号「{code}」在主数据中出现 {len(stable_entries)} 次，"
                    "无法确定唯一主体",
                    location,
                    page,
                    line_no,
                )
            )

    stable_candidates = [
        Candidate(
            code=entry.code,
            name=entry.name,
            score=STABLE_KEY_SCORE,
            basis=stable_basis,
        )
        for entry in stable_entries
    ]

    # ② 名称候选（永远只是候选，不自动绑定）
    name_candidates = (
        _name_candidates(name, master_entries) if name is not None else []
    )

    # ③ 已确认绑定
    binding_code: str | None = None
    if name is not None:
        key = normalize_name(name)
        if key in bindings.conflicted:
            issues.append(
                _error(
                    "E_BINDING_CONFLICT",
                    f"名称「{name}」的绑定配置存在同键冲突，拒绝解析",
                    location,
                    page,
                    line_no,
                )
            )
        elif key in bindings.mapping:
            candidate_code = bindings.mapping[key]
            target = _exact_entries(master_entries, code=candidate_code)
            if not target:
                issues.append(
                    _error(
                        "E_BINDING_TARGET_UNKNOWN",
                        f"名称「{name}」的已确认绑定指向不存在的编码「{candidate_code}」",
                        location,
                        page,
                        line_no,
                    )
                )
            else:
                binding_code = candidate_code

    binding_candidates = (
        [
            Candidate(
                code=binding_code,
                name=next(
                    entry.name
                    for entry in master_entries
                    if entry.code == binding_code
                ),
                score=STABLE_KEY_SCORE,
                basis="binding",
            )
        ]
        if binding_code is not None
        else []
    )

    candidates = _merge_candidates(
        stable_candidates, binding_candidates, name_candidates
    )

    # ④ 状态判定：冲突优先，其次稳定标识，再名称候选
    if binding_code is not None and code is not None and not stable_entries:
        # 单据给了稳定标识但主数据查不到，名称绑定不能替它兜底：两者不一致时
        # 宁可交人工，也不把「未知编码」静默收编到绑定目标。
        status = MatchStatus.AMBIGUOUS
        resolved_code = None
        issues.append(
            _error(
                "E_UNKNOWN_CODE_WITH_BINDING",
                f"{_CODE_LABEL[kind]}「{code}」未命中主数据，而名称「{name}」"
                f"绑定到「{binding_code}」；不能自动确认，交人工裁决",
                location,
                page,
                line_no,
            )
        )
    elif binding_code is not None and stable_candidates:
        stable_codes = {candidate.code for candidate in stable_candidates}
        if stable_codes == {binding_code} and len(stable_candidates) == 1:
            status = MatchStatus.BOUND
            resolved_code = binding_code
            notes.append(f"使用已确认绑定「{name}」→ {binding_code}")
        else:
            status = MatchStatus.AMBIGUOUS
            resolved_code = None
            issues.append(
                _error(
                    "E_CODE_BINDING_CONFLICT",
                    f"{_CODE_LABEL[kind]}「{code}」与名称「{name}」的已确认绑定"
                    f"「{binding_code}」指向不同主体，交人工裁决",
                    location,
                    page,
                    line_no,
                )
            )
    elif binding_code is not None:
        status = MatchStatus.BOUND
        resolved_code = binding_code
        notes.append(f"使用已确认绑定「{name}」→ {binding_code}")
    elif len(stable_candidates) == 1:
        status = MatchStatus.AUTO_MATCHED
        resolved_code = stable_candidates[0].code
        notes.append(
            f"{_CODE_LABEL[kind]}「{code}」在主数据唯一命中，按稳定标识自动解析"
        )
        if (
            name_candidates
            and name_candidates[0].code != resolved_code
            and name_candidates[0].score >= NAME_EXACT_SCORE
        ):
            issues.append(
                _warning(
                    "W_CODE_NAME_CONFLICT",
                    f"名称「{name}」与{_CODE_LABEL[kind]}「{resolved_code}」"
                    f"的名称不一致，名称更强的候选是「{name_candidates[0].code}」；"
                    "按稳定标识解析，但需复核主数据",
                    location,
                    page,
                    line_no,
                )
            )
    elif len(stable_candidates) > 1:
        status = MatchStatus.AMBIGUOUS
        resolved_code = None
    elif name_candidates:
        top, second = name_candidates[0], name_candidates[1] if len(name_candidates) > 1 else None
        if (
            top.score < STABLE_KEY_SCORE
            and second is not None
            and top.score - second.score < TIE_GAP
        ):
            status = MatchStatus.AMBIGUOUS
            resolved_code = None
            issues.append(
                _error(
                    "E_AMBIGUOUS",
                    f"Top1「{top.code}」与 Top2「{second.code}」分差"
                    f" {top.score - second.score} < {TIE_GAP}，只给候选不判定",
                    location,
                    page,
                    line_no,
                )
            )
        else:
            status = MatchStatus.NEEDS_BINDING
            resolved_code = None
            issues.append(
                _error(
                    "E_PENDING_BINDING",
                    f"名称「{name}」没有已确认绑定，Top1 候选「{top.code}」"
                    f"（{top.score}）仅供参考，必须人工绑定",
                    location,
                    page,
                    line_no,
                )
            )
    else:
        status = MatchStatus.UNMATCHED
        resolved_code = None
        if name is None:
            reason = (
                f"未提供名称，且{_CODE_LABEL[kind]}「{code}」未命中主数据，"
                "不生成猜测候选"
            )
        else:
            reason = (
                f"名称「{name}」没有相似度 ≥ {MIN_CANDIDATE_SIMILARITY} 的候选，"
                "需人工建主数据或确认无此主体"
            )
        issues.append(
            _error("E_NO_CANDIDATE", reason, location, page, line_no)
        )

    if low_confidence:
        issues.append(
            _warning(
                "W_LOW_CONFIDENCE_INPUT",
                f"{_KIND_LABEL[kind]}匹配所用的抽取字段置信度低于 "
                f"{LOW_CONFIDENCE_THRESHOLD}，候选只能作为人工复核参考",
                location,
                page,
                line_no,
            )
        )

    return EntityMatch(
        kind=kind,
        status=status,
        query_name=name,
        query_code=code,
        candidates=candidates,
        resolved_code=resolved_code,
        location=location,
        page=page,
        line_no=line_no,
        low_confidence_input=low_confidence,
        notes=tuple(notes),
        issues=tuple(issues),
    )


def match_item(
    name: str | None,
    code: str | None,
    master: MasterDataSnapshot,
    *,
    bindings: Mapping[str, str] | None = None,
    location: str = "item",
    page: int | None = None,
    line_no: int | None = None,
    low_confidence: bool = False,
) -> EntityMatch:
    """匹配单个物料；``bindings`` 为「人工已确认的名称 → 编码」。"""

    index, issues = _build_bindings(bindings, location)
    result = _resolve(
        kind=MatchKind.ITEM,
        name=name,
        code=code,
        master_entries=master.items,
        bindings=index,
        location=location,
        page=page,
        line_no=line_no,
        low_confidence=low_confidence,
        stable_basis="code_exact",
        unknown_code_issue="E_ITEM_CODE_UNKNOWN",
        duplicate_issue="E_CODE_AMBIGUOUS",
    )
    if not master.items:
        issues.append(
            _error(
                "E_MASTER_EMPTY",
                "主数据物料表为空，无法匹配（缺失≠零，不生成候选）",
                "master.items",
                None,
                None,
            )
        )
    return _with_issues(result, issues)


def match_supplier(
    name: str | None,
    tax_no: str | None,
    master: MasterDataSnapshot,
    *,
    bindings: Mapping[str, str] | None = None,
    location: str = "supplier",
    page: int | None = None,
    line_no: int | None = None,
    low_confidence: bool = False,
) -> EntityMatch:
    """匹配单个供应商；``tax_no`` 是稳定标识，名称只生成候选。"""

    index, issues = _build_bindings(bindings, location)
    result = _resolve(
        kind=MatchKind.SUPPLIER,
        name=name,
        code=tax_no,
        master_entries=master.suppliers,
        bindings=index,
        location=location,
        page=page,
        line_no=line_no,
        low_confidence=low_confidence,
        stable_basis="tax_no_exact",
        unknown_code_issue="E_SUPPLIER_TAX_NO_UNKNOWN",
        duplicate_issue="E_TAX_NO_AMBIGUOUS",
    )
    if not master.suppliers:
        issues.append(
            _error(
                "E_MASTER_SUPPLIERS_EMPTY",
                "主数据供应商表为空，无法匹配（缺失≠零，不生成候选）",
                "master.suppliers",
                None,
                None,
            )
        )
    return _with_issues(result, issues)


def _with_issues(match: EntityMatch, extra: Sequence[MatchIssue]) -> EntityMatch:
    if not extra:
        return match
    return EntityMatch(
        kind=match.kind,
        status=match.status,
        query_name=match.query_name,
        query_code=match.query_code,
        candidates=match.candidates,
        resolved_code=match.resolved_code,
        location=match.location,
        page=match.page,
        line_no=match.line_no,
        low_confidence_input=match.low_confidence_input,
        notes=match.notes,
        issues=(*extra, *match.issues),
    )


def _supplier_location(header: DocHeader) -> str:
    if _text(header.supplier_name) is not None:
        return "header.supplier_name"
    if _text(header.supplier_tax_no) is not None:
        return "header.supplier_tax_no"
    return "header.supplier"


def _item_location(index: int, line: DocLine) -> str:
    if _text(line.item_name) is not None:
        return f"lines[{index}].item_name"
    if _text(line.item_code) is not None:
        return f"lines[{index}].item_code"
    return f"lines[{index}].item"


def _pending(matches: Iterable[EntityMatch]) -> tuple[PendingBinding, ...]:
    """把未自动解析的同名实体合并成一次人工绑定动作。"""

    grouped: dict[tuple[MatchKind, str], list[EntityMatch]] = {}
    for match in matches:
        if match.auto_resolved or match.query_name is None:
            continue
        key = (match.kind, normalize_name(match.query_name))
        if not key[1]:
            continue
        grouped.setdefault(key, []).append(match)

    # 状态越靠前越需要人工介入，用于同名多次出现时取最严格的那个
    priority = {
        MatchStatus.MISSING_INPUT: 0,
        MatchStatus.UNMATCHED: 1,
        MatchStatus.AMBIGUOUS: 2,
        MatchStatus.NEEDS_BINDING: 3,
    }

    pending: list[PendingBinding] = []
    for (kind, normalized), items in sorted(
        grouped.items(), key=lambda pair: (pair[0][0].value, pair[0][1])
    ):
        strictest = min(items, key=lambda item: priority.get(item.status, 99))
        pending.append(
            PendingBinding(
                kind=kind,
                name=strictest.query_name or items[0].query_name or "",
                normalized_name=normalized,
                status=strictest.status,
                locations=tuple(item.location for item in items),
                candidates=strictest.candidates,
            )
        )
    return tuple(pending)


def match_document(
    result: ParseResult,
    master: MasterDataSnapshot,
    *,
    item_bindings: Mapping[str, str] | None = None,
    supplier_bindings: Mapping[str, str] | None = None,
) -> MasterMatchResult:
    """对一份解析结果做供应商 + 明细的主数据匹配（纯函数，不落库）。

    ``item_bindings`` / ``supplier_bindings`` 是**人工已确认**的「名称 → 编码」。
    没有传入的名称一律按首次出现处理（``needs_binding``）；本模块不持久化绑定。
    """

    issues: list[MatchIssue] = []
    item_index, item_binding_issues = _build_bindings(item_bindings, "items")
    supplier_index, supplier_binding_issues = _build_bindings(
        supplier_bindings, "suppliers"
    )
    issues.extend(item_binding_issues)
    issues.extend(supplier_binding_issues)

    if not master.items:
        issues.append(
            _error(
                "E_MASTER_EMPTY",
                "主数据物料表为空，无法匹配（缺失≠零，不生成候选）",
                "master.items",
                None,
                None,
            )
        )
    if not master.suppliers:
        issues.append(
            _error(
                "E_MASTER_SUPPLIERS_EMPTY",
                "主数据供应商表为空，无法匹配（缺失≠零，不生成候选）",
                "master.suppliers",
                None,
                None,
            )
        )

    header = result.header
    supplier_name = _text(header.supplier_name)
    supplier_tax_no = _text(header.supplier_tax_no)
    supplier_match = _resolve(
        kind=MatchKind.SUPPLIER,
        name=supplier_name,
        code=supplier_tax_no,
        master_entries=master.suppliers,
        bindings=supplier_index,
        location=_supplier_location(header),
        page=None,
        line_no=None,
        low_confidence=_is_low_confidence(
            header.supplier_name, header.supplier_tax_no
        ),
        stable_basis="tax_no_exact",
        unknown_code_issue="E_SUPPLIER_TAX_NO_UNKNOWN",
        duplicate_issue="E_TAX_NO_AMBIGUOUS",
    )
    issues.extend(supplier_match.issues)

    line_matches: list[EntityMatch] = []
    for index, line in enumerate(result.lines):
        match = _resolve(
            kind=MatchKind.ITEM,
            name=_text(line.item_name),
            code=_text(line.item_code),
            master_entries=master.items,
            bindings=item_index,
            location=_item_location(index, line),
            page=line.page,
            line_no=line.line_no,
            low_confidence=_is_low_confidence(line.item_name, line.item_code),
            stable_basis="code_exact",
            unknown_code_issue="E_ITEM_CODE_UNKNOWN",
            duplicate_issue="E_CODE_AMBIGUOUS",
        )
        line_matches.append(match)
        issues.extend(match.issues)

    if not result.lines:
        issues.append(
            _error(
                "E_NO_LINES",
                "解析结果没有明细行，无法做物料匹配",
                "lines",
                None,
                None,
            )
        )

    pending = _pending((supplier_match, *line_matches))
    return MasterMatchResult(
        supplier=supplier_match,
        line_matches=tuple(line_matches),
        pending_bindings=pending,
        issues=_dedupe_issues(issues),
        master_source=master.source,
    )


def _dedupe_issues(issues: Iterable[MatchIssue]) -> tuple[MatchIssue, ...]:
    seen: set[tuple[str, str, str]] = set()
    ordered: list[MatchIssue] = []
    for issue in issues:
        key = (issue.code, issue.location, issue.message)
        if key in seen:
            continue
        seen.add(key)
        ordered.append(issue)
    return tuple(ordered)
