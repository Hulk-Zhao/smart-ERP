"""审计读路径接 SQL：同源、等价性、租户 404 / scope 403。

三条要证明的事实：

1. ``/v1/audit/images`` 的影像行来自 ``erp.audit_image``，并且与巡检共用
   ``apps/audit/image_link.py`` 里的同一个关联谓词（``STEP_IMAGE_PREDICATE``）；
2. 同一工单、同一批被步骤引用的影像，SQL 读取与内存回放逐字段一致；
   未被任何步骤引用的影像行是方向差异，读路径不列出，也不计入
   ``B-AUDIT-ORPHAN``（用例显式断言差异，不改测试迁就实现）；
3. 跨租户读一律 404（不泄露存在性），同租户 scope 不覆盖才 403。
"""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import conftest as cf
import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from apps.api.audit_integrity import AuditIntegrityScanner
from apps.api.store import TicketRecord, TicketStepRecord
from apps.api.ticket_sql import SqlTicketRepository
from apps.audit.image_link import ORPHAN_SQL
from apps.audit.ledger import AuditLedger, AuditQuery
from apps.audit.store import InMemoryAuditStore, SqlAuditStore
from apps.platform.contracts.audit import AuditImage
from apps.platform.contracts.ticket import TicketState
from apps.platform.policy.levels import WriteLevel

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TENANT = cf.TENANT
ORG = cf.ORG
PERIOD = cf.PERIOD
SCENE = cf.SCENE
SYSTEM = cf.SYSTEM
PREFIX = cf.PREFIX

HASH_A = "a" * 64
HASH_B = "b" * 64
HASH_C = "c" * 64


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
            await conn.execute(sa.text("TRUNCATE erp.audit_anchor"))
            await conn.execute(sa.text("TRUNCATE erp.ticket CASCADE"))

    asyncio.run(_clean())
    try:
        yield factory
    finally:
        asyncio.run(engine.dispose())


def _ticket(
    *, ticket_no: str, tenant_id: UUID = TENANT, org_id: str = ORG
) -> TicketRecord:
    return TicketRecord(
        ticket_no=ticket_no,
        tenant_id=tenant_id,
        scene_code=SCENE,
        initiator_id=cf.INITIATOR,
        business_key=f"BK-{ticket_no}",
        org_id=org_id,
        period=PERIOD,
        systems=(SYSTEM,),
        write_level=WriteLevel.W1,
        amount=Decimal("1200.00"),
        touches_fund=False,
        state=TicketState.EXECUTING,
        created_at=datetime(2026, 11, 10, 9, 0, 0, tzinfo=UTC),
    )


def _image(
    *,
    tenant_id: UUID,
    ticket_id: UUID,
    step_id: UUID,
    object_id: str,
    prev_hash: str,
    hash_: str,
    created_at: datetime,
) -> AuditImage:
    return AuditImage(
        id=uuid4(),
        tenant_id=tenant_id,
        ticket_id=ticket_id,
        step_id=step_id,
        system=SYSTEM,
        object_type="draft",
        object_id=object_id,
        before_ref=f"mem://before/{object_id}",
        after_ref=f"mem://after/{object_id}",
        field_diff=[],
        diff_missing=False,
        prev_hash=prev_hash,
        hash=hash_,
        created_at=created_at,
    )


def _step(
    *,
    step_no: int,
    object_id: str,
    audit_image_id: UUID | None,
    step_id: UUID | None = None,
) -> TicketStepRecord:
    return TicketStepRecord(
        step_no=step_no,
        action_key="u8.trade.save_draft",
        system=SYSTEM,
        object_type="draft",
        object_id=object_id,
        level=WriteLevel.W1,
        idem_key=f"idem-{step_no}",
        step_id=step_id or uuid4(),
        executed_at=datetime(2026, 11, 10, 9, 0, 0, tzinfo=UTC),
        audit_image_id=audit_image_id,
    )


def _record(repo: SqlTicketRepository, ticket_id: UUID, step: TicketStepRecord) -> None:
    asyncio.run(
        repo.record_step(
            tenant_id=TENANT,
            ticket_id=ticket_id,
            step=step,
            request_hash=f"req-{step.step_no}",
        )
    )


