"""U8 连接器（假实现）端到端：读拉单、缺数降级、写回只走 WriteGateway。"""

from __future__ import annotations

import pytest

from apps.connectors.u8.actions import PO_CREATE_DRAFT
from apps.platform.connectors.sdk import (
    MissingIdempotencyKey,
    MissingTicketContext,
    ReadQuery,
)
from apps.platform.policy.gateway import PreviewFailedError
from tests.connectors.support import (
    PO_SCENE,
    make_stack,
    po_payload,
    post_calls,
    run,
)


async def _no_sleep(_seconds: float) -> None:
    return None


def test_read_purchase_orders_degrades_instead_of_raising() -> None:
    _, stack = make_stack()

    result = run(stack.connector.read("purchase_order", ReadQuery()))

    # 3 张单里 1 张缺供应商编码：读到 2 张 + 完整的缺失原因，而不是整体失败或空集
    assert result.completeness == "partial"
    assert len(result.items) == 2
    reasons = " ".join(item.reason for item in result.degraded)
    assert "amount_mismatch" in reasons
    assert "party_code" in reasons


def test_read_single_document_by_business_key() -> None:
    _, stack = make_stack()

    result = run(
        stack.connector.read(
            "purchase_order", ReadQuery(filters={"doc_no": "PO202610310001"})
        )
    )

    assert result.completeness == "complete"
    assert result.items[0]["doc_no"] == "PO202610310001"
    assert result.items[0]["total_amount"]["amount"] == "12680.0000"


def test_read_unavailable_when_downstream_keeps_failing() -> None:
    backend, stack = make_stack(sleep=_no_sleep)
    backend.inject_failure("/u8/api/purchase-orders", 503, times=3)

    result = run(stack.connector.read("purchase_order", ReadQuery()))

    assert result.completeness == "unavailable"
    assert result.items == ()
    assert "read_failed" in result.degraded[0].reason
    # 3 次尝试都打到了 U8（1 次初始 + 2 次重试），然后如实报不可用
    assert len(backend.calls_to("/u8/api/purchase-orders")) == 3


def test_read_typed_marks_master_data_gaps() -> None:
    _, stack = make_stack()
    items = {
        "M-1001": run(stack.connector.read_typed("item", code="M-1001"))[0].record,
        "M-2001": run(stack.connector.read_typed("item", code="M-2001"))[0].record,
    }

    outcomes = run(stack.connector.read_typed("inbound_order", items=items))

    assert len(outcomes) == 2
    assert outcomes[0].record is not None
    assert outcomes[0].blocked is False
    assert "unit_conversion_missing" in outcomes[1].reason_codes()


def test_write_without_ticket_or_idem_key_never_reaches_transport() -> None:
    backend, stack = make_stack()

    with pytest.raises(MissingTicketContext):
        run(
            stack.connector.write(
                PO_CREATE_DRAFT, po_payload(), ticket_id="", idem_key="idem-1"
            )
        )
    with pytest.raises(MissingIdempotencyKey):
        run(
            stack.connector.write(
                PO_CREATE_DRAFT, po_payload(), ticket_id="T-1", idem_key=""
            )
        )
    assert post_calls(backend) == []


def test_write_through_gateway_creates_draft_and_verifies() -> None:
    backend, stack = make_stack()

    result = run(
        stack.gateway.execute(
            PO_CREATE_DRAFT,
            po_payload(),
            ticket_id="T-1",
            idem_key="idem-1",
            scene=PO_SCENE,
        )
    )

    assert result.level.name == "W1"
    assert result.write.external_id == "DPO-0001"
    assert result.write.before_ref == "PO202610310001"
    assert result.verify.status == "match"
    assert result.preview.before == {"exists": False, "source_doc_no": "PO202610310001"}

    posts = post_calls(backend)
    assert len(posts) == 1
    assert posts[0].idempotency_key == "idem-1"
    assert posts[0].headers.get("x-ticket-id") == "T-1"


def test_same_idem_key_replays_without_second_post() -> None:
    backend, stack = make_stack()

    first = run(
        stack.gateway.execute(
            PO_CREATE_DRAFT,
            po_payload(),
            ticket_id="T-1",
            idem_key="idem-1",
            scene=PO_SCENE,
        )
    )
    second = run(
        stack.gateway.execute(
            PO_CREATE_DRAFT,
            po_payload(),
            ticket_id="T-1",
            idem_key="idem-1",
            scene=PO_SCENE,
        )
    )

    assert first.write.external_id == second.write.external_id
    assert len(post_calls(backend)) == 1


def test_preview_without_business_key_means_zero_write() -> None:
    backend, stack = make_stack()
    payload = po_payload()
    payload.pop("source_doc_no")

    with pytest.raises(PreviewFailedError):
        run(
            stack.gateway.execute(
                PO_CREATE_DRAFT,
                payload,
                ticket_id="T-1",
                idem_key="idem-1",
                scene=PO_SCENE,
            )
        )
    assert post_calls(backend) == []


def test_unmappable_payload_never_reaches_u8() -> None:
    backend, stack = make_stack()
    payload = po_payload()
    payload["lines"][0].pop("unit")

    with pytest.raises(Exception) as caught:
        run(
            stack.gateway.execute(
                PO_CREATE_DRAFT,
                payload,
                ticket_id="T-1",
                idem_key="idem-1",
                scene=PO_SCENE,
            )
        )
    assert getattr(caught.value, "kind", None).value == "invalid_request"
    assert post_calls(backend) == []


def test_compensate_deletes_draft_and_is_idempotent() -> None:
    backend, stack = make_stack()
    result = run(
        stack.gateway.execute(
            PO_CREATE_DRAFT,
            po_payload(),
            ticket_id="T-1",
            idem_key="idem-1",
            scene=PO_SCENE,
        )
    )
    external_id = result.write.external_id

    first = run(
        stack.connector.compensate(
            {"action_key": PO_CREATE_DRAFT.key, "external_id": external_id}, idem_key="c-1"
        )
    )
    second = run(
        stack.connector.compensate(
            {"action_key": PO_CREATE_DRAFT.key, "external_id": external_id}, idem_key="c-2"
        )
    )

    assert first.status == "compensated"
    assert second.status == "unchanged"
    assert backend.drafts["purchase-order-drafts"] == {}


def test_health_reports_breaker_and_rate_limit() -> None:
    _, stack = make_stack()
    run(stack.connector.read("supplier", ReadQuery()))

    health = run(stack.connector.health())

    assert health.ok is True
    assert health.last_sync_at is not None
    assert health.failure_rate == 0.0
    assert 0.0 <= health.rate_limit_remaining <= 1.0
    assert health.version_fingerprint.startswith("sha256:")
