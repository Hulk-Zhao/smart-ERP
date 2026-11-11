"""T204：对账批次生命周期与状态机（FR-101 / FR-102 / FR-108）。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

import pytest

from apps.recon.batch import (
    BATCH_STATES,
    Batch,
    BatchNotFoundError,
    BatchService,
    BatchState,
    BatchValidationError,
    ClosedBatchError,
    ConclusionSnapshot,
    GenerationRequest,
    IllegalBatchTransition,
    MappingIncompleteError,
    OpenDiffError,
    ReopenBlockedError,
    ReopenPermissionError,
    SourceStatus,
    UnclosedBatchError,
    assert_transition,
)

TENANT = UUID("00000000-0000-0000-0000-000000000001")
T0 = datetime(2026, 11, 11, 12, 0, tzinfo=UTC)
T1 = T0 + timedelta(minutes=1)
T2 = T0 + timedelta(minutes=2)
T3 = T0 + timedelta(minutes=3)
T4 = T0 + timedelta(minutes=4)
T5 = T0 + timedelta(minutes=5)
T6 = T0 + timedelta(minutes=6)

SNAPSHOT = ConclusionSnapshot(
    matched=118,
    supplier_lines=120,
    diff_count=2,
    diff_amount=Decimal("12.30"),
    conclusive=True,
    status_reason="差异已确认",
)


def make_request(
    suppliers: tuple[str, ...] = ("SUP-001",),
    **overrides,
) -> GenerationRequest:
    payload = {
        "tenant_id": TENANT,
        "org_id": "ORG-1",
        "period": "2026-10",
        "suppliers": suppliers,
        "system_ids": ("erp", "wms", "sap"),
        "mapping_completeness": Decimal("0.99"),
        "estimated_lines": {supplier: 120 for supplier in suppliers},
    }
    payload.update(overrides)
    return GenerationRequest(**payload)


def all_sources(batch, *, readable: bool = True) -> tuple[SourceStatus, ...]:
    return tuple(SourceStatus(system, readable) for system in batch.system_ids)


def drive_to_closed() -> tuple[BatchService, Batch]:
    service = BatchService()
    batch = service.generate(make_request(), actor="alice", at=T0)[0]
    batch = service.mark_sources(
        batch.batch_id, all_sources(batch), actor="fetcher", at=T1
    )
    batch = service.mark_matched(batch.batch_id, actor="matcher", at=T2)
    batch = service.record_diff_outcome(batch.batch_id, 2, actor="matcher", at=T2)
    batch = service.settle_diffs(batch.batch_id, actor="reviewer", at=T3)
    batch = service.close(batch.batch_id, SNAPSHOT, actor="closer", at=T4)
    return service, batch


def test_state_vocabulary_matches_prd_enumeration() -> None:
    assert BATCH_STATES == (
        "已创建",
        "已取数",
        "已匹配",
        "有差异",
        "已平",
        "待补数据",
        "已关闭",
        "已作废",
    )


def test_generate_52_suppliers_creates_52_unique_batches() -> None:
    suppliers = tuple(f"SUP-{index:03d}" for index in range(52))
    batches = BatchService().generate(make_request(suppliers), actor="alice", at=T0)

    assert len(batches) == 52
    assert len({batch.batch_id for batch in batches}) == 52
    assert len({batch.batch_no for batch in batches}) == 52
    assert all(batch.state is BatchState.CREATED for batch in batches)
    assert all(batch.version == 1 for batch in batches)
    assert all(batch.estimated_lines == 120 for batch in batches)


def test_generate_same_request_is_idempotent() -> None:
    service = BatchService()
    first = service.generate(make_request(("SUP-001", "SUP-002")), actor="alice", at=T0)
    second = service.generate(
        make_request(("SUP-001", "SUP-002")), actor="alice", at=T1
    )

    assert [batch.batch_id for batch in first] == [batch.batch_id for batch in second]


def test_mapping_below_95_percent_blocks_generation() -> None:
    service = BatchService()
    request = make_request(
        mapping_completeness=Decimal("0.94"),
        missing_mappings=("SUP-001:物料 A", "SUP-002:物料 B"),
    )

    with pytest.raises(MappingIncompleteError) as excinfo:
        service.generate(request, actor="alice", at=T0)

    assert excinfo.value.code == "E-DATA-UNBOUND"
    assert "物料 A" in str(excinfo.value)
    assert service.current(TENANT, "ORG-1", "2026-10", "SUP-001") is None


def test_unclosed_batch_conflict_requires_merge_or_void() -> None:
    service = BatchService()
    service.generate(make_request(), actor="alice", at=T0)

    with pytest.raises(UnclosedBatchError) as excinfo:
        service.generate(make_request(system_ids=("erp", "wms")), actor="alice", at=T1)

    assert excinfo.value.code == "E-IDEM-CONFLICT"
    assert "合并或作废" in str(excinfo.value)
    assert excinfo.value.batch.state is BatchState.CREATED


def test_generate_is_atomic_when_one_supplier_conflicts() -> None:
    service = BatchService()
    service.generate(make_request(("SUP-002",)), actor="alice", at=T0)

    with pytest.raises(UnclosedBatchError):
        service.generate(
            make_request(("SUP-001", "SUP-002"), system_ids=("erp", "wms")),
            actor="alice",
            at=T1,
        )

    assert service.current(TENANT, "ORG-1", "2026-10", "SUP-001") is None


def test_void_requires_reason_then_allows_new_batch() -> None:
    service = BatchService()
    original = service.generate(make_request(), actor="alice", at=T0)[0]

    with pytest.raises(BatchValidationError) as excinfo:
        service.void(original.batch_id, reason="  ", actor="alice", at=T1)
    assert excinfo.value.code == "E-DATA-MISSING"

    voided = service.void(
        original.batch_id, reason="供应商换系统", actor="alice", at=T1
    )
    assert voided.state is BatchState.VOID
    assert voided.void_reason == "供应商换系统"

    replacement = service.generate(make_request(), actor="alice", at=T2)[0]
    assert replacement.batch_id != original.batch_id
    assert service.get(original.batch_id).state is BatchState.VOID
    assert service.current(TENANT, "ORG-1", "2026-10", "SUP-001") == replacement


def test_missing_source_enters_waiting_and_blocks_matching() -> None:
    service = BatchService()
    batch = service.generate(make_request(), actor="alice", at=T0)[0]

    waiting = service.mark_sources(
        batch.batch_id,
        (
            SourceStatus("erp", True),
            SourceStatus("wms", False, owner="仓储-王", detail="网关未拉取"),
            SourceStatus("sap", True),
        ),
        actor="fetcher",
        at=T1,
    )

    assert waiting.state is BatchState.MISSING_DATA
    assert [(item.system, item.owner) for item in waiting.missing_sources] == [
        ("wms", "仓储-王")
    ]
    with pytest.raises(IllegalBatchTransition):
        service.mark_matched(batch.batch_id, actor="matcher", at=T2)

    still_waiting = service.mark_sources(
        batch.batch_id,
        (SourceStatus("wms", False, owner="仓储-王"),),
        actor="fetcher",
        at=T2,
    )
    assert still_waiting.state is BatchState.MISSING_DATA

    fetched = service.mark_sources(
        batch.batch_id,
        (SourceStatus(system, True) for system in batch.system_ids),
        actor="fetcher",
        at=T3,
    )
    assert fetched.state is BatchState.DATA_FETCHED
    assert fetched.missing_sources == ()


def test_match_outcome_goes_to_diff_then_balanced() -> None:
    service = BatchService()
    batch = service.generate(make_request(), actor="alice", at=T0)[0]
    service.mark_sources(batch.batch_id, all_sources(batch), actor="fetcher", at=T1)
    matched = service.mark_matched(batch.batch_id, actor="matcher", at=T2)
    assert matched.state is BatchState.MATCHED

    with_diff = service.record_diff_outcome(batch.batch_id, 3, actor="matcher", at=T2)
    assert with_diff.state is BatchState.HAS_DIFF
    assert with_diff.open_diff_count == 3

    with pytest.raises(OpenDiffError):
        service.close(batch.batch_id, SNAPSHOT, actor="closer", at=T3)

    balanced = service.settle_diffs(batch.batch_id, actor="reviewer", at=T3)
    assert balanced.state is BatchState.BALANCED
    assert balanced.open_diff_count == 0


def test_close_freezes_conclusion_and_rejects_writes() -> None:
    service, closed = drive_to_closed()

    assert closed.state is BatchState.CLOSED
    assert closed.conclusion is not None
    assert closed.conclusion.version == 1
    assert closed.conclusion.matched == 118
    assert closed.conclusion.diff_amount == Decimal("12.30")
    assert closed.conclusion.status_reason == "差异已确认"

    with pytest.raises(IllegalBatchTransition):
        service.mark_sources(closed.batch_id, all_sources(closed), actor="x", at=T5)
    with pytest.raises(ClosedBatchError):
        service.void(closed.batch_id, reason="作废", actor="x", at=T5)


def test_close_requires_balanced_state() -> None:
    service = BatchService()
    batch = service.generate(make_request(), actor="alice", at=T0)[0]

    with pytest.raises(IllegalBatchTransition):
        service.close(batch.batch_id, SNAPSHOT, actor="closer", at=T1)


def test_reopen_guards() -> None:
    service, closed = drive_to_closed()
    common = {
        "actor": "senior",
        "at": T5,
        "senior": True,
        "approver": "director",
    }

    with pytest.raises(BatchValidationError):
        service.reopen(closed.batch_id, reason=" ", **common)
    with pytest.raises(ReopenPermissionError):
        service.reopen(
            closed.batch_id,
            reason="上游修正",
            senior=False,
            actor="senior",
            at=T5,
            approver="director",
        )
    with pytest.raises(ReopenPermissionError):
        service.reopen(
            closed.batch_id,
            reason="上游修正",
            senior=True,
            actor="senior",
            at=T5,
            approver=None,
        )
    with pytest.raises(ReopenPermissionError):
        service.reopen(
            closed.batch_id,
            reason="上游修正",
            senior=True,
            actor="senior",
            at=T5,
            approver="alice",
        )
    with pytest.raises(ReopenPermissionError):
        service.reopen(
            closed.batch_id,
            reason="上游修正",
            senior=True,
            actor="senior",
            at=T5,
            approver="senior",
        )
    with pytest.raises(ReopenBlockedError) as draft_error:
        service.reopen(
            closed.batch_id, reason="上游修正", payment_draft_count=1, **common
        )
    assert draft_error.value.code == "E-EXTERNAL-EFFECTIVE"
    with pytest.raises(ReopenBlockedError):
        service.reopen(
            closed.batch_id,
            reason="上游修正",
            external_confirmation_sent=True,
            **common,
        )

    assert service.get(closed.batch_id).state is BatchState.CLOSED


def test_reopen_voids_old_version_and_creates_new_version() -> None:
    service, closed = drive_to_closed()

    reopened = service.reopen(
        closed.batch_id,
        reason="上游价格修正",
        actor="senior",
        at=T5,
        senior=True,
        approver="director",
        input_fingerprint="upstream-v2",
    )

    assert reopened.version == 2
    assert reopened.state is BatchState.CREATED
    assert reopened.batch_no == closed.batch_no
    assert reopened.supersedes == closed.batch_id
    assert reopened.created_by == "senior"
    assert reopened.input_fingerprint == "upstream-v2"

    old = service.get(closed.batch_id)
    assert old.state is BatchState.VOID
    assert old.void_reason == "重开：上游价格修正"
    assert old.conclusion is not None
    assert old.conclusion.version == 1

    history = service.history(closed.batch_no)
    assert [item.version for item in history] == [1, 2]
    assert [item.state for item in history] == [
        BatchState.VOID,
        BatchState.CREATED,
    ]
    assert service.current(TENANT, "ORG-1", "2026-10", "SUP-001") == reopened


def test_reopen_only_from_closed() -> None:
    service = BatchService()
    batch = service.generate(make_request(), actor="alice", at=T0)[0]

    with pytest.raises(IllegalBatchTransition):
        service.reopen(
            batch.batch_id,
            reason="未关闭",
            actor="senior",
            at=T1,
            senior=True,
            approver="director",
        )


def test_assert_transition_rejects_illegal_edges() -> None:
    assert_transition(BatchState.CREATED, BatchState.DATA_FETCHED)
    with pytest.raises(IllegalBatchTransition):
        assert_transition(BatchState.CREATED, BatchState.MATCHED)
    with pytest.raises(IllegalBatchTransition):
        assert_transition(BatchState.VOID, BatchState.CREATED)


def test_closed_batch_cannot_be_voided_without_reopen() -> None:
    _, closed = drive_to_closed()

    with pytest.raises(IllegalBatchTransition) as excinfo:
        closed.transition(BatchState.VOID, actor="x", at=T5, reason="直接作废")

    assert "reopen()" in str(excinfo.value)


def test_generate_after_close_returns_closed_batch_or_requires_reopen() -> None:
    service, closed = drive_to_closed()

    duplicate = service.generate(make_request(), actor="alice", at=T6)[0]
    assert duplicate.batch_id == closed.batch_id
    assert duplicate.state is BatchState.CLOSED

    with pytest.raises(ClosedBatchError):
        service.generate(make_request(system_ids=("erp", "wms")), actor="alice", at=T6)


def test_generation_request_validation() -> None:
    with pytest.raises(BatchValidationError):
        make_request(())
    with pytest.raises(BatchValidationError):
        make_request(("SUP-001", "SUP-001"))
    with pytest.raises(BatchValidationError):
        make_request(mapping_completeness=Decimal("1.2"))
    with pytest.raises(BatchValidationError):
        BatchService().generate(
            make_request(), actor="alice", at=datetime(2026, 11, 11)
        )


def test_unknown_batch_raises_not_found() -> None:
    with pytest.raises(BatchNotFoundError) as excinfo:
        BatchService().get(UUID("00000000-0000-0000-0000-000000000099"))
    assert excinfo.value.code == "E-RESOURCE-NOTFOUND"
