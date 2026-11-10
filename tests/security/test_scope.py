"""数据范围：三要素必填、只能收窄（G-05 / FR-1402）。"""

from __future__ import annotations

import pytest
from security_support import grant, scope

from apps.security.scope import (
    Scope,
    ScopeGrant,
    ScopeGuard,
    ScopeViolation,
    intersect_grants,
    merge_grants,
)


@pytest.mark.parametrize(
    ("org_id", "period", "systems"),
    [
        ("", "2026-10", ("fake",)),
        ("org-1", " ", ("fake",)),
        ("org-1", "2026-10", ()),
        ("org-1", "2026-10", ("fake", " ")),
        ("org-1", "2026-10", ("fake", "fake")),
    ],
)
def test_scope_requires_org_period_and_unique_systems(org_id, period, systems):
    with pytest.raises(ScopeViolation):
        Scope(org_id=org_id, period=period, system_ids=systems)


def test_grant_must_not_be_empty():
    with pytest.raises(ScopeViolation):
        ScopeGrant(org_ids=frozenset(), periods=frozenset({"2026-10"}), system_ids={"fake"})


def test_scope_guard_accepts_subset_and_rejects_escalation():
    guard = ScopeGuard(grant())
    assert guard.authorize(scope()) == scope()

    with pytest.raises(ScopeViolation):
        guard.authorize(Scope(org_id="org-2", period="2026-10", system_ids=("fake",)))
    with pytest.raises(ScopeViolation):
        guard.authorize(Scope(org_id="org-1", period="2026-09", system_ids=("fake",)))
    with pytest.raises(ScopeViolation):
        guard.authorize(Scope(org_id="org-1", period="2026-10", system_ids=("fake", "other")))
    with pytest.raises(ScopeViolation):
        guard.authorize(Scope(org_id="org-1", period="2026-10", system_ids=("other",)))


def test_multi_role_grants_union_and_intersection():
    left = ScopeGrant(
        org_ids=frozenset({"org-1", "org-2"}),
        periods=frozenset({"2026-10"}),
        system_ids=frozenset({"fake", "fake-b"}),
    )
    right = ScopeGrant(
        org_ids=frozenset({"org-2"}),
        periods=frozenset({"2026-10", "2026-11"}),
        system_ids=frozenset({"fake-b"}),
    )
    merged = merge_grants(left, right)
    assert merged.org_ids == {"org-1", "org-2"}
    assert merged.system_ids == {"fake", "fake-b"}

    overlap = intersect_grants(left, right)
    assert overlap.org_ids == {"org-2"}
    assert overlap.system_ids == frozenset({"fake-b"})
    assert ScopeGuard(overlap).authorize(
        Scope(org_id="org-2", period="2026-10", system_ids=("fake-b",))
    )

    disjunct = ScopeGrant(
        org_ids=frozenset({"org-3"}),
        periods=frozenset({"2026-10"}),
        system_ids=frozenset({"fake"}),
    )
    with pytest.raises(ScopeViolation):
        # 交集为空 = 无权限，而不是「取全部」
        intersect_grants(left, disjunct)
