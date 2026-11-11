"""``GET /v1/recon/batches``：计数同源、键集分页、租户/scope 过滤与空态语义。"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from uuid import UUID, uuid4

import conftest as cf

from apps.api.recon_snapshot import BatchSnapshot

PREFIX = cf.PREFIX
BOTH_SYSTEMS = [cf.SYSTEM, cf.STATEMENT]


def _line(
    *, system: str, doc_no: str, item_code: str, amount: str, qty: str = "10"
) -> dict[str, object]:
    return {
        "system": system,
        "doc_no": doc_no,
        "line_no": 1,
        "supplier_code": "SUP-A",
        "item_code": item_code,
        "qty": qty,
        "unit": "PCS",
        "unit_price": "5",
        "amount": amount,
        "tax_rate": "0.13",
        "period": cf.PERIOD,
    }


def _body(env, *, supplier_amount: str = "50.50") -> dict[str, object]:
    return {
        "scope": env.scope(system_ids=BOTH_SYSTEMS),
        "suppliers": ["SUP-A"],
        "dataset": {
            "our_lines": [
                _line(system=cf.SYSTEM, doc_no="PO-1", item_code="IT-A", amount="50.00"),
                _line(system=cf.SYSTEM, doc_no="PO-2", item_code="IT-B", amount="50.00"),
            ],
            "supplier_lines": [
                _line(system=cf.STATEMENT, doc_no="PO-1", item_code="IT-A", amount="50.00"),
                _line(
                    system=cf.STATEMENT,
                    doc_no="PO-2",
                    item_code="IT-B",
                    amount=supplier_amount,
                ),
            ],
            "master_data": {
                "suppliers": [{"code": "SUP-A", "name": "供应商A"}],
                "items": [
                    {"code": "IT-A", "name": "物料A", "base_unit": "PCS"},
                    {"code": "IT-B", "name": "物料B", "base_unit": "PCS"},
                ],
            },
            "tolerance": {},
        },
    }


def _create_batch(env, key: str, *, supplier_amount: str = "50.50") -> dict:
    response = env.client.post(
        f"{PREFIX}/recon/runs",
        json=_body(env, supplier_amount=supplier_amount),
        headers=env.headers("initiator", key),
    )
    assert response.status_code == 201, response.text
    return response.json()


def _list(env, *, limit: int | None = None, cursor: str | None = None):
    params = env.scope_params(system_ids=BOTH_SYSTEMS)
    if limit is not None:
        params.append(("limit", str(limit)))
    if cursor is not None:
        params.append(("cursor", cursor))
    return env.client.get(
        f"{PREFIX}/recon/batches", params=params, headers=env.headers()
    )


def _snapshot(*, tenant_id: UUID, org_id: str, view=None) -> BatchSnapshot:
    return BatchSnapshot(
        batch_id=uuid4(),
        tenant_id=tenant_id,
        org_id=org_id,
        period=cf.PERIOD,
        system_ids=(cf.SYSTEM,),
        suppliers=("SUP-A",),
        status="done",
        conclusive=True,
        status_reason="",
        metrics=None,
        input_fingerprint=f"fp-{uuid4()}",
        rerun_of=None,
        rerun_reason=None,
        created_by=cf.INITIATOR,
        created_at=datetime.now(UTC),
        lines=(),
        view=view,
    )


def test_list_counts_match_detail(env) -> None:
    batch = _create_batch(env, "k-batch-detail")
    batch_id = batch["batch_id"]

    listed = _list(env)
    assert listed.status_code == 200, listed.text
    items = listed.json()["items"]
    assert len(items) == 1
    item = items[0]

    detail = env.client.get(
        f"{PREFIX}/recon/runs/{batch_id}",
        params=env.scope_params(system_ids=BOTH_SYSTEMS),
        headers=env.headers(),
    )
    assert detail.status_code == 200, detail.text
    body = detail.json()

    # 同一份投影：列表不得与详情出现第二套计数。
    assert item["batch_id"] == batch_id
    assert item["status"] == body["status"]
    assert item["diff_count"] == body["diff_count"]
    assert item["line_count"] == body["metrics"]["supplier_lines"]
    assert item["diff_amount"] == body["metrics"]["diff_amount"]
    assert item["created_at"] == body["created_at"]


def test_system_scope_containment_filters_batches(env) -> None:
    """批次涉及的系统必须是请求 scope 的子集，与详情 ``_load_snapshot`` 同口径。"""

    _create_batch(env, "k-batch-systems")

    allowed = _list(env)
    assert len(allowed.json()["items"]) == 1

    narrowed = env.client.get(
        f"{PREFIX}/recon/batches",
        params=env.scope_params(system_ids=[cf.SYSTEM]),
        headers=env.headers(),
    )
    assert narrowed.status_code == 200
    assert narrowed.json()["items"] == []


def test_keyset_pagination_has_no_overlap(env) -> None:
    created = {
        _create_batch(env, f"k-page-{index}", supplier_amount=f"50.{index + 10}")[
            "batch_id"
        ]
        for index in range(3)
    }

    first = _list(env, limit=2)
    assert first.status_code == 200, first.text
    page_one = first.json()
    assert len(page_one["items"]) == 2
    assert page_one["next_cursor"]

    ids = [item["batch_id"] for item in page_one["items"]]

    # 用第一页的 next_cursor 取第二页：不重不漏。
    final = _list(env, limit=2, cursor=page_one["next_cursor"])
    assert final.status_code == 200, final.text
    payload = final.json()
    ids.extend(item["batch_id"] for item in payload["items"])
    assert len(ids) == len(set(ids)) == 3
    assert set(ids) == created
    assert payload["next_cursor"] is None


def test_scope_outside_grant_is_403_not_empty_list(env) -> None:
    response = env.client.get(
        f"{PREFIX}/recon/batches",
        params=env.scope_params(org_id="ORG-2", system_ids=BOTH_SYSTEMS),
        headers=env.headers(),
    )

    assert response.status_code == 403
    assert response.json()["code"] == "E-AUTHZ-001"
    assert "items" not in response.json()


def test_empty_scope_returns_empty_list(env) -> None:
    response = _list(env)

    assert response.status_code == 200
    assert response.json() == {"items": [], "next_cursor": None}


def test_invalid_cursor_is_422(env) -> None:
    response = env.client.get(
        f"{PREFIX}/recon/batches",
        params=[
            *env.scope_params(system_ids=BOTH_SYSTEMS),
            ("cursor", "not-a-cursor"),
        ],
        headers=env.headers(),
    )

    assert response.status_code == 422
    assert response.json()["code"] == "E-PARSE-FAILED"


def test_other_org_and_tenant_are_not_listed(env) -> None:
    asyncio.run(
        env.recon_store.save(_snapshot(tenant_id=cf.TENANT, org_id="ORG-2"))
    )
    asyncio.run(
        env.recon_store.save(_snapshot(tenant_id=uuid4(), org_id=cf.ORG))
    )
    asyncio.run(
        env.recon_store.save(_snapshot(tenant_id=cf.TENANT, org_id=cf.ORG))
    )

    response = _list(env)

    assert response.status_code == 200
    assert len(response.json()["items"]) == 1


def test_batch_without_projection_keeps_nulls(env) -> None:
    """旧批次没有投影时返回 null，不从 recon_line 反推、不伪造 0。"""

    asyncio.run(
        env.recon_store.save(
            _snapshot(tenant_id=cf.TENANT, org_id=cf.ORG, view=None)
        )
    )

    response = _list(env)

    assert response.status_code == 200
    item = response.json()["items"][0]
    assert item["status"] == "done"
    assert item["line_count"] is None
    assert item["diff_count"] is None
    assert item["diff_amount"] is None
