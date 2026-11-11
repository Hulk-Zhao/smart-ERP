"""PR4：审批决定落库 + 重启语义 + 孤儿 ``audit_image_id`` 巡检。

三个判定（都是「事实」而不是「设计意图」）：

1. **重启语义**：审批决定落 ``erp.approval_record`` 之后，换一个**新容器实例**
   读同一个库，``GET /v1/tickets/{id}`` 的 ``approvals`` 与重启前逐字段相同，
   ``missing_roles`` 不回到「全部缺失」，``state`` / ``version`` 不变；
2. **两个 approve 入口不是同一动作**：``POST /approvals/{id}/approve`` 只登记
   审批意见（不写状态、不加事件），``POST /tickets/{id}/actions/approve`` 才是
   发起人推进工单（写状态、加事件、执行写计划）；
3. **孤儿 ``audit_image_id`` 巡检**：一条 SQL 同时给出孤儿数与样本，指标进
   ``/metrics``，``WARNING`` 日志可在 ``docker logs`` 看到。

跳过策略与 ``test_ticket_write_sql.py`` 一致：未设置 ``ERP_TEST_DATABASE_URL``
时整组 skip。
"""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime
from decimal import Decimal
from unittest import mock
from uuid import UUID

import conftest as cf
import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from apps.agents.state_machine import TransitionContext
from apps.agents.store import TicketStore
from apps.api import audit_integrity, configure_container
from apps.api.approval_sql import SqlApprovalStore
from apps.api.audit_integrity import AuditIntegrityScanner
from apps.api.container import ApiContainer
from apps.api.store import PendingWrite, TicketRecord
from apps.api.ticket_sql import SqlTicketRepository
from apps.main import create_app
from apps.platform.config import Settings
from apps.platform.contracts.ticket import TicketState
from apps.platform.db.models import TicketStep
from apps.platform.policy.levels import WriteLevel
from observability.metrics import render_metrics

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TENANT = cf.TENANT
INITIATOR = cf.INITIATOR
ORG = cf.ORG
PERIOD = cf.PERIOD
SCENE = cf.SCENE
SYSTEM = cf.SYSTEM


def _upgrade(url: str) -> None:
    config = Config(os.path.join(ROOT, "alembic.ini"))
    config.set_main_option("sqlalchemy.url", url)
    config.set_main_option("script_location", os.path.join(ROOT, "db", "migrations"))
    command.upgrade(config, "head")


async def _wait_ready(url: str, attempts: int = 60) -> None:
    engine = create_async_engine(url, poolclass=NullPool)
    try:
        for _ in range(attempts):
            try:
                async with engine.connect() as conn:
                    await conn.execute(sa.text("SELECT 1"))
                return
            except Exception:  # noqa: BLE001 - 等库起来期间任何连接错误都重试
                await asyncio.sleep(1)
    finally:
        await engine.dispose()
    raise RuntimeError("测试库在 60 秒内没有就绪")


@pytest.fixture(scope="session")
def pg_url() -> str:
    url = os.environ.get("ERP_TEST_DATABASE_URL")
    if not url:
        pytest.skip("未设置 ERP_TEST_DATABASE_URL，跳过 PG 集成用例")
    asyncio.run(_wait_ready(url))
    _upgrade(url)
    return url


@pytest.fixture()
def session_factory(pg_url: str):
    engine = create_async_engine(pg_url, poolclass=NullPool)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def _clean() -> None:
        async with engine.begin() as conn:
            # approval_record 有 FK 到 ticket，CASCADE 会一并清；这里仍显式清一次，
            # 避免将来去掉 FK 时留下跨用例残留。
            await conn.execute(sa.text("TRUNCATE erp.approval_record"))
            await conn.execute(sa.text("TRUNCATE erp.ticket CASCADE"))

    asyncio.run(_clean())
    try:
        yield factory
    finally:
        asyncio.run(engine.dispose())


def _ticket(*, ticket_no: str, pending: PendingWrite | None = None) -> TicketRecord:
    return TicketRecord(
        ticket_no=ticket_no,
        tenant_id=TENANT,
        scene_code=SCENE,
        initiator_id=INITIATOR,
        business_key=f"BK-{ticket_no}",
        org_id=ORG,
        period=PERIOD,
        systems=(SYSTEM,),
        write_level=WriteLevel.W1,
        amount=Decimal("1200.00"),
        touches_fund=False,
        created_at=datetime(2026, 11, 10, 9, 0, 0, tzinfo=UTC),
        pending_write=pending,
    )


