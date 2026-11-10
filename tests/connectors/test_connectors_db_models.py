"""连接器三表的 ORM 模型注册与关键约束（与 0002 的 DDL 对齐，不新发明列）。"""

from __future__ import annotations

from sqlalchemy import Numeric

from apps.connectors.db import models as connector_models
from apps.platform.db.base import Base

TABLES = (
    "erp.connector_idempotency",
    "erp.connector_rate_bucket",
    "erp.connector_breaker_state",
)


def _table(name: str):
    return Base.metadata.tables[f"erp.{name}"]


def _check_sql(table, name: str) -> str:
    for constraint in table.constraints:
        if constraint.__class__.__name__ == "CheckConstraint" and constraint.name == name:
            return str(constraint.sqltext)
    raise AssertionError(f"{table.name} 缺少约束 {name}")


def test_three_tables_registered_on_platform_base() -> None:
    assert connector_models.ConnectorIdempotency.__tablename__ == "connector_idempotency"
    for qualified in TABLES:
        assert qualified in Base.metadata.tables, f"未注册进 S0 Base.metadata: {qualified}"
        assert Base.metadata.tables[qualified].schema == "erp"


def test_idempotency_unique_key_and_columns() -> None:
    table = _table("connector_idempotency")
    assert [column.name for column in table.columns] == [
        "idempotency_key",
        "connector",
        "action_key",
        "request_hash",
        "status",
        "response",
        "created_at",
        "updated_at",
        "expires_at",
    ]
    # 幂等唯一键：idempotency_key 即主键（同键不可能落两行）
    assert [column.name for column in table.primary_key.columns] == ["idempotency_key"]
    assert table.primary_key.name == "pk_connector_idempotency"
    assert table.columns["expires_at"].nullable is True
    assert str(table.columns["created_at"].server_default.arg) == "now()"

    status_sql = _check_sql(table, "ck_connector_idempotency_status")
    for value in connector_models.IDEM_RECORD_STATUSES:
        assert f"'{value}'" in status_sql


def test_idempotency_lookup_index() -> None:
    table = _table("connector_idempotency")
    indexes = {index.name: [column.name for column in index.columns] for index in table.indexes}
    assert indexes["connector_idempotency_connector_action_created_idx"] == [
        "connector",
        "action_key",
        "created_at",
    ]


def test_rate_bucket_primary_key_and_columns() -> None:
    table = _table("connector_rate_bucket")
    assert [column.name for column in table.columns] == [
        "bucket_key",
        "tokens",
        "updated_at",
        "quota_day",
        "quota_used",
        "quota_limit",
    ]
    # 每个动作/读通道一个桶，bucket_key 是唯一键
    assert [column.name for column in table.primary_key.columns] == ["bucket_key"]
    assert table.primary_key.name == "pk_connector_rate_bucket"
    tokens = table.columns["tokens"]
    assert isinstance(tokens.type, Numeric)
    assert (tokens.type.precision, tokens.type.scale) == (12, 4)
    assert str(table.columns["quota_used"].server_default.arg) == "0"
    # 0002 的 DDL 里 updated_at 没有默认值，由补充令牌的 UPDATE 显式写入
    assert table.columns["updated_at"].server_default is None


def test_breaker_primary_key_and_state_check() -> None:
    table = _table("connector_breaker_state")
    assert [column.name for column in table.columns] == [
        "connector",
        "state",
        "consecutive_failures",
        "opened_at",
        "updated_at",
    ]
    # 每个连接器一条熔断状态
    assert [column.name for column in table.primary_key.columns] == ["connector"]
    assert table.primary_key.name == "pk_connector_breaker_state"
    assert table.columns["opened_at"].nullable is True
    assert str(table.columns["consecutive_failures"].server_default.arg) == "0"

    state_sql = _check_sql(table, "ck_connector_breaker_state_state")
    assert connector_models.BREAKER_STATES == ("closed", "open", "half_open")
    for value in connector_models.BREAKER_STATES:
        assert f"'{value}'" in state_sql
