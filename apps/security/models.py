"""权限相关表模型：仅作为迁移评审与 DB 约束的单一定义源（S4 不建迁移）。

CHECK/UNIQUE 与 ``apps/audit/MIGRATION_NEEDS.md`` 的 DDL 一一对应；代码层
（``approvals.py`` / ``identity.py``）先拒绝，数据库约束只做兜底。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Identity,
    Index,
    MetaData,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from apps.platform.db.base import NAMING_CONVENTION, SCHEMA


class SecurityBase(DeclarativeBase):
    metadata = MetaData(schema=SCHEMA, naming_convention=NAMING_CONVENTION)


class ApprovalRecordRow(SecurityBase):
    __tablename__ = "approval_record"
    __table_args__ = (
        CheckConstraint("approver_id <> initiator_id", name="ck_approval_record_sod_self"),
        CheckConstraint(
            "role IN ('reviewer', 'approver', 'cfo')", name="ck_approval_record_role"
        ),
        CheckConstraint(
            "decision IN ('approved', 'rejected')", name="ck_approval_record_decision"
        ),
        CheckConstraint(
            "length(btrim(comment)) > 0", name="ck_approval_record_comment"
        ),
        CheckConstraint(
            "NOT ('platform_admin' = ANY (approver_roles))",
            name="ck_approval_record_no_platform_admin",
        ),
        UniqueConstraint("ticket_id", "approver_id", name="uq_approval_record_one_per_person"),
        Index("approval_record_ticket_idx", "tenant_id", "ticket_id"),
        {"schema": SCHEMA},
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    ticket_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    step_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    role: Mapped[str] = mapped_column(Text, nullable=False)
    approver_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    initiator_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    approver_roles: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default=text("'{}'::text[]")
    )
    decision: Mapped[str] = mapped_column(Text, nullable=False)
    comment: Mapped[str] = mapped_column(Text, nullable=False)
    exception_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    decided_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class ConfigChangeRow(SecurityBase):
    __tablename__ = "config_change"
    __table_args__ = (
        CheckConstraint(
            "approver_id IS NULL OR approver_id <> actor_id",
            name="ck_config_change_dual",
        ),
        CheckConstraint("length(hash) = 64", name="ck_config_change_hash_sha256"),
        CheckConstraint(
            "length(prev_hash) = 0 OR length(prev_hash) = 64",
            name="ck_config_change_prev_hash_sha256",
        ),
        # 同租户链序号唯一：verify 依赖它稳定排序（同一事务内 now() 可能相同）
        UniqueConstraint("tenant_id", "chain_seq", name="uq_config_change_chain_seq"),
        # 同租户同父只允许一个后继：并发追加退化为写入失败，而不是静默分叉
        UniqueConstraint("tenant_id", "prev_hash", name="uq_config_change_parent"),
        Index("config_change_tenant_created_idx", "tenant_id", "created_at"),
        {"schema": SCHEMA},
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    chain_seq: Mapped[int] = mapped_column(
        BigInteger, Identity(always=True), nullable=False
    )
    tenant_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    target: Mapped[str] = mapped_column(Text, nullable=False)
    before: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    after: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    actor_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    approver_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    ticket_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    emergency: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    prev_hash: Mapped[str] = mapped_column(Text, nullable=False)
    hash: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class CredentialLeaseRow(SecurityBase):
    __tablename__ = "credential_lease"
    __table_args__ = (
        CheckConstraint(
            "mode IN ('delegated', 'service_account')", name="ck_credential_lease_mode"
        ),
        CheckConstraint("expires_at > issued_at", name="ck_credential_lease_ttl"),
        CheckConstraint(
            "mode = 'delegated' "
            "OR (requires_dual_review AND cardinality(narrowed_fields) > 0)",
            name="ck_credential_lease_b_mode",
        ),
        Index("credential_lease_ticket_idx", "tenant_id", "ticket_id"),
        {"schema": SCHEMA},
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    ticket_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    system: Mapped[str] = mapped_column(Text, nullable=False)
    mode: Mapped[str] = mapped_column(Text, nullable=False)
    subject_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    scope: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    narrowed_fields: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default=text("'{}'::text[]")
    )
    requires_dual_review: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    credential_ref: Mapped[str] = mapped_column(Text, nullable=False)
    degradation_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    issued_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
