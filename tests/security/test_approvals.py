"""审批阈值与职责分离：自审被拒、同人多角色被拒、金额分级（FR-1403 / G-04）。"""

from __future__ import annotations

from decimal import Decimal

import pytest
from security_support import (
    ADMIN,
    APPROVER,
    CFO,
    INITIATOR,
    NOW,
    REVIEWER,
    principal,
)

from apps.platform.policy.levels import WriteLevel
from apps.security.approvals import (
    ApprovalGateAdapter,
    ApprovalService,
    ApprovalThresholds,
    DuplicateApprover,
    MissingComment,
    SelfApprovalRejected,
    SoDViolation,
    ThresholdBelowFloor,
    TicketFacts,
    W3NotApprovable,
)


def _service() -> ApprovalService:
    return ApprovalService(clock=lambda: NOW)


def _record(service: ApprovalService, *, role: str, user, roles=..., ticket="T-1"):
    role_set = frozenset({role}) if roles is ... else frozenset(roles)
    return service.record(
        ticket_id=ticket,
        role=role,
        approver=principal(user, role_set),
        initiator_id=INITIATOR,
        decision="approved",
        comment="金额与单据一致，同意",
    )


def test_self_approval_is_rejected():
    service = _service()
    with pytest.raises(SelfApprovalRejected):
        service.record(
            ticket_id="T-1",
            role="approver",
            approver=principal(INITIATOR, {"approver"}),
            initiator_id=INITIATOR,
            decision="approved",
            comment="自己批自己",
        )
    assert service.records("T-1") == ()


def test_build_requires_scene_code_at_construction():
    """scene_code 必填：漏传在构造时就失败，不能被默认值静默放行。"""

    service = _service()
    with pytest.raises(TypeError, match="scene_code"):
        service.build(
            ticket_id="T-1",
            role="reviewer",
            approver=principal(REVIEWER, {"reviewer", "finance"}),
            initiator_id=INITIATOR,
            decision="approved",
            comment="漏传场景",
        )
    assert service.records("T-1") == ()


def test_platform_admin_cannot_review_business():
    service = _service()
    with pytest.raises(SoDViolation):
        _record(service, role="approver", user=ADMIN, roles={"platform_admin", "approver"})
    assert service.records("T-1") == ()


def test_same_person_cannot_hold_two_approval_roles():
    service = _service()
    _record(service, role="reviewer", user=REVIEWER, roles={"reviewer", "approver"})
    with pytest.raises(DuplicateApprover):
        _record(service, role="approver", user=REVIEWER, roles={"reviewer", "approver"})


def test_comment_is_mandatory_and_role_capability_is_checked():
    service = _service()
    with pytest.raises(MissingComment):
        service.record(
            ticket_id="T-1",
            role="reviewer",
            approver=principal(REVIEWER, {"reviewer"}),
            initiator_id=INITIATOR,
            decision="approved",
            comment="   ",
        )

    with pytest.raises(SoDViolation):
        _record(service, role="cfo", user=APPROVER, roles={"approver"})


def test_amount_thresholds_derive_approval_chain():
    service = _service()
    assert service.required_roles(
        level=WriteLevel.W0, amount=0, touches_fund=False
    ) == ()
    assert service.required_roles(
        level=WriteLevel.W1, amount=1_000, touches_fund=False
    ) == ("reviewer",)
    assert service.required_roles(
        level=WriteLevel.W2, amount=1_000, touches_fund=False
    ) == ("approver",)
    assert service.required_roles(
        level=WriteLevel.W2, amount=200_000, touches_fund=False
    ) == ("reviewer", "approver")
    assert service.required_roles(
        level=WriteLevel.W1, amount=1_000, touches_fund=True
    ) == ("reviewer", "approver")
    assert service.required_roles(
        level=WriteLevel.W2, amount=600_000, touches_fund=False
    ) == ("reviewer", "approver", "cfo")
    assert service.required_roles(
        level=WriteLevel.W1, amount=1_000, touches_fund=False, force_dual=True
    ) == ("reviewer", "approver")

    with pytest.raises(W3NotApprovable):
        service.required_roles(level=WriteLevel.W3, amount=1_000, touches_fund=False)


def test_threshold_below_g04_floor_is_rejected():
    with pytest.raises(ThresholdBelowFloor):
        ApprovalThresholds(dual_amount=Decimal(90000))
    with pytest.raises(ThresholdBelowFloor):
        ApprovalThresholds(cfo_amount=Decimal(400000))


def test_dual_review_requires_two_distinct_approvers():
    service = _service()
    assert service.satisfied(
        ticket_id="T-1",
        level=WriteLevel.W2,
        amount=200_000,
        touches_fund=False,
        initiator_id=INITIATOR,
    ) is False

    _record(service, role="reviewer", user=REVIEWER)
    assert service.satisfied(
        ticket_id="T-1",
        level=WriteLevel.W2,
        amount=200_000,
        touches_fund=False,
        initiator_id=INITIATOR,
    ) is False
    assert service.missing_roles(
        ticket_id="T-1",
        level=WriteLevel.W2,
        amount=200_000,
        touches_fund=False,
        initiator_id=INITIATOR,
    ) == ("approver",)

    _record(service, role="approver", user=APPROVER)
    assert service.satisfied(
        ticket_id="T-1",
        level=WriteLevel.W2,
        amount=200_000,
        touches_fund=False,
        initiator_id=INITIATOR,
    ) is True


def test_high_amount_requires_cfo_and_rejection_does_not_count():
    service = _service()
    _record(service, role="reviewer", user=REVIEWER)
    _record(service, role="approver", user=APPROVER)
    service.record(
        ticket_id="T-1",
        role="cfo",
        approver=principal(CFO, {"cfo"}),
        initiator_id=INITIATOR,
        decision="rejected",
        comment="预算不足",
    )
    assert service.satisfied(
        ticket_id="T-1",
        level=WriteLevel.W2,
        amount=600_000,
        touches_fund=False,
        initiator_id=INITIATOR,
    ) is False
    assert service.missing_roles(
        ticket_id="T-1",
        level=WriteLevel.W2,
        amount=600_000,
        touches_fund=False,
        initiator_id=INITIATOR,
    ) == ("cfo",)


def test_gateway_adapter_denies_unknown_ticket():
    service = _service()
    _record(service, role="reviewer", user=REVIEWER)
    adapter = ApprovalGateAdapter(
        service,
        {
            "T-1": TicketFacts(
                amount=Decimal(1000), touches_fund=False, initiator_id=INITIATOR
            )
        },
    )
    assert adapter.is_satisfied("T-1", WriteLevel.W1) is True
    assert adapter.is_satisfied("T-404", WriteLevel.W1) is False
