"""守卫：迁移 head 建出的表集合 == ORM metadata 表集合（双向差集为空）。

为什么要有这条：测试夹具会按 metadata 建表（``create_all`` / ``checkfirst``），
即使迁移漏写也照样绿，生产却直接缺表。这里用离线 ``upgrade head --sql`` 渲染
迁移实际会建的 DDL，与 ``apps.platform.db.registry.table_names()``（各流已声明
的全部表）做双向比对：任何「加了模型没加迁移」或「迁移建了模型里没有的表」
都会当场失败，不必等到上线。API 流用原生 SQL 的 recon 四表登记在
``MIGRATION_ONLY_TABLES``，其余任何差异都不放过。
"""

from __future__ import annotations

import io
import re
from contextlib import redirect_stdout
from pathlib import Path

from alembic import command
from alembic.config import Config

from apps.platform.db.registry import table_names

ROOT = Path(__file__).resolve().parents[2]
VERSION_TABLE = "erp.alembic_version_erp"

# API 流（0003_recon_persistence）用原生 SQL 读写，不声明 ORM 模型；
# 这里只登记「迁移有、metadata 没有」的已知例外，新增例外必须写清归属，
# 否则守卫会退化成永远绿的摆设。
MIGRATION_ONLY_TABLES: frozenset[str] = frozenset(
    {
        "erp.recon_batch",
        "erp.recon_line",
        "erp.recon_line_event",
        "erp.recon_line_link",
    }
)


def _migrated_tables() -> set[str]:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "db" / "migrations"))
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        command.upgrade(config, "head", sql=True)
    created = set(re.findall(r"CREATE TABLE erp\.([a-z_]+)", buffer.getvalue()))
    return {f"erp.{name}" for name in created} - {VERSION_TABLE}


def test_migration_head_matches_orm_metadata() -> None:
    migrated = _migrated_tables()
    models = set(table_names())

    missing = sorted(models - migrated)
    orphan = sorted((migrated - models) - MIGRATION_ONLY_TABLES)
    assert not missing, f"模型已声明但迁移 head 未建表（生产会缺表）: {missing}"
    assert not orphan, f"迁移 head 建了 metadata 里没有的表（表名拼错或未登记模型）: {orphan}"
    assert migrated == models | MIGRATION_ONLY_TABLES


def test_runtime_tables_are_in_both_sides() -> None:
    """本流新增表必须同时出现在 metadata 与迁移里，防止断言空转。"""

    expected = {
        "erp.ticket_event_anchor",
        "erp.audit_guard_state",
        "erp.worker_heartbeat",
        "erp.scheduler_lease",
        "erp.metering_price_book",
    }
    assert expected <= set(table_names())
    assert expected <= _migrated_tables()
