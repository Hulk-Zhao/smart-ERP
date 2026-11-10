"""四道闸集成：身份 → scope → 等级 → 审批（FR-1401/1402/1403/1406）。"""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest
from security_support import (
    APPROVER,
    INITIATOR,
    NOW,
    REVIEWER,
    grant,
    meta_a,
    meta_b,
    principal,
    scope,
)

from apps.platform.policy.levels import WriteLevel
from apps.security.approvals import ApprovalService
from apps.security.gate import (
    AccessGate,
    AccessRequest,
    ApprovalNotSatisfied,
    LevelNotAllowed,
    W3AccessDenied,
)
from apps.security.identity import TokenBroker
from apps.security.scope import Scope, ScopeViolation


def _gate() -> tuple[AccessGate, ApprovalService, TokenBroker]:
    broker = TokenBroker(clock=lambda: NOW)
    approvals = ApprovalService(clock=lambda: NOW)
    return AccessGate(broker=broker, approvals=approvals), approvals, broker


def _record(service: ApprovalService, *, role: str, user) -> None:
    service.record(
        ticket_id="T-1",
        role=role,
        approver=principal(user, {role}),
        initiator_id=INITIATOR,
        decision="approved",
        comment="已核对",
    )


def _request(broker: TokenBroker, **overrides) -> AccessRequest:
    base = AccessRequest(
        session=broker.issue_session(principal(), now=NOW),
        requested_scope=scope(),
        connector_meta=meta_a(),
        ticket_id="T-1",
        level=WriteLevel.W1,
        amount=Decimal(1000),
    )
    return replace(base, **overrides)


def test_w3_is_rejected_before_token_exchange():
    gate, _, broker = _gate()
    with pytest.raises(W3AccessDenied):
        gate.authorize(_request(broker, level=WriteLevel.W3))


def test_a_level_requires_approval_then_grants_delegated_lease():
    gate, approvals, broker = _gate()
    with pytest.raises(ApprovalNotSatisfied):
        gate.authorize(_request(broker))

    _record(approvals, role="reviewer", user=REVIEWER)
    grant_result = gate.authorize(_request(broker))

    assert grant_result.required_roles == ("reviewer",)
    assert grant_result.initiator_id == INITIATOR
    assert grant_result.lease.mode == "delegated"
    assert grant_result.lease.subject_id == INITIATOR
    assert grant_result.lease.ticket_id == "T-1"


def test_b_level_forces_dual_review_and_caps_at_w1():
    gate, approvals, broker = _gate()
    request_b = _request(
        broker,
        connector_meta=meta_b(),
        field_whitelist=frozenset({"amount"}),
    )
    with pytest.raises(ApprovalNotSatisfied):
        gate.authorize(request_b)

    _record(approvals, role="reviewer", user=REVIEWER)
    with pytest.raises(ApprovalNotSatisfied):
        # B 级强制双人：只有复核人不够
        gate.authorize(request_b)

    _record(approvals, role="approver", user=APPROVER)
    result = gate.authorize(request_b)
    assert result.required_roles == ("reviewer", "approver")
    assert result.lease.mode == "service_account"
    assert result.lease.requires_dual_review is True
    assert result.lease.max_level is WriteLevel.W1
    assert result.lease.degradation is not None

    with pytest.raises(LevelNotAllowed):
        gate.authorize(
            _request(
                broker,
                connector_meta=meta_b(),
                field_whitelist=frozenset({"amount"}),
                level=WriteLevel.W2,
            )
        )


def test_scope_outside_grant_is_rejected_before_approval():
    gate, _, broker = _gate()
    with pytest.raises(ScopeViolation):
        gate.authorize(
            _request(
                broker,
                requested_scope=Scope("org-1", "2026-10", ("fake", "unlisted")),
            )
        )


def test_unknown_grant_scope_is_not_widened_by_gate():
    gate, _, broker = _gate()
    assert grant().covers(scope()) is True
    with pytest.raises(ScopeViolation):
        gate.authorize(
            _request(
                broker,
                requested_scope=Scope("org-2", "2026-10", ("fake",)),
            )
        )
