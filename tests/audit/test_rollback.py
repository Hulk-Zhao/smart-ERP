"""回滚链路：W3 拒绝、经 WriteGateway、反向顺序、回滚留审计（FR-1302~1305）。"""

from __future__ import annotations

from uuid import uuid4

import pytest

from apps.audit.ledger import AuditLedger, AuditQuery
from apps.audit.rollback import (
    RollbackForbidden,
    RollbackKind,
    RollbackPlanner,
    RollbackRejected,
)
from apps.platform.policy.gateway import ApprovalRequiredError, DenyAllApprovals
from apps.platform.policy.levels import (
    ActionSpec,
    CompensateSpec,
    VerifySpec,
    WriteLevel,
)
from apps.platform.policy.registry import ActionRegistry
from audit_support import (
    TICKET,
    TENANT,
    FakeConnector,
    build_service,
    draft_action,
    run,
    seed_step,
)

VERIFY = VerifySpec(kind="read_by_business_key", fields=("draft_id",))


class StubRegistry:
    """允许注入 W3 动作，专门验证「反向动作 W3」被拒绝（生产注册表注册不进来）。"""

    def __init__(self, specs: dict[str, ActionSpec]) -> None:
        self._specs = specs

    def get(self, key: str) -> ActionSpec:
        if key not in self._specs:
            raise KeyError(key)
        return self._specs[key]


def _registry(action: ActionSpec) -> ActionRegistry:
    registry = ActionRegistry()
    registry.register(action)
    return registry


def test_w3_original_refuses_whole_rollback_without_any_write():
    connector = FakeConnector()
    ledger = AuditLedger()
    action = draft_action()
    steps = [
        run(seed_step(ledger, action=action, step_no=1)),
        run(seed_step(ledger, action=action, step_no=2, level=WriteLevel.W3)),
    ]
    plan = RollbackPlanner(_registry(action)).plan(TICKET, steps)

    assert plan.has_forbidden is True
    assert plan.summary()["one_click_allowed"] is False

    service = build_service(ledger, connector)
    with pytest.raises(RollbackForbidden):
        run(service.execute(plan))
    assert connector.writes == []
    assert connector.previews == []


def test_rollback_goes_through_gateway_and_appends_audit_image():
    connector = FakeConnector()
    ledger = AuditLedger()
    action = draft_action()
    step = run(seed_step(ledger, action=action, step_no=1, object_id="D-1"))
    plan = RollbackPlanner(_registry(action)).plan(TICKET, [step])
    assert plan.items[0].kind is RollbackKind.AUTO

    result = run(build_service(ledger, connector).execute(plan))

    assert result.rolled_back is True
    assert len(connector.writes) == 1
    action_key, payload, ticket_id, idem_key = connector.writes[0]
    assert action_key == "fake.invoice.delete_draft"
    assert payload == {"draft_id": "D-1"}
    assert ticket_id == str(TICKET)
    assert idem_key == "idem-1:undo"

    images = run(ledger.list_images(AuditQuery(tenant_id=TENANT, ticket_id=TICKET)))
    assert len(images) == 2
    assert images[-1].object_type == "invoice_draft:rollback"
    assert images[-1].step_id == step.step_id
    assert run(ledger.verify(TENANT)).ok is True


def test_explicit_compensation_action_is_used():
    connector = FakeConnector()
    ledger = AuditLedger()
    delete_draft = ActionSpec(
        key="fake.invoice.remove_draft",
        level=WriteLevel.W1,
        writable_fields=frozenset({"draft_id"}),
        compensation=CompensateSpec(kind="release_reservation"),
        verify=VERIFY,
    )
    create = ActionSpec(
        key="fake.invoice.create_draft",
        level=WriteLevel.W1,
        writable_fields=frozenset({"draft_id", "note"}),
        compensation=CompensateSpec(
            kind="delete_draft", action_key="fake.invoice.remove_draft"
        ),
        verify=VERIFY,
    )
    registry = ActionRegistry()
    registry.register(delete_draft)
    registry.register(create)
    step = run(seed_step(ledger, action=create, step_no=1))

    plan = RollbackPlanner(registry).plan(TICKET, [step])
    result = run(build_service(ledger, connector).execute(plan))

    assert result.executed[0].item.action.key == "fake.invoice.remove_draft"  # type: ignore[union-attr]
    assert connector.writes[0][0] == "fake.invoice.remove_draft"


