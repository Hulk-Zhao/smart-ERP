"""对账批次落库的 SQLAlchemy 表声明（与 0003_recon_persistence 一一对应）。

只用于模型注册表/迁移一致性校验与 SQL 适配器的列引用；业务读写走
``apps.api.recon_store``。表落 ``erp`` schema（``Base.metadata`` 已带 schema）。
"""

from __future__ import annotations

import sqlalchemy as sa

from apps.platform.db.base import Base

recon_batch = sa.Table(
    "recon_batch",
    Base.metadata,
    sa.Column("id", sa.Uuid, primary_key=True),
    sa.Column("tenant_id", sa.Uuid, nullable=False),
    sa.Column("org_id", sa.Text, nullable=False),
    sa.Column("period", sa.Text, nullable=False),
    sa.Column("status", sa.Text, nullable=False),
    sa.Column("conclusive", sa.Boolean, nullable=False),
    sa.Column("status_reason", sa.Text, nullable=False),
    sa.Column("suppliers", sa.ARRAY(sa.Text), nullable=False),
    sa.Column("system_ids", sa.ARRAY(sa.Text), nullable=False),
    sa.Column("metrics", sa.JSON),
    sa.Column("input_fingerprint", sa.Text, nullable=False),
    sa.Column("rerun_of", sa.Uuid),
    sa.Column("rerun_reason", sa.Text),
    sa.Column("created_by", sa.Uuid, nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.UniqueConstraint("id", "tenant_id", name="recon_batch_id_tenant_uniq"),
    sa.ForeignKeyConstraint(
        ["rerun_of", "tenant_id"],
        ["erp.recon_batch.id", "erp.recon_batch.tenant_id"],
        name="recon_batch_rerun_fk",
    ),
)

recon_line = sa.Table(
    "recon_line",
    Base.metadata,
    sa.Column("id", sa.Uuid, primary_key=True),
    sa.Column("batch_id", sa.Uuid, nullable=False),
    sa.Column("tenant_id", sa.Uuid, nullable=False),
    sa.Column("side", sa.Text, nullable=False),
    sa.Column("line_no", sa.Integer, nullable=False),
    sa.Column("supplier_code", sa.Text),
    sa.Column("supplier_ref", sa.Text),
    sa.Column("our_ref", sa.Text),
    sa.Column("status", sa.Text, nullable=False),
    sa.Column("match_type", sa.Text),
    sa.Column("confidence", sa.Numeric(3, 2)),
    sa.Column("needs_manual", sa.Boolean, nullable=False),
    sa.Column("diffs", sa.JSON, nullable=False),
    sa.Column("evidence", sa.JSON, nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.UniqueConstraint("id", "tenant_id", name="recon_line_id_tenant_uniq"),
    sa.UniqueConstraint(
        "id", "batch_id", "tenant_id", name="recon_line_id_batch_tenant_uniq"
    ),
    sa.UniqueConstraint("batch_id", "side", "line_no", name="recon_line_no_uniq"),
    sa.ForeignKeyConstraint(
        ["batch_id", "tenant_id"],
        ["erp.recon_batch.id", "erp.recon_batch.tenant_id"],
        name="recon_line_batch_fk",
    ),
)

recon_line_event = sa.Table(
    "recon_line_event",
    Base.metadata,
    sa.Column("seq", sa.BigInteger, sa.Identity(always=True), primary_key=True),
    sa.Column("id", sa.Uuid, nullable=False),
    sa.Column("tenant_id", sa.Uuid, nullable=False),
    sa.Column("batch_id", sa.Uuid, nullable=False),
    sa.Column("line_id", sa.Uuid, nullable=False),
    sa.Column("event", sa.Text, nullable=False),
    sa.Column("actor", sa.Uuid, nullable=False),
    sa.Column("reason", sa.Text),
    sa.Column("evidence", sa.JSON, nullable=False),
    sa.Column("idem_key", sa.Text),
    sa.Column("dedupe_key", sa.Text, nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.UniqueConstraint("id", name="recon_line_event_id_uniq"),
    sa.ForeignKeyConstraint(
        ["line_id", "batch_id", "tenant_id"],
        ["erp.recon_line.id", "erp.recon_line.batch_id", "erp.recon_line.tenant_id"],
        name="recon_line_event_line_fk",
    ),
    sa.ForeignKeyConstraint(
        ["batch_id", "tenant_id"],
        ["erp.recon_batch.id", "erp.recon_batch.tenant_id"],
        name="recon_line_event_batch_fk",
    ),
)

recon_line_link = sa.Table(
    "recon_line_link",
    Base.metadata,
    sa.Column("id", sa.Uuid, primary_key=True),
    sa.Column("tenant_id", sa.Uuid, nullable=False),
    sa.Column("line_id", sa.Uuid, nullable=False),
    sa.Column("ticket_id", sa.Uuid),
    sa.Column("audit_image_id", sa.Uuid),
    sa.Column("link_type", sa.Text, nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(
        ["line_id", "tenant_id"],
        ["erp.recon_line.id", "erp.recon_line.tenant_id"],
        name="recon_line_link_line_fk",
    ),
    sa.ForeignKeyConstraint(
        ["ticket_id", "tenant_id"],
        ["erp.ticket.id", "erp.ticket.tenant_id"],
        name="recon_line_link_ticket_fk",
    ),
    sa.ForeignKeyConstraint(
        ["audit_image_id", "tenant_id"],
        ["erp.audit_image.id", "erp.audit_image.tenant_id"],
        name="recon_line_link_image_fk",
    ),
)

__all__ = ["recon_batch", "recon_line", "recon_line_event", "recon_line_link"]
