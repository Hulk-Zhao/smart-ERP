"""API 流新增表的 SQLAlchemy 声明（与 ``0008_metering_price_book`` 一一对应）。

只用于模型注册表/迁移一致性校验与 SQL 适配器的列引用；业务读写由
``apps.api.metering`` 的原生 SQL 完成。没有这张声明，``tests/platform/test_migrations.py``
的双向差集会把迁移建出的表当成「拼错表名」。
"""

from __future__ import annotations

import sqlalchemy as sa

from apps.platform.db.base import Base

metering_price_book = sa.Table(
    "metering_price_book",
    Base.metadata,
    sa.Column("id", sa.Uuid, primary_key=True),
    sa.Column("tenant_id", sa.Uuid, nullable=False),
    sa.Column("currency", sa.CHAR(3), nullable=False, server_default=sa.text("'CNY'")),
    sa.Column(
        "unit_label",
        sa.Text,
        nullable=False,
        server_default=sa.text("'供应商对账单行'"),
    ),
    sa.Column("unit_price", sa.Numeric(18, 4), nullable=False),
    sa.Column("effective_from", sa.DateTime(timezone=True), nullable=False),
    sa.Column("contract_ref", sa.Text, nullable=False),
    sa.Column("approved_by_user_id", sa.Uuid, nullable=False),
    sa.Column("approved_by_display", sa.Text, nullable=False),
    sa.Column("approved_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("supersedes_id", sa.Uuid),
    sa.Column(
        "created_at",
        sa.DateTime(timezone=True),
        nullable=False,
        server_default=sa.text("now()"),
    ),
    sa.UniqueConstraint("id", "tenant_id", name="uq_price_book_id_tenant"),
    sa.ForeignKeyConstraint(
        ["supersedes_id", "tenant_id"],
        ["erp.metering_price_book.id", "erp.metering_price_book.tenant_id"],
        name="fk_price_book_supersedes",
    ),
)

__all__ = ["metering_price_book"]