def test_rollback_runs_in_reverse_step_order():
    connector = FakeConnector()
    ledger = AuditLedger()
    action = draft_action()
    steps = [
        run(seed_step(ledger, action=action, step_no=1, object_id="D-1")),
        run(seed_step(ledger, action=action, step_no=2, object_id="D-2")),
    ]
    plan = RollbackPlanner(_registry(action)).plan(TICKET, steps)

    result = run(build_service(ledger, connector).execute(plan))

    assert [payload["draft_id"] for _, payload, _, _ in connector.writes] == [
        "D-2",
        "D-1",
    ]
    assert [executed.item.step.step_no for executed in result.executed] == [2, 1]


def test_manual_compensation_requires_confirmation():
    connector = FakeConnector()
    ledger = AuditLedger()
    action = draft_action(compensation="red_invoice")
    step = run(seed_step(ledger, action=action, step_no=1))
    plan = RollbackPlanner(_registry(action)).plan(TICKET, [step])
    assert plan.items[0].kind is RollbackKind.MANUAL

    service = build_service(ledger, connector)
    with pytest.raises(RollbackRejected):
        run(service.execute(plan))
    assert connector.writes == []

    result = run(service.execute(plan, confirmed_manual=True))
    assert result.rolled_back is True
    assert len(connector.writes) == 1


@pytest.mark.parametrize(
    ("inverse_level", "reason"),
    [
        (WriteLevel.W3, "反向动作是 W3"),
        (WriteLevel.W2, "高于原动作"),
    ],
)
def test_inverse_action_w3_or_level_upgrade_is_forbidden(inverse_level, reason):
    ledger = AuditLedger()
    original = ActionSpec(
        key="fake.invoice.create_draft",
        level=WriteLevel.W1,
        writable_fields=frozenset({"draft_id"}),
        compensation=CompensateSpec(
            kind="post_voucher", action_key="fake.invoice.post_voucher"
        ),
        verify=VERIFY,
    )
    inverse = ActionSpec(
        key="fake.invoice.post_voucher",
        level=inverse_level,
        writable_fields=frozenset({"draft_id"}),
        verify=VERIFY,
    )
    step = run(seed_step(ledger, action=original, step_no=1))
    plan = RollbackPlanner(
        StubRegistry({original.key: original, inverse.key: inverse})
    ).plan(TICKET, [step])

    assert plan.has_forbidden is True
    assert reason in plan.items[0].reason

    connector = FakeConnector()
    with pytest.raises(RollbackForbidden):
        run(build_service(ledger, connector).execute(plan))
    assert connector.writes == []


def test_uncompensatable_step_becomes_manual_task_with_audit():
    connector = FakeConnector()
    ledger = AuditLedger()
    readonly = ActionSpec(
        key="fake.invoice.readonly_note",
        level=WriteLevel.W0,
        writable_fields=frozenset(),
        compensation=None,
        verify=VERIFY,
    )
    step = run(seed_step(ledger, action=readonly, step_no=1))
    plan = RollbackPlanner(_registry(readonly)).plan(TICKET, [step])
    assert plan.impossible_items[0].kind is RollbackKind.IMPOSSIBLE

    result = run(build_service(ledger, connector).execute(plan))

    assert result.executed == ()
    assert len(result.manual_tasks) == 1
    assert result.manual_tasks[0].requires_owner is True
    assert connector.writes == []

    images = run(ledger.list_images(AuditQuery(tenant_id=TENANT, ticket_id=TICKET)))
    assert len(images) == 2
    assert images[-1].object_type == "invoice_draft:manual_task"
    assert run(ledger.verify(TENANT)).ok is True


def test_rollback_still_obeys_gateway_approval_gate():
    connector = FakeConnector()
    ledger = AuditLedger()
    action = draft_action()
    step = run(seed_step(ledger, action=action, step_no=1))
    plan = RollbackPlanner(_registry(action)).plan(TICKET, [step])
    service = build_service(ledger, connector, approvals=DenyAllApprovals())

    with pytest.raises(ApprovalRequiredError):
        run(service.execute(plan))
    assert connector.writes == []
    # 没有绕过网关直接写连接器，也没有产生「成功回滚」审计
    images = run(ledger.list_images(AuditQuery(tenant_id=TENANT, ticket_id=TICKET)))
    assert len(images) == 1


def test_step_write_rejects_empty_idempotency_inputs():
    from apps.audit.rollback import StepWrite

    with pytest.raises(ValueError):
        StepWrite(
            step_id=uuid4(),
            step_no=1,
            action_key="fake.invoice.create_draft",
            level=WriteLevel.W1,
            idem_key=" ",
            audit_image=run(
                AuditLedger().append_image(
                    tenant_id=TENANT,
                    ticket_id=TICKET,
                    step_id=uuid4(),
                    system="fake",
                    object_type="x",
                    object_id="x-1",
                    before={},
                    after={},
                )
            ),
        )
