"""对账批次视图投影：运行详情 / 差异列表 / 差异详情随批次落库

Revision ID: 0007_recon_batch_projection
Revises: 0006_audit_image_prev_uniq
Create Date: 2026-11-11

背景：计量表早就只读持久化快照，但 ``GET /v1/recon/runs/{id}``、
``/runs/{id}/diffs``、``/diffs/{id}`` 仍读进程内存，服务一重启就 404——
「账能算、路走不通」，按结果计费因此无法演示，更无法交付。

口径：
- 只 ``ADD COLUMN``：不给既有列加约束、不改类型；既有行保持不动。
- ``NULL`` = 该批次由本迁移前的版本写入、没有投影；**不等于** ``{}``。
  ``{}`` 或缺 ``view_version`` 的 JSON 一律视为不可读，读侧明确报错（503），
  不做 best-effort 解析。
- 投影带 ``view_version``，当前为 1；未知版本明确报错，不猜形状。
- 投影是**批次产物的一部分（真源），不是可丢弃缓存**：差异详情的
  ``related_lines`` / ``draft`` / ``unattributed`` 与计费桶无法只从
  ``recon_line`` 重建；它和批次头一样 append-only（改判只能重跑新批次）。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0007_recon_batch_projection"
down_revision = "0006_audit_image_prev_uniq"
branch_labels = None
depends_on = None

SCHEMA = "erp"


def upgrade() -> None:
    op.add_column(
        "recon_batch",
        sa.Column(
            "result_view", postgresql.JSONB(astext_type=sa.Text()), nullable=True
        ),
        schema=SCHEMA,
    )


def downgrade() -> None:
    op.drop_column("recon_batch", "result_view", schema=SCHEMA)
