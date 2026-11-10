"""平台层 API 装配：请求 ID / trace_id、problem+json 错误包装、超时兜底。"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, FastAPI, HTTPException
from fastapi.testclient import TestClient
from pydantic import BaseModel

from apps.main import create_app
from apps.platform.api import ProblemError, install_platform
from apps.platform.config import Settings
from apps.platform.contracts.errors import ErrorCode

router = APIRouter()


class Item(BaseModel):
    amount: int


@router.get("/_t/echo")
async def echo() -> dict[str, str]:
    return {"ok": "1"}


@router.post("/_t/items")
async def create_item(payload: Item) -> dict[str, int]:
    return {"amount": payload.amount}


@router.get("/_t/problem")
async def raise_problem() -> None:
    raise ProblemError(ErrorCode.AUTHZ_SCOPE, "越权访问客户 42")


@router.get("/_t/missing")
async def raise_http() -> None:
    raise HTTPException(status_code=404, detail="工单不存在")


@router.get("/_t/slow")
async def slow() -> dict[str, str]:
    await asyncio.sleep(0.2)
    return {"ok": "late"}


def _client() -> TestClient:
    app = create_app(Settings(image_tag="a" * 40))
    app.include_router(router)
    return TestClient(app)


def test_success_response_carries_request_and_trace_ids() -> None:
    response = _client().get("/healthz")

    assert response.status_code == 200
    request_id = response.headers["X-Request-ID"]
    assert request_id
    assert response.headers["X-Trace-ID"] == request_id


def test_incoming_ids_are_reused() -> None:
    response = _client().get(
        "/healthz",
        headers={"X-Request-ID": "req-123", "X-Trace-ID": "trace-456"},
    )

    assert response.headers["X-Request-ID"] == "req-123"
    assert response.headers["X-Trace-ID"] == "trace-456"


def test_problem_error_returns_problem_json_with_trace_id() -> None:
    response = _client().get("/_t/problem")

    assert response.status_code == 403
    assert response.headers["content-type"].startswith("application/problem+json")
    body = response.json()
    assert body["code"] == ErrorCode.AUTHZ_SCOPE.value
    assert body["detail"] == "越权访问客户 42"
    assert body["trace_id"] == response.headers["X-Trace-ID"]
    assert body["retriable"] is False


def test_validation_error_is_wrapped_as_problem_json() -> None:
    response = _client().post("/_t/items", json={"amount": "not-a-number"})

    assert response.status_code == 422
    assert response.headers["content-type"].startswith("application/problem+json")
    body = response.json()
    assert body["code"] == ErrorCode.PARSE_FAILED.value
    assert body["trace_id"] == response.headers["X-Trace-ID"]


def test_http_exception_is_wrapped_and_keeps_status() -> None:
    response = _client().get("/_t/missing")

    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/problem+json")
    body = response.json()
    assert body["code"] == ErrorCode.RESOURCE_NOT_FOUND.value
    assert body["detail"] == "工单不存在"
    assert body["trace_id"] == response.headers["X-Trace-ID"]


def test_timeout_returns_problem_json_with_trace_id() -> None:
    app = FastAPI()
    app.include_router(router)
    install_platform(app, timeout_s=0.01)
    client = TestClient(app)

    response = client.get("/_t/slow")

    assert response.status_code == 504
    assert response.headers["content-type"].startswith("application/problem+json")
    body = response.json()
    assert body["code"] == ErrorCode.CONN_UNAVAILABLE.value
    assert body["trace_id"] == response.headers["X-Trace-ID"]


def test_install_platform_is_idempotent() -> None:
    app = FastAPI()
    install_platform(app)
    route_count = len(app.routes)
    middleware_count = len(app.user_middleware)

    install_platform(app)

    assert len(app.routes) == route_count
    assert len(app.user_middleware) == middleware_count
