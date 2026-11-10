"""写入等级取严：动作/场景/连接器/熔断四路取最小，W3 只拒绝。"""

from __future__ import annotations

import pytest

from apps.connectors.errors import ConnectorError, ErrorKind
from apps.connectors.u8.actions import PO_CREATE_DRAFT
from apps.platform.policy.gateway import ForbiddenWriteError
from apps.platform.policy.levels import (
    ActionSpec,
    SceneSpec,
    WriteLevel,
    effective_level,
)
from tests.connectors.support import make_stack, po_payload, post_calls, run


def test_effective_level_takes_minimum() -> None:
    assert (
        effective_level(
            scene_cap=WriteLevel.W1,
            connector_cap=WriteLevel.W2,
            action_level=WriteLevel.W1,
            breaker_cap=WriteLevel.W2,
        )
        is WriteLevel.W1
    )
    # 配置只能降级：动作是 W2，场景上限 W1 → W1
    assert (
        effective_level(
            scene_cap=WriteLevel.W1,
            connector_cap=WriteLevel.W2,
            action_level=WriteLevel.W2,
            breaker_cap=WriteLevel.W2,
        )
        is WriteLevel.W1
    )


def test_w3_is_absorbing() -> None:
    assert (
        effective_level(
            scene_cap=WriteLevel.W0,
            connector_cap=WriteLevel.W2,
            action_level=WriteLevel.W3,
            breaker_cap=WriteLevel.W2,
        )
        is WriteLevel.W3
    )


def test_gateway_rejects_w3_action_with_zero_calls() -> None:
    backend, stack = make_stack()
    forbidden_action = ActionSpec(
        key="u8.po.post_voucher",
        level=WriteLevel.W3,
        writable_fields=frozenset({"source_doc_no"}),
    )

    with pytest.raises(ForbiddenWriteError):
        run(
            stack.gateway.execute(
                forbidden_action,
                {"source_doc_no": "PO202610310001"},
                ticket_id="T-9",
                idem_key="idem-9",
                scene=SceneSpec(code="AG-9", max_level=WriteLevel.W2),
            )
        )
    assert backend.calls == []


def test_scene_cap_downgrades_effective_level() -> None:
    _, stack = make_stack()

    result = run(
        stack.gateway.execute(
            PO_CREATE_DRAFT,
            po_payload(),
            ticket_id="T-1",
            idem_key="idem-1",
            scene=SceneSpec(code="AG-1", max_level=WriteLevel.W0),
        )
    )
    assert result.level is WriteLevel.W0


def test_experimental_connector_caps_at_w0_and_cannot_write() -> None:
    backend, stack = make_stack(experimental=True)

    assert stack.connector.connector_cap() is WriteLevel.W0
    with pytest.raises(ConnectorError) as caught:
        run(
            stack.gateway.execute(
                PO_CREATE_DRAFT,
                po_payload(),
                ticket_id="T-1",
                idem_key="idem-1",
                scene=SceneSpec(code="AG-2", max_level=WriteLevel.W2),
            )
        )
    assert caught.value.kind is ErrorKind.FORBIDDEN
    assert post_calls(backend) == []


def test_open_breaker_means_zero_writes() -> None:
    backend, stack = make_stack()
    for _ in range(3):
        stack.breaker.record_failure()
    assert stack.breaker.write_cap() is WriteLevel.W0

    with pytest.raises(ConnectorError) as caught:
        run(
            stack.gateway.execute(
                PO_CREATE_DRAFT,
                po_payload(),
                ticket_id="T-1",
                idem_key="idem-1",
                scene=SceneSpec(code="AG-2", max_level=WriteLevel.W2),
            )
        )
    assert caught.value.kind is ErrorKind.CIRCUIT_OPEN
    assert post_calls(backend) == []
