"""防复发：``/metrics`` 必须挂进 ``create_app()``，且埋点与端点共用容器的注册表。

只测接线，不重复 ``tests/observability`` 的指标口径：
- 缺省不挂载（生产不会被随手翻出内部指标）；
- 挂载后读到的就是容器里那一个 registry（否则端点能 200 但永远全 0）；
- 一次对账只记一次批次（``observe_recon_result`` 已含计数，禁止再叠加）。
"""

from __future__ import annotations

import conftest as cf
from fastapi.testclient import TestClient

from apps.api.container import get_container
from apps.api.recon_store import ReconConflict
from apps.main import create_app
from apps.platform.config import Settings

PREFIX = cf.PREFIX
RESULT_LABELS = ("matched", "pending_manual", "unreconcilable")


class _Result:
    """``observe_recon_result`` 是鸭子类型，这里只用它需要的最小字段。"""

    batch_status = "RECONCILED"
    lines: tuple[object, ...] = ()
    metrics = None


def _line(*, system: str, doc_no: str, item_code: str, amount: str) -> dict[str, object]:
    return {
        "system": system,
        "doc_no": doc_no,
        "line_no": 1,
        "supplier_code": "SUP-A",
        "item_code": item_code,
        "qty": "10",
        "unit": "PCS",
        "unit_price": "5",
        "amount": amount,
        "tax_rate": "0.13",
        "period": cf.PERIOD,
    }


def _body(env) -> dict[str, object]:
    return {
        "scope": env.scope(system_ids=[cf.SYSTEM, cf.STATEMENT]),
        "suppliers": ["SUP-A"],
        "dataset": {
            "our_lines": [
                _line(system=cf.SYSTEM, doc_no="PO-1", item_code="IT-A", amount="50.00"),
            ],
            "supplier_lines": [
                _line(
                    system=cf.STATEMENT, doc_no="PO-1", item_code="IT-A", amount="50.50"
                ),
            ],
            "master_data": {
                "suppliers": [{"code": "SUP-A", "name": "供应商A"}],
                "items": [{"code": "IT-A", "name": "物料A", "base_unit": "PCS"}],
            },
            "tolerance": {},
        },
    }


def test_metrics_endpoint_is_not_mounted_by_default(monkeypatch) -> None:
    monkeypatch.delenv("ERP_METRICS_ENABLED", raising=False)
    app = create_app(Settings(image_tag="test"))
    assert TestClient(app).get("/metrics").status_code == 404


def test_metrics_endpoint_reads_the_container_registry(monkeypatch) -> None:
    # 挂载前该路径是 404（FastAPI 默认），所以「404」不构成已拦截的证据；
    # 这里断言的是「开启后能读到容器里刚写进去的数」，即两端同源。
    monkeypatch.setenv("ERP_METRICS_ENABLED", "true")
    app = create_app(Settings(image_tag="test"))
    get_container().metrics.observe_recon_result(_Result())

    response = TestClient(app).get("/metrics")

    assert response.status_code == 200
    assert 'recon_batches_total{result="matched"} 1' in response.text


def test_recon_run_is_observed_exactly_once(env) -> None:
    response = env.client.post(
        f"{PREFIX}/recon/runs", json=_body(env), headers=env.headers("initiator", "k-metrics")
    )
    assert response.status_code == 201, response.text

    registry = env.container.metrics
    batches = sum(
        registry.recon_batches_total.value(result=label) for label in RESULT_LABELS
    )
    assert batches == 1
    _, _, duration_count = registry.recon_batch_duration_seconds.snapshot()
    assert duration_count == 1


class _ConflictStore:
    """模拟同一 batch_id 二次写入：真实 store 会抛 ReconConflict。"""

    async def save(self, _snapshot) -> None:
        raise ReconConflict("批次已存在")


def test_missing_dataset_is_counted_as_input_failure(env) -> None:
    body = _body(env)
    body.pop("dataset")

    response = env.client.post(
        f"{PREFIX}/recon/runs", json=body, headers=env.headers("initiator", "k-fail-input")
    )

    assert response.status_code == 422, response.text
    registry = env.container.metrics
    assert registry.recon_runs_failed_total.value(
        stage="input", code="E-DATA-MISSING"
    ) == 1
    # 失败不冒充批次：结果计数保持为 0。
    assert sum(
        registry.recon_batches_total.value(result=label) for label in RESULT_LABELS
    ) == 0


def test_persist_conflict_is_counted_as_failure(env) -> None:
    env.container.recon_store = _ConflictStore()

    response = env.client.post(
        f"{PREFIX}/recon/runs", json=_body(env), headers=env.headers("initiator", "k-fail-persist")
    )

    assert response.status_code == 409, response.text
    assert env.container.metrics.recon_runs_failed_total.value(
        stage="persist", code="E-IDEM-CONFLICT"
    ) == 1

