"""E2E 冒烟：从 HTTP 层驱动 ``apps.main``，覆盖健康检查、只读与拒绝写入。

不依赖 PG / 外部系统：连接器、闸门、审计都在进程内装配。
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from e2e_support import E2E_PREFIX, Harness, build_harness_app
from fastapi.testclient import TestClient


@pytest.fixture()
def harness(monkeypatch) -> Iterator[tuple[Harness, TestClient]]:
    monkeypatch.setenv("ERP_APP_ENV", "test")
    built = build_harness_app()
    assert built.routes_mounted is True
    with TestClient(built.app) as client:
        yield built, client


def _write_body(**overrides):
    body = {
        "ticket_id": "T-1001",
        "action_key": "u8.purchase_order.create_draft",
        "level": "W1",
        "idem_key": "idem-e2e-1",
        "payload": {"supplier": "S-001", "amount": "1200.00"},
        "scope": {"org_id": "org-1", "period": "2026-10", "system_ids": ["u8"]},
        "amount": "1000.00",
    }
    body.update(overrides)
    return body


def test_healthz_and_openapi_are_reachable(harness) -> None:
    _, client = harness

    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "service": "smart-erp-api",
        "image_tag": "e2e",
    }

    schema = client.get("/openapi.json")
    assert schema.status_code == 200
    assert "/healthz" in schema.json()["paths"]


def test_readonly_ticket_endpoint_does_not_touch_connector(harness) -> None:
    built, client = harness

    response = client.get("/_e2e/tickets/T-1001")
    assert response.status_code == 200
    assert response.json() == {
        "ticket_id": "T-1001",
        "state": "planned",
        "steps": [],
        "readonly": True,
    }
    assert built.connector.writes == []


def test_w3_write_is_rejected_over_http(harness) -> None:
    built, client = harness

    response = client.post("/_e2e/write", json=_write_body(level="W3"))

    assert response.status_code == 403
    assert response.headers["content-type"].startswith("application/problem+json")
    body = response.json()
    assert body["code"] == "E-POLICY-W3FORBIDDEN"
    assert body["retriable"] is False
    assert body["trace_id"]
    assert built.connector.writes == []

    rejected = [
        row
        for row in built.exporter.spans
        if row["status"] == "rejected"
        and row["attributes"].get("reject_code") == "E-POLICY-W3FORBIDDEN"
    ]
    assert len(rejected) == 1
    assert rejected[0]["level"] == "W3"


def test_out_of_scope_write_is_rejected_over_http(harness) -> None:
    built, client = harness

    response = client.post(
        "/_e2e/write",
        json=_write_body(
            scope={"org_id": "org-2", "period": "2026-10", "system_ids": ["u8"]}
        ),
    )

    assert response.status_code == 403
    body = response.json()
    assert body["code"] == "E-AUTHZ-001"
    assert body["title"] == "超出数据范围"
    assert built.connector.writes == []


def test_write_without_approval_is_rejected_over_http(harness) -> None:
    built, client = harness

    response = client.post("/_e2e/write", json=_write_body())

    assert response.status_code == 409
    body = response.json()
    assert body["code"] == "E-POLICY-APPROVAL"
    assert built.connector.writes == []


def test_test_env_openapi_exposes_e2e_routes(harness) -> None:
    built, client = harness

    assert built.routes_mounted is True
    paths = client.get("/openapi.json").json()["paths"]
    assert f"{E2E_PREFIX}/write" in paths
    assert f"{E2E_PREFIX}/tickets/{{ticket_id}}" in paths


@pytest.mark.parametrize(
    "env",
    [
        pytest.param({}, id="unset"),
        pytest.param({"ERP_APP_ENV": ""}, id="empty"),
        pytest.param({"ERP_APP_ENV": "prod"}, id="prod"),
        pytest.param({"ERP_APP_ENV": "staging"}, id="staging"),
        pytest.param({"ERP_APP_ENV": "dev"}, id="dev"),
    ],
)
def test_e2e_routes_do_not_exist_outside_test_env(env) -> None:
    built = build_harness_app(env=env)

    assert built.routes_mounted is False
    assert not any(
        getattr(route, "path", "").startswith(E2E_PREFIX) for route in built.app.routes
    )

    with TestClient(built.app) as client:
        assert client.get(f"{E2E_PREFIX}/tickets/T-1001").status_code == 404
        assert client.post(f"{E2E_PREFIX}/write", json=_write_body()).status_code == 404

        paths = client.get("/openapi.json").json()["paths"]
        assert not any(path.startswith(E2E_PREFIX) for path in paths)
        assert "/healthz" in paths

    assert built.connector.writes == []
