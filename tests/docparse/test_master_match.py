"""主数据匹配 Top3 与首次出现人工绑定（FR-403 / FR-1404）。

覆盖：稳定标识自动解析、名称首次出现待绑定、已确认绑定复用、并列歧义、
未知编码不改绑、编码/名称冲突、缺失输入、无候选、空主数据、重复主数据、
绑定目标不存在、绑定配置冲突、低置信输入、行定位与无明细行。
"""

from __future__ import annotations

from apps.docparse import (
    DocHeader,
    DocLine,
    DocType,
    MatchKind,
    MatchStatus,
    ParseResult,
    ParseStatus,
    match_document,
    match_item,
)
from apps.docparse.models import StrField
from apps.masterdata import Item, MasterDataSnapshot, Supplier


def _str(value: str | None, confidence: float = 0.98) -> StrField:
    if value is None:
        return StrField(value=None, confidence=0.0)
    return StrField(value=value, confidence=confidence)


def _line(
    name: str | None = None,
    code: str | None = None,
    *,
    page: int = 1,
    line_no: int | None = 1,
    name_confidence: float = 0.98,
) -> DocLine:
    return DocLine(
        page=page,
        line_no=line_no,
        item_name=_str(name, name_confidence),
        item_code=_str(code),
    )


def _master(
    items: tuple[Item, ...] = (),
    suppliers: tuple[Supplier, ...] = (),
) -> MasterDataSnapshot:
    return MasterDataSnapshot(
        org_id="ORG-1",
        as_of_period="2026-10",
        source="tests/docparse/test_master_match.py",
        items=items,
        suppliers=suppliers,
    )


def _result(
    lines: list[DocLine],
    *,
    supplier_name: str | None = None,
    supplier_tax_no: str | None = None,
) -> ParseResult:
    return ParseResult(
        file_name="statement.csv",
        file_kind="csv",
        doc_type=DocType.STATEMENT,
        doc_type_confidence=0.95,
        status=ParseStatus.OK,
        header=DocHeader(
            supplier_name=_str(supplier_name),
            supplier_tax_no=_str(supplier_tax_no),
        ),
        lines=lines,
        parser="tests",
    )


def _codes(match) -> set[str]:
    return {issue.code for issue in match.issues}


ITEMS = (
    Item(code="I-001", name="六角螺栓 M8*30", base_unit="个"),
    Item(code="I-002", name="六角螺栓 M8*35", base_unit="个"),
    Item(code="I-003", name="六角螺栓 M10*30", base_unit="个"),
    Item(code="I-004", name="内六角螺栓 M8*30", base_unit="个"),
    Item(code="I-005", name="平垫圈 M8", base_unit="个"),
)


def test_exact_code_auto_matches_and_keeps_top3():
    result = match_document(
        _result([_line(name="六角螺栓 M8*30", code="I-001")]), _master(items=ITEMS)
    )
    match = result.line_matches[0]

    assert match.status is MatchStatus.AUTO_MATCHED
    assert match.resolved_code == "I-001"
    assert match.auto_resolved
    assert match.candidates[0].code == "I-001"
    assert match.candidates[0].basis == "code_exact"
    assert len(match.candidates) == 3
    scores = [candidate.score for candidate in match.candidates]
    assert scores == sorted(scores, reverse=True)


def test_name_only_requires_manual_binding_first_time():
    result = match_document(_result([_line(name="平垫圈 M8")]), _master(items=ITEMS))
    match = result.line_matches[0]

    assert match.status is MatchStatus.NEEDS_BINDING
    assert match.resolved_code is None
    assert not match.auto_resolved
    assert match.candidates[0].code == "I-005"
    assert match.candidates[0].basis == "name_exact"
    assert "E_PENDING_BINDING" in _codes(match)

    assert result.requires_manual_binding
    assert len(result.pending_bindings) == 1
    pending = result.pending_bindings[0]
    assert pending.kind is MatchKind.ITEM
    assert pending.occurrences == 1


def test_confirmed_binding_is_reused():
    result = match_document(
        _result([_line(name="平垫圈 M8")]),
        _master(items=ITEMS),
        item_bindings={"平垫圈 M8": "I-005"},
    )
    match = result.line_matches[0]

    assert match.status is MatchStatus.BOUND
    assert match.resolved_code == "I-005"
    assert match.auto_resolved
    assert match.candidates[0].basis == "binding"
    assert not result.requires_manual_binding


def test_duplicate_master_names_are_ambiguous():
    master = _master(
        items=(Item(code="I-A", name="冷轧钢板"), Item(code="I-B", name="冷轧钢板"))
    )
    match = match_document(_result([_line(name="冷轧钢板")]), master).line_matches[0]

    assert match.status is MatchStatus.AMBIGUOUS
    assert match.resolved_code is None
    assert {candidate.code for candidate in match.candidates[:2]} == {"I-A", "I-B"}
    assert "E_AMBIGUOUS" in _codes(match)


