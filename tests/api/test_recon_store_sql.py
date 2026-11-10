"""``SqlReconStore`` 集成用例：计量表的生产读路径必须真的经过 PostgreSQL。

跳过策略与 ``tests/agents/conftest.py`` 一致：未设置 ``ERP_TEST_DATABASE_URL``
时整组 skip 并写明原因；CI 的 PG15 service 会设置该变量，故 CI 上必须真跑。

覆盖点（对应当前唯一生产路径）：
1. 写入后**新建 store 实例**（等价进程重启）仍能 load 出同一批行；
2. 跨租户 load 必须拿不到数据；
3. 空批次、供应商行缺 ``supplier_code`` 的边界；
4. 落库后直接查 ``erp.recon_line`` 行数，证明不是适配器自己的内存缓存。
"""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from apps.api.metering import MeteringPolicy, build_statement
from apps.api.recon_snapshot import BatchSnapshot, SnapshotLine
from apps.api.recon_store import SqlReconStore

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


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
                    await conn.execute(text("SELECT 1"))
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
            await conn.execute(
                text("TRUNCATE erp.recon_line_event, erp.recon_line, erp.recon_batch CASCADE")
            )

    asyncio.run(_clean())
    try:
        yield factory
    finally:
        asyncio.run(engine.dispose())


def _policy() -> MeteringPolicy:
    return MeteringPolicy(
        unit_price=Decimal("2.00"),
        currency="CNY",
        cap_amount=None,
        prior_amount=Decimal(0),
    )


def _line(
    *,
    line_no: int,
    status: str,
    supplier_code: str | None = "SUP-A",
    needs_manual: bool = False,
    match_type: str | None = "exact",
    diff_types: tuple[str, ...] = (),
    diff_facts: tuple[dict, ...] = (),
    owner: str | None = None,
) -> SnapshotLine:
    return SnapshotLine(
        line_no=line_no,
        side="supplier",
        supplier_code=supplier_code,
        supplier_ref=f"statement:PO-{line_no}#1",
        our_ref=f"u8:PO-{line_no}#1" if status == "已匹配" else None,
        status=status,
        match_type=match_type,
        confidence="1" if status == "已匹配" else "0.5",
        needs_manual=needs_manual,
        diff_types=diff_types,
        diff_facts=diff_facts,
        owner=owner,
        evidence=(),
        source_refs=(f"statement:PO-{line_no}#1",),
    )


def _snapshot(
    lines: tuple[SnapshotLine, ...],
    *,
    supplier_lines: int | None = None,
    view: dict | None = None,
) -> BatchSnapshot:
    return BatchSnapshot(
        batch_id=uuid4(),
        tenant_id=uuid4(),
        org_id="ORG-1",
        period="2026-10",
        system_ids=("u8", "statement"),
        suppliers=("SUP-A",),
        status="已完成",
        conclusive=True,
        status_reason="集成用例",
        metrics={"supplier_lines": len(lines) if supplier_lines is None else supplier_lines},
        input_fingerprint=f"fp-{uuid4()}",
        rerun_of=None,
        rerun_reason=None,
        created_by=uuid4(),
        created_at=datetime.now(UTC),
        lines=lines,
        view=view,
    )


def _load(store: SqlReconStore, batch_id: UUID, tenant_id: UUID) -> BatchSnapshot | None:
    return asyncio.run(store.load(batch_id, tenant_id))


def test_roundtrip_survives_new_store_instance(session_factory) -> None:
    snapshot = _snapshot(
        (
            _line(line_no=1, status="已匹配"),
            _line(
                line_no=2,
                status="待人工",
                supplier_code=None,
                needs_manual=True,
                match_type=None,
                diff_types=("UNIT_PRICE",),
                diff_facts=(
                    {
                        "type": "unit_price",
                        "owner": "supplier",
                        "confidence": "0.90",
                        "ref": "statement:PO-2#1",
                        "supplier_code": None,
                    },
                ),
                owner="supplier",
            ),
            _line(
                line_no=3,
                status="供应商未匹配",
                needs_manual=False,
                match_type=None,
                diff_types=("MISSING_DOC",),
            ),
        )
    )
    asyncio.run(SqlReconStore(session_factory).save(snapshot))

    # 新建实例 = 新引擎、新连接池，等价于进程重启后只靠库里数据
    restarted = SqlReconStore(session_factory)
    loaded = _load(restarted, snapshot.batch_id, snapshot.tenant_id)

    assert loaded is not None
    assert loaded.batch_id == snapshot.batch_id
    assert loaded.org_id == snapshot.org_id
    assert loaded.period == snapshot.period
    assert [line.line_no for line in loaded.lines] == [1, 2, 3]
    assert [line.status for line in loaded.lines] == ["已匹配", "待人工", "供应商未匹配"]
    assert loaded.lines[1].supplier_code is None
    assert loaded.lines[1].needs_manual is True
    assert loaded.lines[1].diff_facts[0]["confidence"] == "0.90"
    assert loaded.lines[0].auto_released is True

    statement = build_statement(loaded, _policy())
    assert statement.billable_lines == 1
    assert statement.auto_released_lines == 1
    assert statement.excluded_lines.dispute_hold == 1
    assert statement.excluded_lines.other_unbillable == 1


