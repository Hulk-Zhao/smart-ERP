"""connector-gw 入口：/healthz（不查依赖）、/readyz（查下游）、ERP_ 端口与启动失败。"""

from __future__ import annotations

import json
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

import pytest
from fastapi.testclient import TestClient

from apps.connectors.gateway import (
    DEFAULT_PORT,
    GatewayRegistry,
    create_app,
    port_from_env,
)
from tests.connectors.support import ROOT, make_stack


def _registry(probe_result: bool = True) -> GatewayRegistry:
    _, stack = make_stack()
    registry = GatewayRegistry()

    async def probe() -> bool:
        return probe_result

    registry.register("u8", stack.connector, probe=probe)
    return registry


def test_healthz_reports_actions_and_allowlist_hash() -> None:
    client = TestClient(create_app(_registry()))

    response = client.get("/healthz")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["registered_actions"] == 2
    assert body["actions_allowlist_hash"].startswith("sha256:")
    card = body["connectors"][0]
    assert card["alias"] == "u8"
    assert card["system"] == "u8"
    assert set(card["objects"]) == {
        "supplier",
        "item",
        "purchase_order",
        "inbound_order",
    }
    assert "u8.po.create_draft" in card["actions"]


def test_readyz_ok_when_downstream_probe_succeeds() -> None:
    client = TestClient(create_app(_registry(probe_result=True)))

    response = client.get("/readyz")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    assert body["connectors"]["u8"]["downstream"] is True


def test_readyz_503_when_downstream_probe_fails() -> None:
    client = TestClient(create_app(_registry(probe_result=False)))

    response = client.get("/readyz")

    assert response.status_code == 503
    assert response.json()["status"] == "not_ready"


def test_readyz_503_when_breaker_open() -> None:
    registry = _registry()
    _, stack = make_stack()
    for _ in range(3):
        stack.breaker.record_failure()
    registry.register("u8", stack.connector)

    response = TestClient(create_app(registry)).get("/readyz")

    assert response.status_code == 503
    assert response.json()["connectors"]["u8"]["ready"] is False


def test_port_comes_from_erp_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ERP_CONNECTOR_GW_PORT", raising=False)
    monkeypatch.delenv("ERP_GATEWAY_PORT", raising=False)
    assert port_from_env() == DEFAULT_PORT

    monkeypatch.setenv("ERP_CONNECTOR_GW_PORT", "8123")
    assert port_from_env() == 8123

    monkeypatch.setenv("ERP_CONNECTOR_GW_PORT", "not-a-port")
    with pytest.raises(ValueError):
        port_from_env()


def test_startup_check_exits_zero() -> None:
    completed = subprocess.run(
        [sys.executable, "-m", "apps.connectors.gateway", "--check"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert "自检通过" in completed.stdout


def test_invalid_port_exits_non_zero() -> None:
    completed = subprocess.run(
        [sys.executable, "-m", "apps.connectors.gateway", "--port", "70000"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert completed.returncode == 2
    assert "启动失败" in completed.stderr


def test_module_entry_serves_healthz_on_real_port() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]

    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "apps.connectors.gateway",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.time() + 30
        body: dict | None = None
        while time.time() < deadline:
            if process.poll() is not None:
                break
            try:
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/healthz", timeout=1.0
                ) as response:
                    body = json.loads(response.read().decode("utf-8"))
                break
            except (urllib.error.URLError, ConnectionError, TimeoutError):
                time.sleep(0.2)
        assert body is not None, process.stderr.read() if process.poll() is not None else "超时"
        assert body["status"] == "ok"
        assert body["service"] == "smart-erp-connector-gw"
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:  # pragma: no cover
            process.kill()