def test_duplicate_master_codes_are_ambiguous():
    master = _master(
        items=(Item(code="I-A", name="冷轧钢板"), Item(code="I-A", name="热轧钢板"))
    )
    match = match_document(
        _result([_line(name="冷轧钢板", code="I-A")]), master
    ).line_matches[0]

    assert match.status is MatchStatus.AMBIGUOUS
    assert match.resolved_code is None
    assert "E_CODE_AMBIGUOUS" in _codes(match)


def test_unknown_code_never_falls_back_silently():
    match = match_document(
        _result([_line(name="平垫圈 M8", code="I-999")]), _master(items=ITEMS)
    ).line_matches[0]

    assert match.status is MatchStatus.NEEDS_BINDING
    assert match.resolved_code is None
    assert "E_ITEM_CODE_UNKNOWN" in _codes(match)
    assert match.candidates[0].code == "I-005"


def test_unknown_code_with_binding_is_not_auto_resolved():
    match = match_document(
        _result([_line(name="平垫圈 M8", code="I-999")]),
        _master(items=ITEMS),
        item_bindings={"平垫圈 M8": "I-005"},
    ).line_matches[0]

    assert match.status is MatchStatus.AMBIGUOUS
    assert match.resolved_code is None
    assert "E_UNKNOWN_CODE_WITH_BINDING" in _codes(match)


def test_code_and_name_conflict_is_flagged_but_code_wins():
    master = _master(
        items=(Item(code="I-A", name="六角螺栓"), Item(code="I-B", name="冷轧钢板"))
    )
    match = match_document(
        _result([_line(name="冷轧钢板", code="I-A")]), master
    ).line_matches[0]

    assert match.status is MatchStatus.AUTO_MATCHED
    assert match.resolved_code == "I-A"
    assert "W_CODE_NAME_CONFLICT" in _codes(match)


def test_missing_name_and_code_is_missing_input_not_zero():
    match = match_document(_result([_line()]), _master(items=ITEMS)).line_matches[0]

    assert match.status is MatchStatus.MISSING_INPUT
    assert match.candidates == ()
    assert match.resolved_code is None
    assert match.query_name is None
    assert "E_MISSING_INPUT" in _codes(match)


def test_blank_name_is_treated_as_missing():
    match = match_document(
        _result([_line(name="   ")]), _master(items=ITEMS)
    ).line_matches[0]

    assert match.status is MatchStatus.MISSING_INPUT
    assert match.candidates == ()
    assert "E_MISSING_INPUT" in _codes(match)


def test_no_candidate_is_unmatched():
    master = _master(items=(Item(code="I-A", name="冷轧钢板"),))
    match = match_document(
        _result([_line(name="完全无关的物料XYZ")]), master
    ).line_matches[0]

    assert match.status is MatchStatus.UNMATCHED
    assert match.candidates == ()
    assert "E_NO_CANDIDATE" in _codes(match)


def test_empty_master_is_explicit():
    result = match_document(_result([_line(name="冷轧钢板")]), _master())
    match = result.line_matches[0]

    assert match.status is MatchStatus.UNMATCHED
    assert "E_NO_CANDIDATE" in _codes(match)
    assert "E_MASTER_EMPTY" in {issue.code for issue in result.issues}
    assert "E_MASTER_SUPPLIERS_EMPTY" in {issue.code for issue in result.issues}


def test_supplier_tax_no_auto_matches():
    master = _master(
        items=(Item(code="I-A", name="冷轧钢板"),),
        suppliers=(
            Supplier(
                code="S-1", name="蓝票科技有限公司", tax_no="9131 0000 X"
            ),
        ),
    )
    result = match_document(
        _result(
            [_line(name="冷轧钢板")],
            supplier_name="蓝票科技有限公司",
            supplier_tax_no="91310000X",
        ),
        master,
    )

    assert result.supplier.status is MatchStatus.AUTO_MATCHED
    assert result.supplier.resolved_code == "S-1"
    assert result.supplier.candidates[0].basis == "tax_no_exact"


def test_supplier_name_only_needs_binding():
    master = _master(
        items=(Item(code="I-A", name="冷轧钢板"),),
        suppliers=(Supplier(code="S-1", name="蓝票科技有限公司"),),
    )
    result = match_document(
        _result([_line(name="冷轧钢板")], supplier_name="蓝票科技有限公司"), master
    )

    assert result.supplier.status is MatchStatus.NEEDS_BINDING
    assert result.supplier.resolved_code is None
    supplier_pending = next(
        pending
        for pending in result.pending_bindings
        if pending.kind is MatchKind.SUPPLIER
    )
    assert supplier_pending.name == "蓝票科技有限公司"


def test_missing_supplier_is_reported():
    master = _master(
        items=(Item(code="I-A", name="冷轧钢板"),),
        suppliers=(Supplier(code="S-1", name="蓝票科技有限公司"),),
    )
    result = match_document(_result([_line(name="冷轧钢板")]), master)

    assert result.supplier.status is MatchStatus.MISSING_INPUT
    assert result.supplier.location == "header.supplier"
    assert "E_MISSING_INPUT" in _codes(result.supplier)