def _pending() -> PendingWrite:
    return PendingWrite(
        action_key="u8.trade.save_draft",
        system=SYSTEM,
        payload={"draft_id": "D-P4", "note": "PR4"},
        idem_key="idem-p4",
    )


def _to_pending(repo: SqlTicketRepository, ticket_id: UUID) -> None:
    asyncio.run(
        repo.transition(
            tenant_id=TENANT,
            ticket_id=ticket_id,
            target=TicketState.PENDING_APPROVAL,
            trigger="preview_ok",
            context=TransitionContext(preview_ok=True),
        )
    )


def _detail(env, ticket_id: UUID) -> dict:
    response = env.client.get(
        f"{cf.PREFIX}/tickets/{ticket_id}",
        params=env.scope_params(),
        headers=env.headers(),
    )
    assert response.status_code == 200, response.text
    return response.json()


def _new_instance(env, session_factory) -> ApiContainer:
    """模拟容器重建：新仓储、新审批缓存，只共享令牌与连接器替身。"""

    return ApiContainer(
        tickets=SqlTicketRepository(session_factory=session_factory),
        approval_records=SqlApprovalStore(session_factory=session_factory),
        connectors=env.container.connectors,
        scenes=env.container.scenes,
        actions=env.container.actions,
        price_book=env.container.price_book,
        recon_store=env.container.recon_store,
    )


def test_approvals_survive_container_rebuild(env, session_factory):
    repo = SqlTicketRepository(session_factory=session_factory)
    env.container.tickets = repo
    env.container.approval_records = SqlApprovalStore(session_factory=session_factory)
    created = asyncio.run(
        repo.create(_ticket(ticket_no="T-P4-0001", pending=_pending()))
    )
    _to_pending(repo, created.ticket_id)

    decision = env.client.post(
        f"{cf.PREFIX}/approvals/{created.ticket_id}/approve",
        json={
            "scope": env.scope(),
            "role": "reviewer",
            "comment": "同意（待重启验证）",
        },
        headers=env.headers("reviewer", "k-p4-review"),
    )
    assert decision.status_code == 200, decision.text

    before = _detail(env, created.ticket_id)
    assert len(before["approvals"]) == 1
    assert before["missing_roles"] == []
    assert before["required_roles"] == ["reviewer"]

    # 新实例：内存审批缓存必须是空的，证明后面读到的记录只可能来自库。
    second = _new_instance(env, session_factory)
    assert second.approvals is not env.container.approvals
    assert not second.approvals.records(str(created.ticket_id))
    for token in env.tokens.values():
        second.session_directory.bind(
            token, env.container.session_directory.resolve(token)
        )

    configure_container(second)
    client = TestClient(create_app(Settings(image_tag="test")))
    try:
        response = client.get(
            f"{cf.PREFIX}/tickets/{created.ticket_id}",
            params=env.scope_params(),
            headers=env.headers(),
        )
    finally:
        client.close()
        configure_container(env.container)

    assert response.status_code == 200, response.text
    after = response.json()
    # 逐字段相同：审批人、角色、决定、意见、时间都从库里还原。
    assert after["approvals"] == before["approvals"]
    assert after["missing_roles"] == []
    assert after["required_roles"] == ["reviewer"]
    # 审批意见不推进状态：重建前后 state / version 一致。
    assert after["state"] == before["state"] == TicketState.PENDING_APPROVAL.value
    assert after["version"] == before["version"]

    # 库里的真源也必须是这一条。
    stored = asyncio.run(
        SqlApprovalStore(session_factory=session_factory).list_for(
            tenant_id=TENANT, ticket_id=created.ticket_id
        )
    )
    assert len(stored) == 1
    assert stored[0].decision == "approved"
    assert stored[0].comment == "同意（待重启验证）"


