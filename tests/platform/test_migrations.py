"""迁移可执行性与 PG15 约束（离线渲染，不需要真实数据库）。"""

from __future__ import annotations

from alembic import command
from alembic.config import Config

from apps.platform.contracts.ticket import TICKET_STATES
from apps.platform.db import models  # noqa: F401  确保模型注册进 metadata
from apps.platform.db.base import Base
from apps.platform.contracts.export_contracts import ROOT


def test_baseline_models_are_erp_scoped() -> None:
    tables = {f"{table.schema}.{table.name}" for table in Base.metadata.tables.values()}
    assert tables == {
        "erp.job_queue",
        "erp.ticket",
        "erp.ticket_event",
        "erp.ticket_step",
    }


def test_offline_sql_renders_expected_ddl(capsys) -> None:
    config = Config(str(ROOT / "alembic.ini"))
    command.upgrade(config, "head", sql=True)
    sql = capsys.readouterr().out

    assert "CREATE SCHEMA IF NOT EXISTS erp;" in sql
    assert sql.index("CREATE SCHEMA IF NOT EXISTS erp;") < sql.index("CREATE TABLE erp.job_queue")
    assert "CREATE TABLE erp.ticket (" in sql
    assert "CREATE TABLE erp.ticket_event (" in sql
    assert "CREATE TABLE erp.ticket_step (" in sql
    assert "erp.alembic_version_erp" in sql
    assert "job_queue_pick_idx" in sql
    assert "fn_ticket_event_guard" in sql
    assert "write_level BETWEEN 0 AND 2" in sql
    for state in TICKET_STATES:
        assert f"'{state}'" in sql


def test_no_pg16_only_constructs(capsys) -> None:
    config = Config(str(ROOT / "alembic.ini"))
    command.upgrade(config, "head", sql=True)
    sql = capsys.readouterr().out.lower()

    assert "any_value(" not in sql
    assert "merge into" not in sql
