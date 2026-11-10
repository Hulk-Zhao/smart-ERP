"""连接器运行时状态三表：幂等记录 / 限流桶 / 熔断状态。

字段与约束严格照 ``apps/connectors/MIGRATION_NEEDS.md`` 与
``db/migrations/versions/0002_s1s3s4_merge.py`` 已建的实际 DDL，不新发明列。

导入本模块即把三张表注册进 S0 的 ``Base.metadata``（schema 固定 ``erp``），
由平台层的中央模型注册表统一 import；本模块不依赖任何业务运行时状态。

运行时装配仍是内存实现（``apps/connectors/{idempotency,ratelimit,breaker}.py``），
换 PG 实现时只需按同名接口替换，不改调用方。
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    Index,
    Integer,
    Numeric,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from apps.connectors.breaker import BreakerState
from apps.platform.db.base import SCHEMA, Base

# 落库的幂等记录状态（区别于内存实现的 IdemStatus 判定结果）
IDEM_RECORD_STATUSES: tuple[str, ...] = ("in_flight", "succeeded", "failed")
# 与 CircuitBreaker 共用一套状态字面量，避免「代码改了、DB CHECK 没改」
BREAKER_STATES: tuple[str, ...] = tuple(state.value for state in BreakerState)


def _in_list(column: str, values: tuple[str, ...]) -> str:
    joined = ", ".join(f"'{value}'" for value in values)
    return f"{column} IN ({joined})"


class ConnectorIdempotency(Base):
    """写入幂等的真源（G-02 第二层键）：同键同参可 replay，同键不同参冲突。"""

    __tablename__ = "connector_idempotency"
    __table_args__ = (
        # 命名约定会拼成 ck_connector_idempotency_status，与 0002 的 DDL 一致
        CheckConstraint(_in_list("status", IDEM_RECORD_STATUSES), name="status"),
        Index(
            "connector_idempotency_connector_action_created_idx",
            "connector",
            "action_key",
            "created_at",
        ),
        {"schema": SCHEMA},
    )

    idempotency_key: Mapped[str] = mapped_column(Text, primary_key=True)
    connector: Mapped[str] = mapped_column(Text, nullable=False)
    action_key: Mapped[str] = mapped_column(Text, nullable=False)
    request_hash: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    response: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class ConnectorRateBucket(Base):
    """跨进程令牌桶 + 日配额（可重建，丢了最多短暂超发）。"""

    __tablename__ = "connector_rate_bucket"
    __table_args__ = ({"schema": SCHEMA},)

    bucket_key: Mapped[str] = mapped_column(Text, primary_key=True)
    tokens: Mapped[Decimal] = mapped_column(Numeric(12, 4), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    quota_day: Mapped[date] = mapped_column(Date, nullable=False)
    quota_used: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    quota_limit: Mapped[int] = mapped_column(Integer, nullable=False)


class ConnectorBreakerState(Base):
    """熔断状态（可重建，重启后保守从 closed 或 open 起）。"""

    __tablename__ = "connector_breaker_state"
    __table_args__ = (
        # 命名约定会拼成 ck_connector_breaker_state_state，与 0002 的 DDL 一致
        CheckConstraint(_in_list("state", BREAKER_STATES), name="state"),
        {"schema": SCHEMA},
    )

    connector: Mapped[str] = mapped_column(Text, primary_key=True)
    state: Mapped[str] = mapped_column(Text, nullable=False)
    consecutive_failures: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    opened_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