def _seed_store_fixture(
    repo: SqlTicketRepository, *, ticket_no: str
) -> tuple[TicketRecord, list[AuditImage]]:
    """两条步骤各引用一条影像，链首/链尾手工指定，供 store 层等价性对比。"""

    created = asyncio.run(repo.create(_ticket(ticket_no=ticket_no)))
    images: list[AuditImage] = []
    prev = ""
    for index in range(2):
        step_id = uuid4()
        image = _image(
            tenant_id=TENANT,
            ticket_id=created.ticket_id,
            step_id=step_id,
            object_id=f"D-{ticket_no}-{index}",
            prev_hash=prev,
            hash_=(HASH_A if index == 0 else HASH_B),
            created_at=datetime(2026, 11, 10, 9, 0, index, tzinfo=UTC),
        )
        _record(
            repo,
            created.ticket_id,
            _step(
                step_no=index + 1,
                object_id=image.object_id,
                audit_image_id=image.id,
                step_id=step_id,
            ),
        )
        images.append(image)
        prev = image.hash
    return created, images


def test_images_sql_matches_memory_for_referenced_rows(session_factory) -> None:
    repo = SqlTicketRepository(session_factory=session_factory)
    created, images = _seed_store_fixture(repo, ticket_no="T-A1")

    sql_store = SqlAuditStore(session_factory=session_factory)
    memory_store = InMemoryAuditStore()
    for image in images:
        asyncio.run(sql_store.append(image))
        asyncio.run(memory_store.append(image))

    query = AuditQuery(tenant_id=TENANT, ticket_id=created.ticket_id)
    from_sql = asyncio.run(sql_store.list_images(query))
    from_memory = asyncio.run(memory_store.list_images(query))

    assert [image.id for image in from_sql] == [image.id for image in from_memory]
    assert [image.model_dump() for image in from_sql] == [
        image.model_dump() for image in from_memory
    ]


def test_unreferenced_image_is_listed_only_by_memory(session_factory) -> None:
    """方向差异：没有步骤指向的影像，SQL 读路径不列、巡检也不算孤儿。"""

    repo = SqlTicketRepository(session_factory=session_factory)
    created, images = _seed_store_fixture(repo, ticket_no="T-A2")

    sql_store = SqlAuditStore(session_factory=session_factory)
    for image in images:
        asyncio.run(sql_store.append(image))
    stray = _image(
        tenant_id=TENANT,
        ticket_id=created.ticket_id,
        step_id=uuid4(),
        object_id="D-STRAY",
        prev_hash=images[-1].hash,
        hash_=HASH_C,
        created_at=images[-1].created_at + timedelta(seconds=1),
    )
    asyncio.run(sql_store.append(stray))

    memory_store = InMemoryAuditStore()
    for image in [*images, stray]:
        asyncio.run(memory_store.append(image))

    query = AuditQuery(tenant_id=TENANT, ticket_id=created.ticket_id)
    from_sql = asyncio.run(sql_store.list_images(query))
    from_memory = asyncio.run(memory_store.list_images(query))

    # 差异是设计口径，不是实现 bug：SQL 只列被步骤引用的影像。
    assert {image.id for image in from_sql} == {image.id for image in images}
    assert stray.id in {image.id for image in from_memory}

    async def _orphans() -> tuple[int, str | None]:
        async with session_factory() as session:
            row = (await session.execute(ORPHAN_SQL, {"sample_limit": 5})).one()
            return int(row.orphan_count), row.samples

    count, samples = asyncio.run(_orphans())
    assert count == 0, samples


