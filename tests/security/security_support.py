"""权限测试夹具。"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

from apps.platform.connectors.sdk import ConnectorMeta
from apps.security.identity import TokenBroker, UserPrincipal
from apps.security.scope import Scope, ScopeGrant

NOW = datetime(2026, 11, 10, 12, 0, tzinfo=timezone.utc)

TENANT = UUID("11111111-1111-1111-1111-111111111111")
INITIATOR = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
REVIEWER = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
APPROVER = UUID("cccccccc-cccc-cccc-cccc-cccccccccccc")
CFO = UUID("dddddddd-dddd-dddd-dddd-dddddddddddd")
ADMIN = UUID("eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee")


def grant() -> ScopeGrant:
    return ScopeGrant(
        org_ids=frozenset({"org-1"}),
        periods=frozenset({"2026-10"}),
        system_ids=frozenset({"fake", "fake-b"}),
    )


def scope() -> Scope:
    return Scope(org_id="org-1", period="2026-10", system_ids=("fake",))


def principal(user_id: UUID = INITIATOR, roles=frozenset({"operator"})) -> UserPrincipal:
    return UserPrincipal(
        tenant_id=TENANT, user_id=user_id, roles=frozenset(roles), grant=grant()
    )


def meta_a() -> ConnectorMeta:
    return ConnectorMeta(
        system="fake",
        version="1.0",
        access_level="L1",
        security_level="A",
        supports_obo=True,
        version_fingerprint="fake-a",
    )


def meta_b() -> ConnectorMeta:
    return ConnectorMeta(
        system="fake",
        version="1.0",
        access_level="L1",
        security_level="B",
        supports_obo=False,
        version_fingerprint="fake-b",
    )


def session(broker: TokenBroker, who: UserPrincipal | None = None):
    return broker.issue_session(who or principal(), now=NOW)
