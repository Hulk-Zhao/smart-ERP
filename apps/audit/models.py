"""S4 审计表模型（C3 的 S4 半边）。

**不并入** ``apps.platform.db.base.Base``：迁移链只有 S0 有合并权，S4 只提交
``MIGRATION_NEEDS.md``。独立 ``AuditBase`` 可避免 S0 的 autogenerate 意外把
未评审的表带进基线。

表结构以 ``docs/product/tech/01-技术架构.md`` §3.4.1 为准；``chain_seq`` 是
S4 追加的存储序号（同一事务内 ``now()`` 相同，仅靠 created_at 无法稳定排序）。
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Identity,
    Index,
    Integer,
    MetaData,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from apps.platform.db.base import NAMING_CONVENTION, SCHEMA


class AuditBase(DeclarativeBase):
    metadata = MetaData(schema=SCHEMA, naming_convention=NAMING_CONVENTION)


class AuditImageRow(AuditBase):
    """字段级 diff + 前像/后像指针 + 哈希链。只允许 INSERT/SELECT。"""

    __tablename__ = "audit_image"
    __table_args__ = (
        UniqueConstraint("tenant_id", "chain_seq", name="uq_audit_image_chain_seq"),
        CheckConstraint("length(hash) = 64", name="ck_audit_image_hash_sha256"),
        CheckConstraint(
            "length(prev_hash) = 0 OR length(prev_hash) = 64",
            name="ck_audit_image_prev_hash_sha256",
        ),
        Index("audit_image_tenant_ticket_created_idx", "tenant_id", "ticket_id", "created_at"),
        Index("audit_image_tenant_step_idx", "tenant_id", "step_id"),
        Index("audit_image_tenant_created_idx", "tenant_id", "created_at"),
        {"schema": SCHEMA},
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    # 追加序号：哈希链的物理顺序锚，避免同事务 created_at 相同导致排序漂移
    chain_seq: Mapped[int] = mapped_column(
        BigInteger, Identity(always=True), nullable=False
    )
    tenant_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    ticket_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    step_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    system: Mapped[str] = mapped_column(Text, nullable=False)
    object_type: Mapped[str] = mapped_column(Text, nullable=False)
    object_id: Mapped[str] = mapped_column(Text, nullable=False)
    before_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    after_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    field_diff: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    diff_missing: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    prev_hash: Mapped[str] = mapped_column(Text, nullable=False)
    hash: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class AuditAnchorRow(AuditBase):
    """每日链锚点（FR-1307）：只 INSERT，用于发现截断与尾部篡改。"""

    __tablename__ = "audit_anchor"
    __table_args__ = (
        CheckConstraint("record_count >= 0", name="ck_audit_anchor_count_non_negative"),
        Index("audit_anchor_tenant_date_idx", "tenant_id", "anchor_date"),
        {"schema": SCHEMA},
    )

    tenant_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    anchor_date: Mapped[date] = mapped_column(Date, primary_key=True)
    last_hash: Mapped[str] = mapped_column(Text, nullable=False)
    record_count: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
