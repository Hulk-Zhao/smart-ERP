"""幂等键：同键同参复用结果，同键不同参报障，在途拒绝并发。"""

from __future__ import annotations

import pytest

from apps.connectors.errors import ConnectorError, ErrorKind
from apps.connectors.idempotency import (
    IdemStatus,
    InMemoryIdempotencyStore,
    request_hash,
)
from tests.connectors.support import run


def test_request_hash_is_order_independent_but_value_sensitive() -> None:
    left = request_hash({"a": 1, "b": [1, 2]})
    right = request_hash({"b": [1, 2], "a": 1})
    other = request_hash({"a": 1, "b": [1, 3]})
    assert left == right
    assert left != other


def test_same_key_same_payload_replays_stored_result() -> None:
    store = InMemoryIdempotencyStore()

    async def scenario() -> None:
        fingerprint = request_hash({"doc": "PO-1"})
        first = await store.begin("idem-1", fingerprint)
        assert first.status is IdemStatus.NEW

        await store.complete("idem-1", {"external_id": "DPO-0001"})
        replay = await store.begin("idem-1", fingerprint)
        assert replay.status is IdemStatus.REPLAY
        assert replay.response == {"external_id": "DPO-0001"}

    run(scenario())


def test_same_key_different_payload_conflicts() -> None:
    store = InMemoryIdempotencyStore()

    async def scenario() -> None:
        await store.begin("idem-1", request_hash({"qty": 120}))
        with pytest.raises(ConnectorError) as caught:
            await store.begin("idem-1", request_hash({"qty": 999}))
        assert caught.value.kind is ErrorKind.IDEM_CONFLICT
        assert caught.value.retryable is False

    run(scenario())


def test_in_flight_duplicate_is_rejected() -> None:
    store = InMemoryIdempotencyStore()

    async def scenario() -> None:
        fingerprint = request_hash({"qty": 1})
        await store.begin("idem-1", fingerprint)
        with pytest.raises(ConnectorError):
            await store.begin("idem-1", fingerprint)

    run(scenario())


def test_failed_key_can_be_retried() -> None:
    store = InMemoryIdempotencyStore()

    async def scenario() -> None:
        fingerprint = request_hash({"qty": 1})
        await store.begin("idem-1", fingerprint)
        await store.fail("idem-1")
        again = await store.begin("idem-1", fingerprint)
        assert again.status is IdemStatus.NEW

    run(scenario())
