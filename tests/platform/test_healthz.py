"""/healthz 不检查依赖，进程起来就应 200。"""

from __future__ import annotations

from fastapi.testclient import TestClient

from apps.main import create_app
from apps.platform.config import Settings


def test_healthz_ok() -> None:
    client = TestClient(create_app(Settings(image_tag="a" * 40)))
    response = client.get("/healthz")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["service"] == "smart-erp-api"
    assert body["image_tag"] == "a" * 40
