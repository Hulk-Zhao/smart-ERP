"""连接器 SDK：写调用必须绑定工单与幂等键，未装配 transport 直接失败。"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any

import pytest

from apps.platform.connectors.sdk import (
    BaseConnector,
    CapabilitySet,
    CompensateResult,
    ConnectorContractError,
    ConnectorMeta,
    Health,
    MissingIdempotencyKey,
    MissingTicketContext,
    PreviewResult,
    ReadQuery,
    ReadResult,
    VerifyResult,
)
from apps.platform.connectors.transport import TransportError
from apps.platform.policy.levels import ActionSpec, VerifySpec
from tests.platform.support import make_action


class StubConnector(BaseConnector):
    meta = ConnectorMeta(
        system="fakerp",
        version="1.0.0",
        access_level="L1",
        security_level="A",
        supports_obo=False,
        version_fingerprint="sha256:stub",
    )

    def describe(self) -> CapabilitySet:
        return CapabilitySet(objects=("draft",), actions=())

    async def read(self, entity: str, query: ReadQuery) -> ReadResult:
        return ReadResult(items=(), completeness="complete")

    async def preview(self, action: ActionSpec, payload: Mapping[str, Any]) -> PreviewResult:
        return PreviewResult(diff=(), before={}, verify_spec=VerifySpec(kind="read"))

    async def verify(self, spec: VerifySpec, payload: Mapping[str, Any]) -> VerifyResult:
        return VerifyResult(status="match")

    async def compensate(self, comp: Any, *, idem_key: str) -> CompensateResult:
        return CompensateResult(status="compensated")

    async def health(self) -> Health:
        return Health(
            ok=True,
            last_sync_at=None,
            failure_rate=0.0,
            rate_limit_remaining=1.0,
            version_fingerprint="sha256:stub",
        )


def test_write_requires_ticket_id() -> None:
    connector = StubConnector()
    with pytest.raises(MissingTicketContext):
        asyncio.run(connector.write(make_action(), {}, ticket_id="", idem_key="k"))


def test_write_requires_idem_key() -> None:
    connector = StubConnector()
    with pytest.raises(MissingIdempotencyKey):
        asyncio.run(connector.write(make_action(), {}, ticket_id="AT-1", idem_key=""))


def test_write_without_transport_fails_loudly() -> None:
    connector = StubConnector()
    with pytest.raises(TransportError):
        asyncio.run(connector.write(make_action(), {}, ticket_id="AT-1", idem_key="k"))


def test_read_unavailable_must_carry_reason() -> None:
    with pytest.raises(ConnectorContractError):
        ReadResult(items=(), completeness="unavailable")
