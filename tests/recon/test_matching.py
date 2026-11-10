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
