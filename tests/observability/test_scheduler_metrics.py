"""scheduler 的 ``/metrics``：写入进程与暴露进程必须共用同一个注册表。"""

from __future__ import annotations

from typing import Any, cast

from fastapi.testclient import TestClient

from core.runtime.scheduler import Scheduler, build_metrics_app


def _scheduler(*, audit_scan: bool = False) -> Scheduler:
    return Scheduler(
        session_factory=cast(Any, None),
        owner="test",
        audit_image_scan=audit_scan,
    )


def test_metrics_endpoint_serves_scheduler_registry() -> None:
    scheduler = _scheduler()
    assert scheduler.metrics is not None
    scheduler.metrics.set_audit_chain_broken(True)

    response = TestClient(build_metrics_app(scheduler)).get("/metrics")

    assert response.status_code == 200
    assert "smart_erp_audit_chain_broken 1" in response.text


def test_audit_monitor_sink_is_the_served_registry() -> None:
    """换一个注册表端点照样 200，但断链标志永远是初始值——这里钉住 sink 身份。"""

    scheduler = _scheduler(audit_scan=True)

    assert scheduler.audit_chain is not None
    assert scheduler.audit_chain.sink is scheduler.metrics

    scheduler.audit_chain.sink.set_audit_chain_broken(True)
    body = TestClient(build_metrics_app(scheduler)).get("/metrics").text
    assert "smart_erp_audit_chain_broken 1" in body


def test_metrics_endpoint_ignores_default_switch(monkeypatch) -> None:
    """独立内网端口显式 enabled=True：不受 ERP_METRICS_ENABLED 默认关闭影响。"""

    monkeypatch.setenv("ERP_METRICS_ENABLED", "0")
    scheduler = _scheduler()
    scheduler.metrics.set_audit_chain_broken(False)

    response = TestClient(build_metrics_app(scheduler)).get("/metrics")

    assert response.status_code == 200
    assert "smart_erp_audit_chain_broken 0" in response.text
