"""对账落库：耐久性、状态集合、事件幂等、等价批次防重。"""

from __future__ import annotations

import asyncio
import json
import os
from datetime import UTC, datetime
from uuid import UUID, uuid4

import conftest as cf

from apps.api import configure_container, reset_container
from apps.api.container import ApiContainer
from apps.api.recon_snapshot import BatchSnapshot, SnapshotLine
from apps.api.recon_store import FileReconStore, NewEvent
from apps.platform.policy.levels import SceneSpec, WriteLevel
from apps.recon.models import LineStatus

PREFIX = cf.PREFIX


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


def _body(env) -> dict[str, object]:
    return {
        "scope": env.scope(system_ids=[cf.SYSTEM, cf.STATEMENT]),
        "suppliers": ["SUP-A"],
        "dataset": {
            "our_lines": [
                _line(system=cf.SYSTEM, doc_no="PO-1", item_code="IT-A", amount="50.00"),
                _line(system=cf.SYSTEM, doc_no="PO-2", item_code="IT-B", amount="50.00"),
            ],
            "supplier_lines": [
                _line(system=cf.STATEMENT, doc_no="PO-1", item_code="IT-A", amount="50.00"),
                _line(system=cf.STATEMENT, doc_no="PO-2", item_code="IT-B", amount="50.50"),
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


def _configure_metering(monkeypatch) -> None:
    monkeypatch.setenv("ERP_METERING_UNIT_PRICE", "0.30")
    monkeypatch.setenv("ERP_METERING_PRIOR_AMOUNT", "1.00")
    monkeypatch.setenv("ERP_METERING_CAP_AMOUNT", "5.00")


def _statement(env_like, batch_id: str):
    return env_like.client.get(
        f"{PREFIX}/metering/batches/{batch_id}/statement",
        params=env_like.scope_params(system_ids=[cf.SYSTEM, cf.STATEMENT]),
        headers=env_like.headers(),
    )


def _rebuild_container(store_path) -> tuple[ApiContainer, str]:
    """模拟进程重建：新的容器 + 空的内存仓储 + 同一个持久化文件。"""

    reset_container()
    container = ApiContainer(
        connectors={cf.SYSTEM: cf.FakeConnector()},
        scenes={cf.SCENE: SceneSpec(code=cf.SCENE, max_level=WriteLevel.W2)},
        recon_store=FileReconStore(store_path),
    )
    container.actions.register(cf._reverse_action())
    container.actions.register(cf._forward_action())
    token = container.session_directory.issue(
        container.broker, cf._principal(cf.INITIATOR, frozenset({"operator"}))
    )
    configure_container(container)
    return container, token


def test_statement_survives_container_rebuild(env, monkeypatch):
    _configure_metering(monkeypatch)
    run = env.client.post(
        f"{PREFIX}/recon/runs", json=_body(env), headers=env.headers("initiator", "k-durable")
    )
    assert run.status_code == 201, run.text
    batch_id = run.json()["batch_id"]
    first = _statement(env, batch_id)
    assert first.status_code == 200, first.text
    before = first.json()

    # 内存仓储清空、容器重建后，同一 batch 仍从持久化快照读出
    assert env.recon_store_path is not None
    container, token = _rebuild_container(env.recon_store_path)
    assert container.recon_batches.get(UUID(batch_id)) is None

    second = env.client.get(
        f"{PREFIX}/metering/batches/{batch_id}/statement",
        params=env.scope_params(system_ids=[cf.SYSTEM, cf.STATEMENT]),
        headers={"X-Session-Token": token},
    )
    assert second.status_code == 200, second.text
    after = second.json()

    before.pop("computed_at")
    after.pop("computed_at")
    assert after == before
    assert after["expected_lines"] == 2
    assert after["auto_released_lines"] == 1
    assert after["excluded_lines"]["dispute_hold"] == 1
    assert after["period_amount"] == "0.3000"


def test_equivalent_batch_is_rejected_without_explicit_rerun(env, monkeypatch):
    _configure_metering(monkeypatch)
    body = _body(env)
    first = env.client.post(
        f"{PREFIX}/recon/runs", json=body, headers=env.headers("initiator", "k-equiv-1")
    )
    assert first.status_code == 201, first.text
    second = env.client.post(
        f"{PREFIX}/recon/runs", json=body, headers=env.headers("initiator", "k-equiv-2")
    )
    assert second.status_code == 409, second.text
    assert second.json()["code"] == "E-IDEM-CONFLICT"

    # 显式重跑（提供 rerun_of + 原因）才允许落第二份快照
    rerun = env.client.post(
        f"{PREFIX}/recon/runs",
        json={**body, "rerun_of": first.json()["batch_id"], "rerun_reason": "首次执行数据源补正"},
        headers=env.headers("initiator", "k-equiv-3"),
    )
    assert rerun.status_code == 201, rerun.text


def test_rerun_requires_reason(env):
    response = env.client.post(
        f"{PREFIX}/recon/runs",
        json={**_body(env), "rerun_of": str(uuid4())},
        headers=env.headers("initiator", "k-rerun-no-reason"),
    )
    assert response.status_code == 422, response.text


def test_persisted_status_set_equals_engine_enum(tmp_path):
    """库里不得有第二份状态枚举：写入什么状态，读出就是什么状态。"""

    store = FileReconStore(tmp_path / "statuses.json")
    batch_id = uuid4()
    tenant_id = uuid4()
    lines = tuple(
        SnapshotLine(
            line_no=index,
            side="supplier",
            supplier_code="SUP-A",
            supplier_ref=f"statement:D-{index}#1",
            our_ref=None,
            status=status.value,
            match_type=None,
            confidence="1",
            needs_manual=status
            in {LineStatus.NEEDS_REVIEW, LineStatus.AMBIGUOUS, LineStatus.CANDIDATE_ONLY},
            diff_types=(),
            diff_facts=(),
            owner=None,
            evidence=(),
            source_refs=(f"statement:D-{index}#1",),
        )
        for index, status in enumerate(LineStatus, start=1)
    )
    snapshot = BatchSnapshot(
        batch_id=batch_id,
        tenant_id=tenant_id,
        org_id=cf.ORG,
        period=cf.PERIOD,
        system_ids=(cf.SYSTEM, cf.STATEMENT),
        suppliers=("SUP-A",),
        status="已对账",
        conclusive=True,
        status_reason="ok",
        metrics={"supplier_lines": len(lines)},
        input_fingerprint="fp",
        rerun_of=None,
        rerun_reason=None,
        created_by=uuid4(),
        created_at=datetime.now(UTC),
        lines=lines,
    )
    asyncio.run(store.save(snapshot))
    loaded = asyncio.run(store.load(batch_id, tenant_id))
    assert loaded is not None
    assert {line.status for line in loaded.lines} == {status.value for status in LineStatus}


def test_event_retry_returns_same_seq(tmp_path):
    store = FileReconStore(tmp_path / "events.json")
    event = NewEvent(
        tenant_id=uuid4(),
        batch_id=uuid4(),
        line_id=uuid4(),
        event="confirmed_wrong",
        actor=uuid4(),
        reason="供应商重复计费",
    )
    moment = datetime(2026, 11, 11, 1, 0, tzinfo=UTC)
    first = asyncio.run(store.append_event(event, now=moment))
    second = asyncio.run(store.append_event(event, now=moment))
    assert first.seq == second.seq
    payload = json.loads((tmp_path / "events.json").read_text(encoding="utf-8"))
    assert len(payload["events"]) == 1


def test_migration_adds_sequence_grant_and_no_status_check() -> None:
    """迁移必须给 identity 序列授权，且不得给 status 加第二份枚举。"""

    path = os.path.join("db", "migrations", "versions", "0003_recon_persistence.py")
    text = open(path, encoding="utf-8").read()  # noqa: SIM115
    assert "GRANT USAGE, SELECT ON SEQUENCE erp.recon_line_event_seq_seq TO erp_app" in text
    assert "GRANT USAGE ON SCHEMA erp TO erp_app" in text
    assert "status text NOT NULL CHECK" not in text
