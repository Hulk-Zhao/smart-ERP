"""三层匹配与置信度闸门（FR-103 / PRD §5.0）。"""

from __future__ import annotations

from decimal import Decimal

from apps.recon import match_lines, reconcile
from apps.recon.models import BatchStatus, MatchType


def _outcome(request):
    return match_lines(
        list(request.our_lines), list(request.supplier_lines), request.tolerance
    )


def test_exact_match_by_doc_no(make_request, make_line):
    request = make_request(
        [make_line()],
        [make_line(system="STATEMENT", qty="10", unit_price="5", amount="50")],
    )
    outcome = _outcome(request)

    assert len(outcome.pairs) == 1
    assert outcome.pairs[0].match_type is MatchType.EXACT
    assert outcome.pairs[0].confidence == Decimal("1.00")


def test_fuzzy_match_requires_business_key(make_request, make_line):
    request = make_request(
        [make_line(doc_no="PO-1", qty="100", unit_price="5", amount="500")],
        [
            make_line(
                system="STATEMENT",
                doc_no="INV-1",
                qty="100",
                unit_price="5",
                amount="500",
            )
        ],
    )
    outcome = _outcome(request)

    assert len(outcome.pairs) == 1
    pair = outcome.pairs[0]
    assert pair.match_type is MatchType.FUZZY
    assert pair.confidence >= Decimal("0.70")


def test_close_candidates_mark_ambiguity_without_decision(make_request, make_line):
    request = make_request(
        [
            make_line(doc_no="PO-1", qty="100", amount="500"),
            make_line(doc_no="PO-2", qty="101", amount="505"),
        ],
        [
            make_line(
                system="STATEMENT", doc_no="INV-1", qty="100.5", amount="502.5"
            )
        ],
    )
    outcome = _outcome(request)

    assert outcome.pairs == []
    assert len(outcome.ambiguous) == 1
    supplier, candidates = outcome.ambiguous[0]
    assert supplier.doc_no == "INV-1"
    assert len(candidates) >= 2
    assert candidates[0].confidence - candidates[1].confidence < Decimal("0.10")


def test_low_confidence_stays_candidate_and_never_auto_passes(make_request, make_line):
    request = make_request(
        [make_line(doc_no="PO-1", qty="10", unit_price="5", amount="50")],
        [
            make_line(
                system="STATEMENT",
                doc_no="INV-1",
                qty="1000",
                unit_price="1000",
                amount="1000000",
                period="2026-11",
            )
        ],
    )
    outcome = _outcome(request)

    assert outcome.pairs == []
    assert len(outcome.candidate_only) == 1
    _supplier, candidates = outcome.candidate_only[0]
    assert candidates[0].confidence < Decimal("0.70")
    assert candidates[0].reason == "业务键相似度低"

    result = reconcile(request)
    assert result.batch_status is BatchStatus.NEEDS_REVIEW
    assert result.conclusive is False
    line = next(line for line in result.lines if line.supplier_ref)
    assert line.needs_manual is True
    assert line.diffs == ()


def test_ambiguous_batch_is_not_conclusive(make_request, make_line):
    request = make_request(
        [
            make_line(doc_no="PO-1", qty="100", amount="500"),
            make_line(doc_no="PO-2", qty="101", amount="505"),
        ],
        [
            make_line(
                system="STATEMENT", doc_no="INV-1", qty="100.5", amount="502.5"
            )
        ],
    )
    result = reconcile(request)

    assert result.batch_status is BatchStatus.NEEDS_REVIEW
    assert result.conclusive is False
    assert result.diffs == ()
    assert any(line.status.value == "匹配歧义" for line in result.lines)


def test_unmatched_goes_to_missing_doc(make_request, make_line):
    request = make_request(
        [make_line(item_code="IT-A")],
        [
            make_line(
                system="STATEMENT", doc_no="INV-1", item_code="IT-B", amount="50"
            )
        ],
    )
    outcome = _outcome(request)

    assert outcome.pairs == []
    assert len(outcome.unmatched_supplier) == 1
    assert len(outcome.unmatched_our) == 1

    result = reconcile(request)
    assert [diff.type.value for diff in result.diffs] == ["缺单", "缺单"]
    assert result.metrics.matched == 0


def test_split_invoice_matched_as_aggregated(make_request, make_line):
    """拆单：两行供应商行合计等于我方一行，标成聚合匹配而不是缺单。"""
    request = make_request(
        [make_line(doc_no="PO-1", qty="100", unit_price="5", amount="500")],
        [
            make_line(
                system="STATEMENT", doc_no="INV-1", qty="40", unit_price="5", amount="200"
            ),
            make_line(
                system="STATEMENT", doc_no="INV-2", qty="60", unit_price="5", amount="300"
            ),
        ],
    )
    result = reconcile(request)

    aggregated = [
        line for line in result.lines if line.match_type is MatchType.AGGREGATED
    ]
    assert len(aggregated) == 2
    assert all(line.status.value == "已匹配" for line in aggregated)
    assert {line.our_ref for line in aggregated} == {"ERP:PO-1#1"}
    assert all(len(line.aggregate_refs) == 1 for line in aggregated)
    assert result.diffs == ()
    assert result.batch_status is BatchStatus.RECONCILED
    assert result.metrics.aggregated == 2