def test_dangling_step_pointer_is_counted_by_the_same_predicate(session_factory) -> None:
    """步骤指向不存在的影像：巡检按同一谓词算孤儿，读路径不会凭空造一行。"""

    repo = SqlTicketRepository(session_factory=session_factory)
    created, images = _seed_store_fixture(repo, ticket_no="T-A3")
    _record(
        repo,
        created.ticket_id,
        _step(step_no=9, object_id="D-MISSING", audit_image_id=uuid4()),
    )
    sql_store = SqlAuditStore(session_factory=session_factory)
    for image in images:
        asyncio.run(sql_store.append(image))

    listing = asyncio.run(
        sql_store.list_images(AuditQuery(tenant_id=TENANT, ticket_id=created.ticket_id))
    )
    assert [image.id for image in listing] == [image.id for image in images]

    scanner = AuditIntegrityScanner(session_factory=session_factory)
    report = asyncio.run(scanner.scan_once())
    assert report.orphan_count == 1


def _wire_sql(env, session_factory) -> None:
    env.container.tickets = SqlTicketRepository(session_factory=session_factory)
    env.container.ledger = AuditLedger(
        store=SqlAuditStore(session_factory=session_factory)
    )


def _seed_through_ledger(env, *, ticket_no: str, count: int = 2) -> TicketRecord:
    """走唯一审计写入口（真实哈希链）造证据，步骤回填 ``audit_image_id``。"""

    repo = env.container.tickets
    created = asyncio.run(repo.create(_ticket(ticket_no=ticket_no)))
    for index in range(count):
        step_id = uuid4()
        image = asyncio.run(
            env.container.ledger.append_image(
                tenant_id=TENANT,
                ticket_id=created.ticket_id,
                step_id=step_id,
                system=SYSTEM,
                object_type="draft",
                object_id=f"D-{ticket_no}-{index}",
                before_ref=f"mem://before/{index}",
                after_ref=f"mem://after/{index}",
                created_at=datetime(2026, 11, 10, 9, 0, index, tzinfo=UTC),
            )
        )
        _record(
            repo,
            created.ticket_id,
            _step(
                step_no=index + 1,
                object_id=image.object_id,
                audit_image_id=image.id,
                step_id=step_id,
            ),
        )
    return created


def test_read_paths_status_codes_with_sql_store(env, session_factory) -> None:
    _wire_sql(env, session_factory)
    created = _seed_through_ledger(env, ticket_no="T-A4")

    images_response = env.client.get(
        f"{PREFIX}/audit/tickets/{created.ticket_id}/images",
        params=env.scope_params(),
        headers=env.headers("auditor"),
    )
    assert images_response.status_code == 200, images_response.text
    items = images_response.json()["items"]
    assert [item["object_id"] for item in items] == ["D-T-A4-0", "D-T-A4-1"]

    chain_response = env.client.get(
        f"{PREFIX}/audit/tickets/{created.ticket_id}/chain",
        params=env.scope_params(),
        headers=env.headers("auditor"),
    )
    assert chain_response.status_code == 200, chain_response.text
    assert chain_response.json()["ok"] is True
    assert chain_response.json()["image_count"] == 2

    export_response = env.client.get(
        f"{PREFIX}/audit/export",
        params=[*env.scope_params(), ("ticket_id", str(created.ticket_id))],
        headers=env.headers("auditor"),
    )
    assert export_response.status_code == 200, export_response.text
    assert "D-T-A4-0" in export_response.text
    assert export_response.headers["X-Content-SHA256"]

    # 同租户但 scope 不覆盖 → 403，不是空数组。
    scope_denied = env.client.get(
        f"{PREFIX}/audit/tickets/{created.ticket_id}/images",
        params=env.scope_params(org_id="ORG-2"),
        headers=env.headers("auditor"),
    )
    assert scope_denied.status_code == 403
    assert scope_denied.json()["code"] == "E-AUTHZ-001"

    # 跨租户 → 404，不能用 403 泄露资源存在性。
    foreign = asyncio.run(
        env.container.tickets.create(
            _ticket(ticket_no="T-A5", tenant_id=uuid4())
        )
    )
    cross_tenant = env.client.get(
        f"{PREFIX}/audit/tickets/{foreign.ticket_id}/images",
        params=env.scope_params(),
        headers=env.headers("auditor"),
    )
    assert cross_tenant.status_code == 404
    assert "items" not in cross_tenant.json()