def test_rows_are_in_postgres_not_adapter_memory(session_factory, pg_url: str) -> None:
    snapshot = _snapshot((_line(line_no=1, status="已匹配"), _line(line_no=2, status="已匹配")))
    asyncio.run(SqlReconStore(session_factory).save(snapshot))

    engine = create_async_engine(pg_url, poolclass=NullPool)

    async def _count() -> int:
        async with engine.connect() as conn:
            result = await conn.execute(
                text("SELECT count(*) FROM erp.recon_line WHERE batch_id = :batch"),
                {"batch": snapshot.batch_id},
            )
            return int(result.scalar_one())

    try:
        assert asyncio.run(_count()) == 2
    finally:
        asyncio.run(engine.dispose())


def test_cross_tenant_load_is_empty(session_factory) -> None:
    snapshot = _snapshot((_line(line_no=1, status="已匹配"),))
    asyncio.run(SqlReconStore(session_factory).save(snapshot))

    store = SqlReconStore(session_factory)
    assert _load(store, snapshot.batch_id, snapshot.tenant_id) is not None
    assert _load(store, snapshot.batch_id, uuid4()) is None
    assert _load(store, uuid4(), snapshot.tenant_id) is None


def test_empty_batch_roundtrip(session_factory) -> None:
    snapshot = _snapshot((), supplier_lines=0)
    asyncio.run(SqlReconStore(session_factory).save(snapshot))

    loaded = _load(SqlReconStore(session_factory), snapshot.batch_id, snapshot.tenant_id)
    assert loaded is not None
    assert loaded.lines == ()
    statement = build_statement(loaded, _policy())
    assert statement.expected_lines == 0
    assert statement.billable_lines == 0
    assert statement.period_amount == "0.0000"


def test_view_projection_roundtrips_through_postgres(session_factory) -> None:
    """视图投影（jsonb）跨实例读出同形；未投影的旧行保持 NULL，不伪装成 {}。"""

    view = {
        "view_version": 1,
        "batch": {"status": "已对账", "diff_count": 1},
        "diffs": [{"diff_id": "d0", "type": "unit_price", "amount": {"amount": "5.0000"}}],
        "details": {"d0": {"related_lines": [], "draft": None, "unattributed": None}},
    }
    projected = _snapshot((_line(line_no=1, status="已匹配"),), view=view)
    legacy = _snapshot((_line(line_no=1, status="已匹配"),))
    store = SqlReconStore(session_factory)
    asyncio.run(store.save(projected))
    asyncio.run(store.save(legacy))

    restarted = SqlReconStore(session_factory)
    loaded = _load(restarted, projected.batch_id, projected.tenant_id)
    assert loaded is not None
    assert loaded.view == view
    assert _load(restarted, legacy.batch_id, legacy.tenant_id).view is None


def test_missing_supplier_code_surfaces_as_data_gap(session_factory) -> None:
    snapshot = _snapshot(
        (
            _line(line_no=1, status="已匹配", supplier_code=None),
            _line(line_no=2, status="已匹配", supplier_code="SUP-A"),
        )
    )
    asyncio.run(SqlReconStore(session_factory).save(snapshot))

    loaded = _load(SqlReconStore(session_factory), snapshot.batch_id, snapshot.tenant_id)
    assert loaded is not None
    assert loaded.lines[0].supplier_code is None
    statement = build_statement(loaded, _policy())
    assert "supplier_code" in {gap.field for gap in statement.data_gaps}
