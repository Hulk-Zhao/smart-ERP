"""模拟外部系统（u8-stub）自身的行为：回执、幂等、校验、冲销、故障注入。"""

from __future__ import annotations

import httpx

from apps.connectors.u8.stub_server import SIMULATED_SYSTEM, StubU8Server

_PAYLOAD = {
    "source_doc_no": "PO-UNIT-001",
    "cVenCode": "S-0001",
    "dPODate": "2026-09-28",
    "cMemo": "单元测试",
    "details": [{"cInvCode": "I-0001", "iQuantity": "10"}],
}


def test_post_returns_receipt_and_is_idempotent() -> None:
    with StubU8Server() as stub, httpx.Client(base_url=stub.base_url) as client:
        headers = {"Idempotency-Key": "idem-1"}
        first = client.post("/u8/api/purchase-order-drafts", json=_PAYLOAD, headers=headers)
        second = client.post("/u8/api/purchase-order-drafts", json=_PAYLOAD, headers=headers)

        assert first.status_code == 201
        assert first.headers["X-Simulated-System"] == "u8-stub"
        body = first.json()
        assert body["simulated"] is True
        assert body["system"] == SIMULATED_SYSTEM
        assert body["cCode"].startswith("PO-STUB-")
        assert body["source_doc_no"] == "PO-UNIT-001"
        assert body["line_count"] == 1
        assert second.json()["cCode"] == body["cCode"]
        assert len(stub.state.receipts) == 1


def test_get_filters_by_source_doc_no_and_delete_leaves_reversal() -> None:
    with StubU8Server() as stub, httpx.Client(base_url=stub.base_url) as client:
        created = client.post("/u8/api/purchase-order-drafts", json=_PAYLOAD).json()

        listed = client.get(
            "/u8/api/purchase-order-drafts", params={"source_doc_no": "PO-UNIT-001"}
        )
        assert listed.status_code == 200
        assert [row["cCode"] for row in listed.json()["data"]] == [created["cCode"]]

        deleted = client.delete(f"/u8/api/purchase-order-drafts/{created['cCode']}")
        assert deleted.status_code == 204

        assert client.get("/u8/api/purchase-order-drafts").json()["data"] == []
        reversals = client.get("/stub/reversals").json()["items"]
        assert len(reversals) == 1
        assert reversals[0]["cCode"] == created["cCode"]
        assert reversals[0]["simulated"] is True


def test_reset_restores_clean_start_including_sequence() -> None:
    with StubU8Server() as stub, httpx.Client(base_url=stub.base_url) as client:
        first = client.post("/u8/api/purchase-order-drafts", json=_PAYLOAD)
        assert first.json()["cCode"] == "PO-STUB-000001"

        assert client.post("/stub/reset").status_code == 200
        again = client.post("/u8/api/purchase-order-drafts", json=_PAYLOAD)
        assert again.json()["cCode"] == "PO-STUB-000001"
        assert len(stub.state.receipts) == 1
        assert stub.state.reversals == []


def test_fault_injection_returns_5xx_without_receipt() -> None:
    with StubU8Server() as stub, httpx.Client(base_url=stub.base_url) as client:
        client.post("/stub/fault", json={"fail_posts": 1})

        failed = client.post("/u8/api/purchase-order-drafts", json=_PAYLOAD)
        assert failed.status_code == 503
        assert failed.json()["simulated"] is True
        assert stub.state.receipts == []

        recovered = client.post("/u8/api/purchase-order-drafts", json=_PAYLOAD)
        assert recovered.status_code == 201
        assert len(stub.state.receipts) == 1
