"""对账路由：发起 / 状态 / 差异列表 / 差异明细。"""

from __future__ import annotations

from conftest import PERIOD, PREFIX, STATEMENT, SYSTEM


def _line(*, system: str, unit_price: str, amount: str) -> dict[str, object]:
    return {
        "system": system,
        "doc_no": "PO-1",
        "line_no": 1,
        "supplier_code": "SUP-A",
        "item_code": "IT-A",
        "qty": "10",
        "unit": "PCS",
        "unit_price": unit_price,
        "amount": amount,
        "tax_rate": "0.13",
        "period": PERIOD,
    }


def _body(env, *, with_dataset: bool = True) -> dict[str, object]:
    body: dict[str, object] = {
        "scope": env.scope(system_ids=[SYSTEM, STATEMENT]),
        "suppliers": ["SUP-A"],
    }
    if with_dataset:
        body["dataset"] = {
            "our_lines": [_line(system=SYSTEM, unit_price="5", amount="50")],
            "supplier_lines": [
                _line(system=STATEMENT, unit_price="5.5", amount="55")
            ],
            "master_data": {
                "suppliers": [{"code": "SUP-A", "name": "供应商A"}],
                "items": [{"code": "IT-A", "name": "物料A", "base_unit": "PCS"}],
            },
            "tolerance": {},
        }
    return body


def test_recon_run_then_status_diffs_and_detail(env):
    run = env.client.post(
        f"{PREFIX}/recon/runs",
        json=_body(env),
        headers=env.headers("initiator", "k-recon"),
    )
    assert run.status_code == 201, run.text
    batch = run.json()
    assert batch["conclusive"] is True
    assert batch["diff_count"] == 1
    assert batch["metrics"]["matched"] == 1
    batch_id = batch["batch_id"]

    # 同键同参重放：返回同一批次，不重复计算。
    replay = env.client.post(
        f"{PREFIX}/recon/runs",
        json=_body(env),
        headers=env.headers("initiator", "k-recon"),
    )
    assert replay.status_code == 201
    assert replay.json()["batch_id"] == batch_id

    status = env.client.get(
        f"{PREFIX}/recon/runs/{batch_id}",
        params=env.scope_params(system_ids=[SYSTEM, STATEMENT]),
        headers=env.headers(),
    )
    assert status.status_code == 200, status.text
    assert status.json()["batch_id"] == batch_id

    diffs = env.client.get(
        f"{PREFIX}/recon/runs/{batch_id}/diffs",
        params=env.scope_params(system_ids=[SYSTEM, STATEMENT])
        + [("diff_type", "unit_price")],
        headers=env.headers(),
    )
    assert diffs.status_code == 200, diffs.text
    items = diffs.json()["items"]
    assert len(items) == 1
    assert items[0]["type"] == "unit_price"
    assert items[0]["billing_bucket"] == "billable"
    assert items[0]["diff_id"] == "d0"
    assert items[0]["amount"]["amount"] == "5.0000"

    by_bucket = env.client.get(
        f"{PREFIX}/recon/runs/{batch_id}/diffs",
        params=env.scope_params(system_ids=[SYSTEM, STATEMENT])
        + [("billing_bucket", "billable")],
        headers=env.headers(),
    )
    assert by_bucket.status_code == 200, by_bucket.text
    assert [item["diff_id"] for item in by_bucket.json()["items"]] == ["d0"]

    detail = env.client.get(
        f"{PREFIX}/recon/runs/{batch_id}/diffs/d0",
        params=env.scope_params(system_ids=[SYSTEM, STATEMENT]),
        headers=env.headers(),
    )
    assert detail.status_code == 200, detail.text
    detail_body = detail.json()
    assert detail_body["batch_id"] == batch_id
    assert detail_body["related_lines"][0]["our_ref"] == "u8:PO-1#1"
    assert detail_body["draft"] is not None

    missing = env.client.get(
        f"{PREFIX}/recon/runs/{batch_id}/diffs/d99",
        params=env.scope_params(system_ids=[SYSTEM, STATEMENT]),
        headers=env.headers(),
    )
    assert missing.status_code == 404
    assert missing.json()["code"] == "E-RESOURCE-NOTFOUND"


def test_recon_without_dataset_does_not_guess_sources(env):
    response = env.client.post(
        f"{PREFIX}/recon/runs",
        json=_body(env, with_dataset=False),
        headers=env.headers("initiator", "k-recon-empty"),
    )

    assert response.status_code == 422, response.text
    assert response.json()["code"] == "E-DATA-MISSING"
    assert "dataset" in response.json()["detail"]
    assert env.connector.writes == []


def test_rounding_tolerance_diff_is_listed_and_filterable(env):
    """含 ROUNDING_TOLERANCE 的批次不得 KeyError → 500，且可按新类型过滤。"""
    line = {
        "system": SYSTEM,
        "doc_no": "PO-R",
        "line_no": 1,
        "supplier_code": "SUP-A",
        "item_code": "IT-A",
        "qty": "10",
        "unit": "PCS",
        "unit_price": "5",
        "tax_rate": "0.13",
        "period": PERIOD,
    }
    env.container.recon_datasets.register(
        "rounding.xlsx",
        {
            "our_lines": [{**line, "amount": "50.00"}],
            "supplier_lines": [{**line, "system": STATEMENT, "amount": "50.40"}],
            "master_data": {
                "suppliers": [{"code": "SUP-A", "name": "供应商A"}],
                "items": [{"code": "IT-A", "name": "物料A", "base_unit": "PCS"}],
            },
            "tolerance": {"rounding": "0.50", "rounding_batch_limit": "10.00"},
        },
    )
    params = env.scope_params(system_ids=[SYSTEM, STATEMENT])

    run = env.client.post(
        f"{PREFIX}/recon/runs",
        json={
            "scope": env.scope(system_ids=[SYSTEM, STATEMENT]),
            "suppliers": ["SUP-A"],
            "sources": {"statement_files": ["rounding.xlsx"]},
        },
        headers=env.headers("initiator", "k-recon-rounding"),
    )
    assert run.status_code == 201, run.text
    batch = run.json()
    assert batch["diff_count"] == 1
    batch_id = batch["batch_id"]

    listed = env.client.get(
        f"{PREFIX}/recon/runs/{batch_id}/diffs", params=params, headers=env.headers()
    )
    assert listed.status_code == 200, listed.text
    items = listed.json()["items"]
    assert [item["type"] for item in items] == ["rounding_tolerance"]

    filtered = env.client.get(
        f"{PREFIX}/recon/runs/{batch_id}/diffs",
        params=params + [("diff_type", "rounding_tolerance")],
        headers=env.headers(),
    )
    assert filtered.status_code == 200, filtered.text
    assert [item["type"] for item in filtered.json()["items"]] == [
        "rounding_tolerance"
    ]

    other = env.client.get(
        f"{PREFIX}/recon/runs/{batch_id}/diffs",
        params=params + [("diff_type", "unit_price")],
        headers=env.headers(),
    )
    assert other.status_code == 200, other.text
    assert other.json()["items"] == []