def test_same_new_name_is_one_pending_binding():
    master = _master(
        items=(Item(code="I-A", name="冷轧钢板"),),
        suppliers=(Supplier(code="S-1", name="蓝票科技有限公司"),),
    )
    result = match_document(
        _result(
            [
                _line(name="冷轧钢板", page=1, line_no=3),
                _line(name="冷轧 钢板", page=2, line_no=8),
            ]
        ),
        master,
    )

    assert len(result.pending_bindings) == 1
    pending = result.pending_bindings[0]
    assert pending.occurrences == 2
    assert len(pending.locations) == 2
    assert {match.status for match in result.line_matches} == {
        MatchStatus.NEEDS_BINDING
    }


def test_binding_target_unknown_is_not_used():
    match = match_document(
        _result([_line(name="冷轧钢板")]),
        _master(items=(Item(code="I-A", name="冷轧钢板"),)),
        item_bindings={"冷轧钢板": "I-NOT-EXIST"},
    ).line_matches[0]

    assert match.status is MatchStatus.NEEDS_BINDING
    assert match.resolved_code is None
    assert "E_BINDING_TARGET_UNKNOWN" in _codes(match)


def test_conflicting_bindings_are_rejected():
    match = match_document(
        _result([_line(name="冷轧钢板")]),
        _master(
            items=(Item(code="I-A", name="冷轧钢板"), Item(code="I-B", name="热轧钢板"))
        ),
        item_bindings={"冷轧钢板": "I-A", "冷轧 钢板": "I-B"},
    ).line_matches[0]

    assert match.status is MatchStatus.NEEDS_BINDING
    assert match.resolved_code is None
    assert "E_BINDING_CONFLICT" in _codes(match)


def test_low_confidence_input_is_flagged():
    match = match_document(
        _result([_line(name="冷轧钢板", name_confidence=0.70)]),
        _master(items=(Item(code="I-A", name="冷轧钢板"),)),
    ).line_matches[0]

    assert match.low_confidence_input
    assert "W_LOW_CONFIDENCE_INPUT" in _codes(match)


def test_location_keeps_page_and_line_no():
    master = _master(
        items=(Item(code="I-A", name="冷轧钢板"),),
        suppliers=(Supplier(code="S-1", name="蓝票科技有限公司"),),
    )
    match = match_document(
        _result([_line(name="冷轧钢板", page=2, line_no=17)]), master
    ).line_matches[0]

    assert match.location == "lines[0].item_name"
    assert (match.page, match.line_no) == (2, 17)
    issue = next(issue for issue in match.issues if issue.code == "E_PENDING_BINDING")
    assert (issue.page, issue.line_no) == (2, 17)
    assert "第 2 页" in issue.message
    assert "行号 17" in issue.message


def test_no_lines_reports_e_no_lines():
    result = match_document(
        _result([]), _master(items=(Item(code="I-A", name="冷轧钢板"),))
    )

    assert result.line_matches == ()
    assert result.unresolved_count == 0
    assert not result.is_fully_resolved
    assert "E_NO_LINES" in {issue.code for issue in result.issues}


def test_end_to_end_parse_then_match(pipeline, fixtures_dir):
    """真实夹具：TXT 解析 → 主数据匹配（库内调用，不经过 API）。"""

    parsed = pipeline.parse_file(fixtures_dir / "aligned_statement.txt")
    master = _master(
        items=(
            Item(code="WL-1001", name="不锈钢 螺钉M6"),
            Item(code="WL-2002", name="平垫片"),
        ),
        suppliers=(Supplier(code="SUP-0003", name="东莞市恒泰五金制品有限公司"),),
    )

    result = match_document(parsed, master)

    assert [match.status for match in result.line_matches] == [
        MatchStatus.AUTO_MATCHED,
        MatchStatus.AUTO_MATCHED,
    ]
    assert [match.resolved_code for match in result.line_matches] == [
        "WL-1001",
        "WL-2002",
    ]
    # 契约里的供应商只有名称/税号，没有「供应商编码」；文件中的 SUP-0003 不参与匹配，
    # 所以名称只能给候选，首次出现仍需人工绑定。
    assert result.supplier.status is MatchStatus.NEEDS_BINDING
    assert result.supplier.resolved_code is None
    assert result.requires_manual_binding


def test_top3_is_deterministic():
    master = _master(items=ITEMS)
    first = match_item("六角螺栓 M8*30", None, master)
    second = match_item("六角螺栓 M8*30", None, master)

    assert first.candidates == second.candidates
    assert len(first.candidates) == 3
    assert [c.code for c in first.candidates] == [
        c.code for c in second.candidates
    ]
    ordered = sorted(first.candidates, key=lambda c: (-c.score, c.code))
    assert list(first.candidates) == ordered
