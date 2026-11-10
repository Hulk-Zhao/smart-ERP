"""用户态令牌：A 级委派 / B 级降级留痕，不放大权限（FR-1401）。"""

from __future__ import annotations

from datetime import timedelta

import pytest
from security_support import NOW, meta_a, meta_b, principal, scope

from apps.security.identity import (
    MissingFieldWhitelist,
    TokenBroker,
    TokenExpired,
    TokenTicketMismatch,
    connector_security_level,
)
from apps.security.scope import Scope, ScopeViolation


def _broker() -> TokenBroker:
    return TokenBroker(clock=lambda: NOW)


def test_delegated_lease_is_bound_to_initiator_and_ticket():
    broker = _broker()
    session = broker.issue_session(principal(), now=NOW)
    lease = broker.exchange(
        session,
        connector_meta=meta_a(),
        ticket_id="T-1",
        requested_scope=scope(),
        now=NOW,
    )

    assert lease.mode == "delegated"
    assert lease.subject_id == session.user_id
    assert lease.ticket_id == "T-1"
    assert lease.scope == scope()
    assert lease.requires_dual_review is False
    assert lease.degradation is None
    assert lease.credential_ref.startswith("vault://lease/")
    assert "password" not in lease.credential_ref.lower()

    lease.assert_ticket("T-1")
    with pytest.raises(TokenTicketMismatch):
        lease.assert_ticket("T-2")

    # 凭据范围不可被调用方扩大
    lease.assert_scope(scope())
    with pytest.raises(ScopeViolation):
        lease.assert_scope(Scope("org-1", "2026-10", ("fake", "other")))


def test_scope_request_cannot_exceed_session_grant():
    broker = _broker()
    session = broker.issue_session(principal(), now=NOW)

    with pytest.raises(ScopeViolation):
        broker.exchange(
            session,
            connector_meta=meta_a(),
            ticket_id="T-1",
            requested_scope=Scope("org-2", "2026-10", ("fake",)),
            now=NOW,
        )
    with pytest.raises(ScopeViolation):
        broker.exchange(
            session,
            connector_meta=meta_a(),
            ticket_id="T-1",
            requested_scope=Scope("org-1", "2026-10", ("fake", "unlisted")),
            now=NOW,
        )


def test_connector_system_must_be_inside_requested_scope():
    broker = _broker()
    session = broker.issue_session(principal(), now=NOW)
    with pytest.raises(ScopeViolation):
        broker.exchange(
            session,
            connector_meta=meta_a(),
            ticket_id="T-1",
            requested_scope=Scope("org-1", "2026-10", ("fake-b",)),
            now=NOW,
        )


def test_b_level_falls_back_to_service_account_with_degradation():
    broker = _broker()
    session = broker.issue_session(principal(), now=NOW)

    with pytest.raises(MissingFieldWhitelist):
        broker.exchange(
            session,
            connector_meta=meta_b(),
            ticket_id="T-1",
            requested_scope=scope(),
            now=NOW,
        )
    assert broker.degradations == ()

    lease = broker.exchange(
        session,
        connector_meta=meta_b(),
        ticket_id="T-1",
        requested_scope=scope(),
        field_whitelist=frozenset({"amount", "due_date"}),
        now=NOW,
    )

    assert lease.mode == "service_account"
    assert lease.subject_id != session.user_id
    assert lease.max_level.name == "W1"
    assert lease.requires_dual_review is True
    assert lease.narrowed_fields == frozenset({"amount", "due_date"})
    assert lease.degradation is not None
    assert lease.degradation.reason == "connector_does_not_support_obo"
    assert lease.degradation.ticket_id == "T-1"
    assert broker.degradations == (lease.degradation,)
    assert connector_security_level(meta_b()) == "B"
    assert connector_security_level(meta_a()) == "A"


def test_expired_session_cannot_exchange():
    broker = TokenBroker(clock=lambda: NOW, session_ttl=timedelta(hours=1))
    session = broker.issue_session(principal(), now=NOW)
    with pytest.raises(TokenExpired):
        broker.exchange(
            session,
            connector_meta=meta_a(),
            ticket_id="T-1",
            requested_scope=scope(),
            now=NOW + timedelta(hours=2),
        )


def test_ticket_id_is_mandatory():
    broker = _broker()
    session = broker.issue_session(principal(), now=NOW)
    with pytest.raises(TokenTicketMismatch):
        broker.exchange(
            session,
            connector_meta=meta_a(),
            ticket_id=" ",
            requested_scope=scope(),
            now=NOW,
        )