def test_decision_endpoint_and_ticket_approve_are_different_actions(
    env, session_factory
):
    """一句话结论：不是同一动作的两个入口。

    - ``/approvals/{id}/approve`` = 审批人登记意见（不推进、不加事件）；
    - ``/tickets/{id}/actions/approve`` = 发起人推进并执行写计划（走 ``transition``）。
    """

    repo = SqlTicketRepository(session_factory=session_factory)
    env.container.tickets = repo
    env.container.approval_records = SqlApprovalStore(session_factory=session_factory)
    created = asyncio.run(
        repo.create(_ticket(ticket_no="T-P4-0002", pending=_pending()))
    )
    _to_pending(repo, created.ticket_id)
    store = TicketStore(session_factory)

    before = _detail(env, created.ticket_id)
    events_before = asyncio.run(store.list_events(created.ticket_id))
    assert [event.event_type for event in events_before] == ["created", "transition"]

    decided = env.client.post(
        f"{cf.PREFIX}/approvals/{created.ticket_id}/approve",
        json={"scope": env.scope(), "role": "reviewer", "comment": "审批意见"},
        headers=env.headers("reviewer", "k-p4-decide"),
    )
    assert decided.status_code == 200, decided.text
    assert decided.json()["ticket_state"] == TicketState.PENDING_APPROVAL.value

    after_decision = _detail(env, created.ticket_id)
    events_after_decision = asyncio.run(store.list_events(created.ticket_id))
    # 决定只登记意见：状态/版本不变，事件链不新增 transition。
    assert (
        after_decision["state"] == before["state"] == TicketState.PENDING_APPROVAL.value
    )
    assert after_decision["version"] == before["version"]
    assert [event.event_type for event in events_after_decision] == [
        "created",
        "transition",
    ]
    assert after_decision["missing_roles"] == []
    assert after_decision["approvals"][0]["decision"] == "approved"

    approved = env.client.post(
        f"{cf.PREFIX}/tickets/{created.ticket_id}/actions/approve",
        json={"scope": env.scope(), "comment": "推进"},
        headers=env.headers("initiator", "k-p4-approve"),
    )
    assert approved.status_code == 200, approved.text
    body = approved.json()
    assert body["state"] == TicketState.EXECUTING.value
    assert body["version"] == before["version"] + 1
    assert len(env.connector.writes) == 1

    events_after_approve = asyncio.run(store.list_events(created.ticket_id))
    # 先落 step（外部写入已发生），再推进到 executing。
    assert [event.event_type for event in events_after_approve] == [
        "created",
        "transition",
        "step",
        "transition",
    ]


def test_orphan_audit_image_scan_reports_count_samples_metric_and_log(
    env, session_factory
):
    repo = SqlTicketRepository(session_factory=session_factory)
    env.container.tickets = repo
    created = asyncio.run(repo.create(_ticket(ticket_no="T-P4-0003")))

    orphan_a = UUID(int=1)
    orphan_b = UUID(int=2)
    valid_but_not_persisted = UUID(int=3)

    async def _seed() -> None:
        async with session_factory() as session, session.begin():
            for step_no, step_id in enumerate(
                (orphan_a, orphan_b, valid_but_not_persisted), start=1
            ):
                session.add(
                    TicketStep(
                        id=step_id,
                        ticket_id=created.ticket_id,
                        step_no=step_no,
                        action_key="u8.trade.save_draft",
                        target_system=SYSTEM,
                        write_level=1,
                        state="succeeded",
                        idempotency_key=f"idem-orphan-{step_no}",
                        request_hash="0" * 64,
                        audit_image_id=step_id,  # 三行都指向不存在的 audit_image
                        executed_at=datetime(2026, 11, 10, 9, 30, 0, tzinfo=UTC),
                    )
                )

    asyncio.run(_seed())

    scanner = AuditIntegrityScanner(
        session_factory=session_factory,
        metrics=env.container.metrics,
        sample_limit=1,
    )
    # 落点 ②：WARNING 日志带数量与样本。测试进程里 pytest 会抑制 logging
    # （全局 disable + 不走 lastResort），因此断言「调用了 logger.warning」而不是
    # 去抓记录；生产进程没有 root handler 时 WARNING 会经 logging.lastResort
    # 写到 stderr，docker logs 可见。
    with mock.patch.object(audit_integrity.logger, "warning") as warning:
        report = asyncio.run(scanner.scan_once())
    warning.assert_called_once()
    args = warning.call_args.args
    assert args[1] == 3
    assert str(orphan_a) in args[2]

    # 一条 SQL 同时给「数」与「样本」：计数不受样本上限影响。
    assert report.orphan_count == 3
    assert len(report.samples) == 1
    assert report.samples[0]["step_id"] == str(orphan_a)
    assert report.samples[0]["ticket_id"] == str(created.ticket_id)
    assert report.samples[0]["audit_image_id"] == str(orphan_a)

    # 落点 ①：指标进同一个注册表（``/metrics`` 渲染的就是它；端点默认关闭，
    # 开关本身的证据在 ``tests/observability/test_metrics.py``）。
    metrics_text = render_metrics(env.container.metrics)
    assert "smart_erp_audit_orphan_steps 3\n" in metrics_text