def test_merged_billing_stays_manual_with_aggregate_refs(make_request, make_line):
    """合并：一行供应商行覆盖我方两行，标成聚合匹配但强制转人工确认拆分口径。"""
    request = make_request(
        [
            make_line(doc_no="PO-1-1", qty="40", unit_price="5", amount="200"),
            make_line(doc_no="PO-1-2", qty="60", unit_price="5", amount="300"),
        ],
        [
            make_line(
                system="STATEMENT", doc_no="INV-1", qty="100", unit_price="5", amount="500"
            )
        ],
    )
    result = reconcile(request)

    aggregated = [
        line for line in result.lines if line.match_type is MatchType.AGGREGATED
    ]
    assert len(aggregated) == 1
    assert aggregated[0].status.value == "待人工"
    assert len(aggregated[0].aggregate_refs) == 2
    assert result.batch_status is BatchStatus.NEEDS_REVIEW
    assert result.metrics.auto_disposal_allowed is False


def test_uom_converted_before_scoring(make_request, make_line):
    """跨单位：先按主数据换算成同口径再比，数量/单价一致即高分模糊配。"""
    request = make_request(
        [make_line(doc_no="PO-1", qty="100", unit="PCS", unit_price="5", amount="500")],
        [
            make_line(
                system="STATEMENT",
                doc_no="INV-1",
                qty="10",
                unit="BOX",
                unit_price="50",
                amount="500",
            )
        ],
        unit_conversions=[
            {"item": "IT-A", "from_unit": "BOX", "to_unit": "PCS", "factor": "10"}
        ],
    )
    outcome = match_lines(
        list(request.our_lines),
        list(request.supplier_lines),
        request.tolerance,
        converters=_converters(request),
    )

    assert len(outcome.pairs) == 1
    pair = outcome.pairs[0]
    assert pair.match_type is MatchType.FUZZY
    assert pair.confidence == Decimal("1.00")


def test_unconvertible_uom_is_candidate_with_distinct_reason(make_request, make_line):
    """不可换算：封顶在 0.70 以下只给候选，原因写明「不可换算」而非「相似度低」。"""
    request = make_request(
        [make_line(doc_no="PO-1", qty="100", unit="PCS", unit_price="5", amount="500")],
        [
            make_line(
                system="STATEMENT",
                doc_no="INV-1",
                qty="10",
                unit="BOX",
                unit_price="50",
                amount="500",
            )
        ],
    )
    outcome = match_lines(
        list(request.our_lines),
        list(request.supplier_lines),
        request.tolerance,
        converters=_converters(request),
    )

    assert outcome.pairs == []
    assert len(outcome.candidate_only) == 1
    _supplier, candidates = outcome.candidate_only[0]
    assert candidates[0].confidence < Decimal("0.70")
    assert "不可换算" in candidates[0].reason
    assert "[UNCONVERTIBLE]" in candidates[0].reason
    assert "业务键相似度低" not in candidates[0].reason


def test_same_qty_amount_but_different_unit_never_auto_matches(make_request, make_line):
    """P0-1：同单号、数量与金额字面相等，但 100 BOX ≠ 100 PCS，必须转人工。

    直配层以前只看单据号，会把箱当件自动放行；现在单位不闭合一律只给候选。
    """
    request = make_request(
        [
            make_line(
                doc_no="PO-1", qty="100", unit="PCS", unit_price="12.5", amount="1250"
            )
        ],
        [
            make_line(
                system="STATEMENT",
                doc_no="PO-1",
                qty="100",
                unit="BOX",
                unit_price="12.5",
                amount="1250",
            )
        ],
        unit_conversions=[
            {"item": "IT-A", "from_unit": "BOX", "to_unit": "PCS", "factor": "10"}
        ],
    )
    result = reconcile(request)

    assert result.batch_status is BatchStatus.NEEDS_REVIEW
    line = next(item for item in result.lines if item.supplier_ref)
    assert line.status.value == "候选待人工"
    assert line.match_type is not MatchType.EXACT
    assert line.our_ref is None
    assert "单位不一致" in line.candidates[0].reason
    assert "1000" in line.candidates[0].reason


def test_unit_spelling_case_is_still_same_unit(make_request, make_line):
    """同一单位的不同写法（大小写/空白）不算单位不一致，仍可直配。"""
    request = make_request(
        [make_line(doc_no="PO-1", qty="10", unit="PCS", unit_price="5", amount="50")],
        [
            make_line(
                system="STATEMENT",
                doc_no="PO-1",
                qty="10",
                unit=" pcs ",
                unit_price="5",
                amount="50",
            )
        ],
    )
    result = reconcile(request)

    line = next(item for item in result.lines if item.supplier_ref)
    assert line.status.value == "已匹配"
    assert line.match_type is MatchType.EXACT


def _converters(request):
    from apps.recon.masterdata import conversion_factor

    return lambda item, from_unit, to_unit: conversion_factor(
        request.master_data, item, from_unit, to_unit
    )
