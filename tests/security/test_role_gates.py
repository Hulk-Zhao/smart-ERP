"""C1/C2/C3/C5 修复的层内证据（不依赖 HTTP 接线）。

- C1 回滚他人工单：角色 + 理由 + 理由/回滚人进审计；
- C2 只读与导出角色闸（导出留审计在 HTTP 层，见 test_permission_matrix.py）；
- C3 写闸按租约字段白名单拒写；
- C5 复核/审批按场景业务类别限定。

HTTP 端到端证据在 ``apps/api`` 接线完成后补到 ``test_permission_matrix.py``。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from matrix_support import (
    APPROVER,
    ORG,
    PERIOD,
    REVIEWER,
    SCENE,
    SYSTEM,
    TENANT,
    FakeConnector,
    action_forward,
    action_reverse,
    meta_a,
)

from apps.audit.ledger import AuditLedger
from apps.audit.rollback import RollbackPlanner, RollbackService, StepWrite
from apps.audit.store import AuditQuery
from apps.platform.policy.gateway import FieldScopeViolation, WriteGateway
from apps.platform.policy.levels import SceneSpec, WriteLevel
from apps.platform.policy.registry import ActionRegistry
from apps.security.approvals import ApprovalService
from apps.security.identity import ScopeGrant, UserPrincipal
from apps.security.roles import (
    CrossCategoryReviewRejected,
    OtherInitiatorRollbackDenied,
    RoleDenied,
    RollbackReasonRequired,
    UnknownBusinessCategory,
    assert_can_export_audit,
    assert_can_read_ticket,
    assert_can_rollback,
)

INITIATOR = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")


def _principal(user_id: UUID, roles: frozenset[str]) -> UserPrincipal:
    return UserPrincipal(
        tenant_id=TENANT,
        user_id=user_id,
        roles=roles,
        grant=ScopeGrant(
            org_ids=frozenset({ORG}),
            periods=frozenset({PERIOD}),
            system_ids=frozenset({SYSTEM}),
        ),
    )


# --------------------------------------------------------------------------- C2


@pytest.mark.parametrize(
    "role", ["operator", "reviewer", "approver", "cfo", "auditor", "platform_admin"]
)
def test_c2_read_role_allowlist(role):
    assert_can_read_ticket(_principal(uuid4(), frozenset({role})))


def test_c2_service_account_cannot_read_tickets():
    """服务账号只用于执行，不在 PRD §2.3 的只读角色里。"""

    with pytest.raises(RoleDenied):
        assert_can_read_ticket(_principal(uuid4(), frozenset({"service_account"})))


@pytest.mark.parametrize("role", ["platform_admin", "cfo", "auditor"])
def test_c2_export_allowed_for_it_cfo_auditor(role):
    assert_can_export_audit(_principal(uuid4(), frozenset({role})))


@pytest.mark.parametrize("role", ["operator", "reviewer", "approver", "service_account"])
def test_c2_export_denied_for_business_roles(role):
    with pytest.raises(RoleDenied):
        assert_can_export_audit(_principal(uuid4(), frozenset({role})))


# --------------------------------------------------------------------------- C5


def test_c5_reviewer_with_matching_category_is_allowed():
    service = ApprovalService()
    record = service.record(
        ticket_id="C5-OK",
        role="reviewer",
        approver=_principal(REVIEWER, frozenset({"reviewer", "finance"})),
        initiator_id=INITIATOR,
        decision="approved",
        comment="财务类复核",
        scene_code=SCENE,
    )
    assert record.role == "reviewer"


def test_c5_cross_category_review_is_rejected():
    service = ApprovalService()
    with pytest.raises(CrossCategoryReviewRejected):
        service.record(
            ticket_id="C5-CROSS",
            role="reviewer",
            approver=_principal(REVIEWER, frozenset({"reviewer", "procurement"})),
            initiator_id=INITIATOR,
            decision="approved",
            comment="采购类想复核财务类",
            scene_code=SCENE,
        )


def test_c5_unknown_scene_has_no_review_authority():
    service = ApprovalService()
    with pytest.raises(UnknownBusinessCategory):
        service.record(
            ticket_id="C5-UNKNOWN",
            role="reviewer",
            approver=_principal(REVIEWER, frozenset({"reviewer", "finance"})),
            initiator_id=INITIATOR,
            decision="approved",
            comment="未知场景",
            scene_code="scene-not-registered",
        )


def test_c5_new_scene_can_register_category():
    service = ApprovalService()
    service.categories.register("recon_purchase_v2", "procurement")
    record = service.record(
        ticket_id="C5-REG",
        role="reviewer",
        approver=_principal(APPROVER, frozenset({"reviewer", "procurement"})),
        initiator_id=INITIATOR,
        decision="approved",
        comment="新场景登记后按采购类复核",
        scene_code="recon_purchase_v2",
    )
    assert record.decision == "approved"


# --------------------------------------------------------------------------- C1


def test_c1_own_ticket_rollback_needs_no_reason():
    allowed = assert_can_rollback(
        _principal(INITIATOR, frozenset({"operator"})),
        initiator_id=INITIATOR,
        reason="",
    )
    assert allowed is False


def test_c1_other_ticket_rollback_denied_for_business_roles():
    with pytest.raises(OtherInitiatorRollbackDenied):
        assert_can_rollback(
            _principal(uuid4(), frozenset({"operator"})),
            initiator_id=INITIATOR,
            reason="代客回滚",
        )


@pytest.mark.parametrize("role", ["platform_admin", "cfo"])
@pytest.mark.parametrize("reason", ["", "   "])
def test_c1_other_ticket_rollback_requires_non_blank_reason(role, reason):
    with pytest.raises(RollbackReasonRequired):
        assert_can_rollback(
            _principal(uuid4(), frozenset({role})),
            initiator_id=INITIATOR,
            reason=reason,
        )


@pytest.mark.parametrize("role", ["platform_admin", "cfo"])
def test_c1_other_ticket_rollback_allowed_with_reason(role):
    allowed = assert_can_rollback(
        _principal(uuid4(), frozenset({role})),
        initiator_id=INITIATOR,
        reason="客户要求撤回错误草稿",
    )
    assert allowed is True


def _rollback_plan(ledger: AuditLedger) -> tuple[RollbackPlanner, list[StepWrite]]:
    registry = ActionRegistry()
    registry.register(action_forward())
    registry.register(action_reverse())
    step_id = uuid4()
    image = asyncio.run(
        ledger.append_image(
            tenant_id=TENANT,
            ticket_id=uuid4(),
            step_id=step_id,
            system=SYSTEM,
            object_type="u8.trade",
            object_id="D-C1",
            before={"status": "draft"},
            after={"status": "saved"},
            created_at=datetime(2026, 11, 10, 10, 0, tzinfo=UTC),
        )
    )
    step = StepWrite(
        step_id=step_id,
        step_no=1,
        action_key="u8.trade.save_draft",
        level=WriteLevel.W1,
        idem_key="idem-c1",
        audit_image=image,
    )
    return RollbackPlanner(registry), [step]


def test_c1_rollback_reason_and_actor_are_written_to_audit():
    ledger = AuditLedger()
    planner, steps = _rollback_plan(ledger)
    plan = planner.plan(steps[0].audit_image.ticket_id, steps)
    service = RollbackService(
        gateway=WriteGateway(FakeConnector(meta_a()), approvals=_AllowAll()),
        ledger=ledger,
        scene=SceneSpec(code=SCENE, max_level=WriteLevel.W2),
    )

    result = asyncio.run(
        service.execute(
            plan,
            reason="客户要求撤回错误草稿",
            actor_id=UUID("dddddddd-dddd-dddd-dddd-dddddddddddd"),
            actor_roles=("cfo",),
        )
    )

    rollback_image = result.executed[0].audit_image
    assert rollback_image.object_type.endswith(":rollback")

    images = asyncio.run(
        ledger.list_images(
            AuditQuery(tenant_id=TENANT, ticket_id=rollback_image.ticket_id)
        )
    )
    intent = next(
        image for image in images if image.object_type.endswith(":rollback_request")
    )
    assert ledger.snapshots.get(intent.after_ref) == {
        "reason": "客户要求撤回错误草稿",
        "actor_id": "dddddddd-dddd-dddd-dddd-dddddddddddd",
        "actor_roles": ["cfo"],
    }
    assert asyncio.run(ledger.verify(tenant_id=TENANT)).ok is True


def test_c1_rollback_without_reason_keeps_audit_shape():
    """旧调用不传理由时不多写影像，避免影响既有回滚证据链。"""

    ledger = AuditLedger()
    planner, steps = _rollback_plan(ledger)
    plan = planner.plan(steps[0].audit_image.ticket_id, steps)
    service = RollbackService(
        gateway=WriteGateway(FakeConnector(meta_a()), approvals=_AllowAll()),
        ledger=ledger,
        scene=SceneSpec(code=SCENE, max_level=WriteLevel.W2),
    )

    result = asyncio.run(service.execute(plan))

    images = asyncio.run(
        ledger.list_images(
            AuditQuery(
                tenant_id=TENANT, ticket_id=result.executed[0].audit_image.ticket_id
            )
        )
    )
    assert all(not image.object_type.endswith(":rollback_request") for image in images)


# --------------------------------------------------------------------------- C3


class _AllowAll:
    def is_satisfied(self, ticket_id: str, level: WriteLevel) -> bool:
        return True


def _execute(gateway: WriteGateway, payload: dict[str, object]):
    return asyncio.run(
        gateway.execute(
            action_forward(),
            payload,
            ticket_id="C3-TICKET",
            idem_key=f"idem-{uuid4()}",
            scene=SceneSpec(code=SCENE, max_level=WriteLevel.W2),
        )
    )


def test_c3_narrowed_fields_reject_out_of_whitelist_payload():
    connector = FakeConnector(meta_a())
    gateway = WriteGateway(
        connector,
        approvals=_AllowAll(),
        narrowed_fields=frozenset({"draft_id"}),
    )

    with pytest.raises(FieldScopeViolation):
        _execute(gateway, {"draft_id": "D-C3", "note": "白名单外字段"})

    assert connector.previews == []
    assert connector.writes == []


def test_c3_narrowed_fields_allow_subset():
    connector = FakeConnector(meta_a())
    gateway = WriteGateway(
        connector,
        approvals=_AllowAll(),
        narrowed_fields=frozenset({"draft_id"}),
    )

    _execute(gateway, {"draft_id": "D-C3"})

    assert connector.writes[0][1] == {"draft_id": "D-C3"}


def test_c3_a_level_gateway_is_unaffected():
    connector = FakeConnector(meta_a())
    gateway = WriteGateway(connector, approvals=_AllowAll())

    _execute(gateway, {"draft_id": "D-C3", "note": "A 级仍按动作 scope"})

    assert connector.writes[0][1]["note"] == "A 级仍按动作 scope"
