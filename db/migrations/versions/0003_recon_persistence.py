"""对账批次落库：批次头 / 行快照 / 行级确认事件 / 行↔工单影像关联

Revision ID: 0003_recon_persistence
Revises: 0002_s1s3s4_merge
Create Date: 2026-11-11

口径（经评审）：
- 全部追加式：只新建表/索引/触发器/授权，不改既有表数据；
- 批次与行快照不可变，改判只能开新批次（append-only 触发器兜底）；
- 结论正确性只能由 ``recon_line_event`` 人工事件表达，引擎不自证正确；
- ``recon_line.status`` 不加 CHECK 枚举：状态是 engine 枚举的投影，库里再来一份
  就是第二真源（``DIFF_TYPE_CODES`` 漏项事故的教训）；
- 事件排序只用 ``seq``（GENERATED ALWAYS AS IDENTITY），不用 (created_at, id)；
- 租户隔离用复合外键：``(id, tenant_id)`` / ``(line_id, batch_id, tenant_id)``；
- 无数据迁移：内存里的历史批次不迁移，上线后旧数据不留。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0003_recon_persistence"
down_revision = "0002_s1s3s4_merge"
branch_labels = None
depends_on = None

SCHEMA = "erp"


def _execute(*statements: str) -> None:
    for statement in statements:
        op.execute(sa.text(statement))


def upgrade() -> None:
    _execute(
        # 前置：为租户复合外键补唯一键（纯追加索引，不改数据）
        """
        CREATE UNIQUE INDEX IF NOT EXISTS ticket_id_tenant_uniq
          ON erp.ticket (id, tenant_id)
        """,
        """
        CREATE UNIQUE INDEX IF NOT EXISTS audit_image_id_tenant_uniq
          ON erp.audit_image (id, tenant_id)
        """,
        # 1) 批次头（不可变快照）
        """
        CREATE TABLE erp.recon_batch (
          id                uuid PRIMARY KEY,
          tenant_id         uuid NOT NULL,
          org_id            text NOT NULL,
          period            text NOT NULL,
          status            text NOT NULL,
          conclusive        boolean NOT NULL,
          status_reason     text NOT NULL,
          suppliers         text[] NOT NULL,
          system_ids        text[] NOT NULL,
          metrics           jsonb,
          input_fingerprint text NOT NULL,
          rerun_of          uuid,
          rerun_reason      text,
          created_by        uuid NOT NULL,
          created_at        timestamptz NOT NULL DEFAULT now(),
          CONSTRAINT recon_batch_id_tenant_uniq UNIQUE (id, tenant_id),
          CONSTRAINT recon_batch_rerun_fk FOREIGN KEY (rerun_of, tenant_id)
            REFERENCES erp.recon_batch (id, tenant_id),
          CONSTRAINT recon_batch_rerun_marker
            CHECK (rerun_of IS NULL OR rerun_reason IS NOT NULL)
        )
        """,
        """
        CREATE INDEX recon_batch_tenant_period_idx
          ON erp.recon_batch (tenant_id, period)
        """,
        # 同租户+组织+期间+输入指纹只允许一个“首次执行”；
        # 显式重跑须带 rerun_of，且同一来源+同一输入只允许一次。
        """
        CREATE UNIQUE INDEX recon_batch_input_uniq ON erp.recon_batch (
          tenant_id, org_id, period, input_fingerprint,
          COALESCE(rerun_of, '00000000-0000-0000-0000-000000000000'::uuid)
        )
        """,
        # 2) 行快照（不可变；line_no 供 CSV/报表指认“第 N 行”）
        """
        CREATE TABLE erp.recon_line (
          id            uuid PRIMARY KEY,
          batch_id      uuid NOT NULL,
          tenant_id     uuid NOT NULL,
          side          text NOT NULL CHECK (side IN ('supplier','our')),
          line_no       integer NOT NULL CHECK (line_no > 0),
          supplier_code text,
          supplier_ref  text,
          our_ref       text,
          status        text NOT NULL,
          match_type    text,
          confidence    numeric(3,2),
          needs_manual  boolean NOT NULL,
          diffs         jsonb NOT NULL DEFAULT '[]'::jsonb,
          evidence      jsonb NOT NULL DEFAULT '[]'::jsonb,
          created_at    timestamptz NOT NULL DEFAULT now(),
          CONSTRAINT recon_line_id_tenant_uniq UNIQUE (id, tenant_id),
          CONSTRAINT recon_line_id_batch_tenant_uniq UNIQUE (id, batch_id, tenant_id),
          CONSTRAINT recon_line_batch_fk FOREIGN KEY (batch_id, tenant_id)
            REFERENCES erp.recon_batch (id, tenant_id),
          CONSTRAINT recon_line_no_uniq UNIQUE (batch_id, side, line_no),
          CONSTRAINT recon_line_confidence_check
            CHECK (confidence IS NULL OR (confidence >= 0 AND confidence <= 1)),
          CONSTRAINT recon_line_ref_check CHECK (
            (side = 'supplier' AND supplier_ref IS NOT NULL)
            OR (side = 'our' AND our_ref IS NOT NULL)
          )
        )
        """,
        """
        CREATE INDEX recon_line_batch_idx
          ON erp.recon_line (batch_id, side, line_no)
        """,
        """
        CREATE INDEX recon_line_ref_idx
          ON erp.recon_line (tenant_id, supplier_ref)
        """,
        # 3) 行级人工确认事件（append-only；seq 单调排序）
        """
        CREATE TABLE erp.recon_line_event (
          seq        bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
          id         uuid NOT NULL,
          tenant_id  uuid NOT NULL,
          batch_id   uuid NOT NULL,
          line_id    uuid NOT NULL,
          event      text NOT NULL CHECK (event IN
            ('confirmed_correct','confirmed_wrong','disputed',
             'customer_reason','manual_modified')),
          actor      uuid NOT NULL,
          reason     text,
          evidence   jsonb NOT NULL DEFAULT '[]'::jsonb,
          idem_key   text,
          dedupe_key text NOT NULL,
          created_at timestamptz NOT NULL DEFAULT now(),
          CONSTRAINT recon_line_event_id_uniq UNIQUE (id),
          CONSTRAINT recon_line_event_line_fk
            FOREIGN KEY (line_id, batch_id, tenant_id)
            REFERENCES erp.recon_line (id, batch_id, tenant_id),
          CONSTRAINT recon_line_event_batch_fk FOREIGN KEY (batch_id, tenant_id)
            REFERENCES erp.recon_batch (id, tenant_id)
        )
        """,
        """
        CREATE INDEX recon_line_event_line_seq_idx
          ON erp.recon_line_event (line_id, seq DESC)
        """,
        """
        CREATE INDEX recon_line_event_batch_seq_idx
          ON erp.recon_line_event (batch_id, seq)
        """,
        # 事件幂等：同 actor+line+event 在时间桶内只落一条（重试不得撑大 seq）
        """
        CREATE UNIQUE INDEX recon_line_event_dedupe_uniq
          ON erp.recon_line_event (tenant_id, dedupe_key)
        """,
        """
        CREATE UNIQUE INDEX recon_line_event_idem_uniq
          ON erp.recon_line_event (tenant_id, idem_key)
          WHERE idem_key IS NOT NULL
        """,
        # 4) 行 ↔ 工单/审计影像（append-only）
        """
        CREATE TABLE erp.recon_line_link (
          id             uuid PRIMARY KEY,
          tenant_id      uuid NOT NULL,
          line_id        uuid NOT NULL,
          ticket_id      uuid,
          audit_image_id uuid,
          link_type      text NOT NULL CHECK (link_type IN
            ('created','confirmed','compensated')),
          created_at     timestamptz NOT NULL DEFAULT now(),
          CONSTRAINT recon_line_link_line_fk FOREIGN KEY (line_id, tenant_id)
            REFERENCES erp.recon_line (id, tenant_id),
          CONSTRAINT recon_line_link_ticket_fk FOREIGN KEY (ticket_id, tenant_id)
            REFERENCES erp.ticket (id, tenant_id),
          CONSTRAINT recon_line_link_image_fk FOREIGN KEY (audit_image_id, tenant_id)
            REFERENCES erp.audit_image (id, tenant_id),
          CONSTRAINT recon_line_link_target_check
            CHECK (ticket_id IS NOT NULL OR audit_image_id IS NOT NULL)
        )
        """,
        """
        CREATE INDEX recon_line_link_line_idx
          ON erp.recon_line_link (line_id, created_at)
        """,
        # 5) append-only 守卫：fn_append_only 仅在 TG_OP <> 'INSERT' 时抛错，INSERT 放行
        """
        CREATE TRIGGER recon_batch_append_only
          BEFORE INSERT OR UPDATE OR DELETE ON erp.recon_batch
          FOR EACH ROW EXECUTE FUNCTION erp.fn_append_only()
        """,
        """
        CREATE TRIGGER recon_line_append_only
          BEFORE INSERT OR UPDATE OR DELETE ON erp.recon_line
          FOR EACH ROW EXECUTE FUNCTION erp.fn_append_only()
        """,
        """
        CREATE TRIGGER recon_line_event_append_only
          BEFORE INSERT OR UPDATE OR DELETE ON erp.recon_line_event
          FOR EACH ROW EXECUTE FUNCTION erp.fn_append_only()
        """,
        """
        CREATE TRIGGER recon_line_link_append_only
          BEFORE INSERT OR UPDATE OR DELETE ON erp.recon_line_link
          FOR EACH ROW EXECUTE FUNCTION erp.fn_append_only()
        """,
        # 6) 权限：只给 SELECT/INSERT；identity 序列必须单独授权（否则首次写入才炸）
        """
        DO $$
        BEGIN
          IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'erp_app') THEN
            GRANT USAGE ON SCHEMA erp TO erp_app;
            REVOKE UPDATE, DELETE, TRUNCATE ON
              erp.recon_batch, erp.recon_line,
              erp.recon_line_event, erp.recon_line_link FROM erp_app;
            GRANT SELECT, INSERT ON
              erp.recon_batch, erp.recon_line,
              erp.recon_line_event, erp.recon_line_link TO erp_app;
            GRANT USAGE, SELECT ON SEQUENCE erp.recon_line_event_seq_seq TO erp_app;
          END IF;
        END $$
        """,
    )


def downgrade() -> None:
    _execute(
        """
        DROP TRIGGER IF EXISTS recon_line_link_append_only ON erp.recon_line_link
        """,
        """
        DROP TRIGGER IF EXISTS recon_line_event_append_only ON erp.recon_line_event
        """,
        """
        DROP TRIGGER IF EXISTS recon_line_append_only ON erp.recon_line
        """,
        """
        DROP TRIGGER IF EXISTS recon_batch_append_only ON erp.recon_batch
        """,
        "DROP TABLE IF EXISTS erp.recon_line_link",
        "DROP TABLE IF EXISTS erp.recon_line_event",
        "DROP TABLE IF EXISTS erp.recon_line",
        "DROP TABLE IF EXISTS erp.recon_batch",
        "DROP INDEX IF EXISTS erp.audit_image_id_tenant_uniq",
        "DROP INDEX IF EXISTS erp.ticket_id_tenant_uniq",
    )
