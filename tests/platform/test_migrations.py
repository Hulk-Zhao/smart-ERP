"""迁移可执行性与 PG15 约束（离线渲染，不需要真实数据库）。

表集合断言分两层，避免「任何流加表都红」：
1. ``BASELINE_TABLES``：显式冻结的已交付表集合，少一张即失败（防误删/回退）；
2. ``apps.platform.db.registry`` 汇总各流模型（可选导入，缺模块不报红），与迁移实际
   建出的表做双向差集：漏写迁移 → missing 报警；拼错表名 → orphan 报警。

模型清单由注册表统一发现（按 Table 对象身份去重，共享 ``Base`` 不会被误判重复），
新增流只要把模型模块登记进 ``MODEL_MODULES`` 并写迁移即可，不必改本文件。

另有一条独立校验：revision id 长度不得超过 Alembic 版本列宽度（宽度从 Alembic 实际
使用的版本表定义解析，不写死常数），防止「空库 upgrade 直接 value too long」复发。
"""

from __future__ import annotations

import io
import re
from contextlib import redirect_stdout

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory

from apps.platform.contracts.export_contracts import ROOT
from apps.platform.contracts.ticket import TICKET_STATES
from apps.platform.db.registry import MODEL_MODULES, load_model_modules, table_names

# 0001 + 0002 已交付的表（显式冻结；新增表走注册表对比，不必改这里）
BASELINE_TABLES: frozenset[str] = frozenset(
    {
        "erp.job_queue",
        "erp.ticket",
        "erp.ticket_event",
        "erp.ticket_step",
        "erp.connector_idempotency",
        "erp.connector_rate_bucket",
        "erp.connector_breaker_state",
        "erp.compensation_task",
        "erp.audit_image",
        "erp.audit_anchor",
        "erp.approval_record",
        "erp.config_change",
        "erp.credential_lease",
    }
)

VERSION_TABLE = "alembic_version_erp"
VERSION_TABLE_SCHEMA = "erp"


def _render_sql() -> str:
    config = Config(str(ROOT / "alembic.ini"))
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        command.upgrade(config, "head", sql=True)
    return buffer.getvalue()


def _migrated_tables(sql: str) -> set[str]:
    created = {
        f"{VERSION_TABLE_SCHEMA}.{name}"
        for name in re.findall(rf"CREATE TABLE {VERSION_TABLE_SCHEMA}\.([a-z_]+)", sql)
    }
    return created - {f"{VERSION_TABLE_SCHEMA}.{VERSION_TABLE}"}


def _version_column_width() -> int:
    """从 Alembic 实际使用的版本表定义读取 varchar 宽度（唯一真源，不写死 32）。"""

    context = MigrationContext.configure(
        dialect_name="postgresql",
        opts={
            "version_table": VERSION_TABLE,
            "version_table_schema": VERSION_TABLE_SCHEMA,
        },
    )
    version_table = context._version  # Alembic 内部构造的版本表
    assert version_table.name == VERSION_TABLE
    assert version_table.schema == VERSION_TABLE_SCHEMA
    length = getattr(version_table.c.version_num.type, "length", None)
    assert isinstance(length, int) and length > 0, "无法从 Alembic 解析版本列宽度"
    return length


def test_all_model_tables_are_erp_scoped() -> None:
    for qualified in table_names():
        assert qualified.startswith(f"{VERSION_TABLE_SCHEMA}."), (
            f"表必须落在 {VERSION_TABLE_SCHEMA} schema: {qualified}"
        )


def test_model_registry_handles_missing_modules() -> None:
    """注册表必须登记全部候选模块；缺失模块给原因而不是让整棵树报红。"""

    modules = load_model_modules()
    assert {module.name for module in modules} == set(MODEL_MODULES)
    assert modules[0].loaded, "S0 平台模型模块必须存在"
    for module in modules:
        if not module.loaded:
            assert module.missing, f"{module.name} 缺失但未给出原因"
    assert {"erp.ticket", "erp.ticket_event", "erp.ticket_step"} <= table_names()


def test_baseline_tables_are_all_migrated() -> None:
    migrated = _migrated_tables(_render_sql())
    missing = BASELINE_TABLES - migrated
    assert not missing, f"基线表未建出（迁移回退或漏写）: {sorted(missing)}"


def test_migrations_match_model_metadata() -> None:
    """双向差集：漏迁移、多建表都会失败并打印差集。"""

    migrated = _migrated_tables(_render_sql())
    models = table_names()

    missing = models - migrated
    orphan = migrated - models
    assert not missing, f"模型已声明但迁移未建表: {sorted(missing)}"
    assert not orphan, (
        "迁移建了模型里不存在的表（表名拼错，或需在 MODEL_MODULES 登记所属流）: "
        f"{sorted(orphan)}"
    )
    assert migrated == models


def test_revision_ids_fit_version_column() -> None:
    """revision id 超过版本列宽度会让空库 upgrade 直接 value too long。"""

    width = _version_column_width()
    script_dir = ScriptDirectory.from_config(Config(str(ROOT / "alembic.ini")))
    too_long = {
        script.revision: len(script.revision)
        for script in script_dir.walk_revisions()
        if len(script.revision) > width
    }
    assert not too_long, (
        f"revision id 超过 Alembic 版本列 VARCHAR({width})，空库 upgrade 会失败: {too_long}"
    )


def test_offline_sql_renders_expected_ddl() -> None:
    sql = _render_sql()

    assert "CREATE SCHEMA IF NOT EXISTS erp;" in sql
    assert sql.index("CREATE SCHEMA IF NOT EXISTS erp;") < sql.index("CREATE TABLE erp.job_queue")
    for table in sorted(BASELINE_TABLES):
        assert f"CREATE TABLE {table} (" in sql, table
    assert "erp.alembic_version_erp" in sql
    assert "job_queue_pick_idx" in sql
    assert "fn_ticket_event_guard" in sql
    assert "fn_append_only" in sql
    assert "fn_audit_image_chain_guard" in sql
    assert "compensation_task_one_running_idx" in sql
    assert "connector_idempotency_connector_action_created_idx" in sql
    assert "NUMERIC(12, 4)" in sql
    assert "'in_flight'" in sql
    assert "'half_open'" in sql
    assert "GENERATED ALWAYS AS IDENTITY" in sql
    assert "write_level BETWEEN 0 AND 2" in sql
    for state in TICKET_STATES:
        assert f"'{state}'" in sql


def test_no_pg16_only_constructs() -> None:
    sql = _render_sql().lower()

    assert "any_value(" not in sql
    assert "merge into" not in sql
