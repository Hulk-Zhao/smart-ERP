"""``GET /v1/auth/demo-identities``：演示身份清单的存在边界。

硬约束：只有 ``APP_ENV=demo`` 且 ``DEMO_SESSION_ENABLED=true`` 时端点存在；
其余环境一律 404——不返回空列表，也不返回任何令牌线索。
"""

from __future__ import annotations

import json
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from apps.main import create_app
from apps.platform.config import get_settings

PREFIX = "/erp/api/v1"
TOKEN_A = "demo-operator-token-0001"
TOKEN_D = "demo-cfo-token-000000001"
USER_A = UUID("aaaaaaaa-0000-4000-8000-000000000001")
USER_D = UUID("dddddddd-0000-4000-8000-000000000004")
TENANT = UUID("11111111-1111-4111-8111-111111111111")


def _identity(token: str, user_id: UUID, roles: list[str]) -> dict:
    return {
        "token": token,
        "user_id": str(user_id),
        "tenant_id": str(TENANT),
        "roles": roles,
        "org_ids": ["ORG-1"],
        "periods": ["2026-10"],
        "system_ids": ["u8", "statement"],
    }


@pytest.fixture(autouse=True)
def _clear_settings_cache():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_demo_identities_listed_in_demo_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ERP_APP_ENV", "demo")
    monkeypatch.setenv("ERP_DEMO_SESSION_ENABLED", "true")
    monkeypatch.setenv(
        "ERP_DEMO_SESSION_IDENTITIES",
        json.dumps(
            [
                _identity(TOKEN_A, USER_A, ["operator"]),
                _identity(TOKEN_D, USER_D, ["reviewer", "approver", "cfo"]),
            ]
        ),
    )

    client = TestClient(create_app(get_settings()))
    response = client.get(f"{PREFIX}/auth/demo-identities")

    assert response.status_code == 200
    assert response.headers.get("cache-control") == "no-store"
    items = response.json()["items"]
    assert [item["token"] for item in items] == [TOKEN_A, TOKEN_D]
    assert items[1]["user_id"] == str(USER_D)
    assert items[1]["roles"] == ["approver", "cfo", "reviewer"]
    # 展示名能区分身份，但不直接泄露完整令牌。
    assert str(USER_D)[:8] in items[1]["label"]
    assert TOKEN_D not in items[1]["label"]


def test_demo_identities_hidden_outside_demo() -> None:
    client = TestClient(create_app(get_settings()))
    response = client.get(f"{PREFIX}/auth/demo-identities")

    assert response.status_code == 404
    assert TOKEN_A not in response.text
    assert TOKEN_D not in response.text

