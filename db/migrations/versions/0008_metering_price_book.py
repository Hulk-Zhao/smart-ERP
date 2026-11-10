"""计量单价：纯追加价格表 + 批次计价快照

Revision ID: 0008_metering_price_book
Revises: 0007_recon_batch_projection
Create Date: 2026-11-11

口径（经评审，逐条对应评审意见）：
- **纯追加，不存 effective_to**：某时点的价 = ``effective_from <= t`` 中最大的那一行，
  有效期上界由后继行推导。于是不可能重叠（不再需要 EXCLUDE / btree_gist），
  也不可能改价（改价 = 新行 + ``supersedes_id``），``REVOKE UPDATE, DELETE`` 才成立。
  若将来确需限时价，另开迁移：列级 ``GRANT UPDATE(effective_to)`` + 触发器断言区间。
- **租户复合外键**：``supersedes_id`` 与 ``recon_batch.price_book_id`` 都按
  ``(id, tenant_id)`` 关联，禁止 A 租户引用 B 租户的价目行。
- **币种**：``CHECK (currency ~ '^[A-Z]{3}$')``，挡住 ``'CN'`` / ``'cny'`` 这类手填低级错；
  正则只能保证「三位大写字母」，挡不住 ``'ZZZ'`` 这种格式合法但非 ISO 4217 的代码
  （要挡需内置币种清单，本迁移不做）。
- **审批人**：``approved_by_user_id`` + ``approved_by_display``（批准时的姓名快照）。
  仓库内没有身份表（0003 的 ``created_by`` 同为裸 uuid），因此不做跨表外键；
  若平台层后续提供身份表，再补 ``(approved_by_user_id, tenant_id)`` 复合外键。
  插价目行必须在**同一事务**内写一条审计事件（实现期用例保证）。
- **owner / 授权**：本迁移必须由 ``erp_migrator`` 执行（表 owner = erp_migrator）；
  ``ALTER DEFAULT PRIVILEGES`` 会给 ``erp_app`` 自动授 UPDATE/DELETE，
  所以建表后必须显式 ``REVOKE``，再只给 SELECT/INSERT。负向用例见
  ``tests/platform/test_price_book_migration.py``（UPDATE / DELETE / TRUNCATE 均须
  ``42501 insufficient_privilege``——**不是** ``23514 check_violation``，
  后者是 CHECK 约束拒绝，排查方向完全不同；``pg_tables.tableowner = 'erp_migrator'``）。

取价口径（写在实现里，不只留在评审记录）：
- 取价 SQL 见 ``apps/api/metering.py::PRICE_LOOKUP_SQL``：在
  ``effective_from <= t`` 中 ``ORDER BY effective_from DESC LIMIT 1``；
- **不按 ``supersedes_id IS NULL`` 过滤**。``supersedes_id`` 是审计追溯链，
  不是取价条件；「作废」的生效时刻由**新行的 effective_from** 定义，
  不由旧行状态定义。按它过滤会让 ``t`` 早于新价生效日的区间直接查不到价，
  历史账无法复算；
- 纠错 = 插新行（``supersedes_id`` 指向旧行）；旧批次金额由 ``recon_batch``
  的四字段快照保证不变，不得 UPDATE / DELETE 价格表（``erp_app`` 无此权限，42501）；
- 未来日期的错价行无法被更早的 ``effective_from`` 遮蔽：纠错行的
  ``effective_from`` 必须 ≥ 错价行，取 ``错价行.effective_from + 1µs``；
- 币种必须与批次一致；不符即「无有效价」，fail closed，绝不改用价格表里的其他币种。

取整口径（两级，只在账期汇总层取整一次）：
- 批次级 ``statement`` 金额保留 4 位小数、**不取整**，前端标注「未取整，最终以账期账单为准」；
- 账期应付 = 各批次 4 位金额相加后**一次** ``ROUND_HALF_UP`` 到 2 位；
- 法律口径 = 账期汇总层的数；批次级数字只用于展示与核对；
- 算例：``0.3333 × 1 = 0.3333``，20 个批次 ``= 6.6666 → 6.67``；
  若逐批次先取整 ``0.33 × 20 = 6.60``，与客户按总额付的 6.67 差 **7 分**
  （`0.0333 × 20 = 0.6666`，不是 1 分）；「差 1 分」的最小算例是
  ``[0.0025, 0.0025]``：一次取整 ``0.0050 → 0.01``，逐批次 ``0.00 + 0.00 = 0.00``；
- 精确性前提：本表单价是 ``numeric(18,4)``、行数是整数，``单价 × 行数`` 在 4 位内
  **可精确表示**，批次级无需任何舍入；``env_fallback`` 的单价必须在读取时一次
  ``quantize(0.0001, ROUND_HALF_UP)``，否则「不取整」这句话就不成立；
- 实现约束：Python ``Decimal`` 必须显式 ``ROUND_HALF_UP``（默认 HALF_EVEN 会在 ``x.xx5`` 少 1 分）。

空价目表（上线当天必然遇到）：
- ``ERP_METERING_REQUIRE_CONTRACT_PRICE=true`` 且表中无有效价 → 出账必须 **503**，
  且**不复用** ``E-DATA-MISSING``（那会让人分不清「没配价」与「输入缺数据」）：
  - 无任何生效价 → ``E-PRICE-MISSING``；
  - 有价但币种不符 → ``E-PRICE-CURRENCY``（fail closed，禁止折算或改用其他币种）；
  - 两者都带 ``context.currency / context.tenant_id / context.at`` 供排障，
    但**不回显**价格表里其他币种的单价（那是别家的合同价）；
- 不得回退 ``ERP_METERING_UNIT_PRICE``（``require=false`` 时才允许显式回落，
  且快照的 ``unit_price_source`` 写 ``env_fallback``，不是静默）；
- ``unit_price_source`` 是**封闭枚举** ``contract_price`` / ``env_fallback``，
  未知值在 Python 读侧直接报错，DB 侧由 ``ck_recon_batch_price_snapshot`` 拒绝；
- 落地点：``apps/api/price_book.py``（取价端口）与 ``apps/api/service.py``
  （批次创建/账单读取共用同一取价口径）；用例见
  ``tests/api/test_price_snapshot.py`` 与
  ``tests/api/test_recon_store_sql.py::test_new_batch_takes_new_price_while_old_batch_keeps_snapshot``；
- 上线首条价目（占位值必须替换，否则示例价会真的进账单）::

    INSERT INTO erp.metering_price_book
      (id, tenant_id, currency, unit_label, unit_price, effective_from,
       contract_ref, approved_by_user_id, approved_by_display, approved_at)
    VALUES
      (gen_random_uuid(), '<tenant_id>'::uuid, 'CNY', '供应商对账单行', 0.3000,
       '2026-11-01T00:00:00+08:00', 'HT-2026-001',
       '<approved_by_user_id>'::uuid, '张三', now());

**回退警告**：``downgrade`` 会 ``DROP COLUMN`` 掉 ``recon_batch`` 上的
``unit_price / currency / unit_price_source / price_book_id``，**已快照的计价依据
会永久丢失**（历史批次将无法自证「按哪版价算的」）。迁移可回退不等于可随便回退：
执行前必须先备份 ``erp.recon_batch`` 相关列，并在变更单里写明回退原因。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0008_metering_price_book"
down_revision = "0007_recon_batch_projection"
branch_labels = None
depends_on = None

SCHEMA = "erp"


def _execute(*statements: str) -> None:
    for statement in statements:
        op.execute(sa.text(statement))


def upgrade() -> None:
    _execute(
        # 1) 价格表：纯追加；上界由后继行推导，不存 effective_to
        """
        CREATE TABLE erp.metering_price_book (
          id                   uuid PRIMARY KEY,
          tenant_id            uuid NOT NULL,
          currency             char(3) NOT NULL DEFAULT 'CNY',
          unit_label           text NOT NULL DEFAULT '供应商对账单行',
          unit_price           numeric(18,4) NOT NULL CHECK (unit_price > 0),
          effective_from       timestamptz NOT NULL,
          contract_ref         text NOT NULL CHECK (length(btrim(contract_ref)) > 0),
          approved_by_user_id  uuid NOT NULL,
          approved_by_display  text NOT NULL CHECK (length(btrim(approved_by_display)) > 0),
          approved_at          timestamptz NOT NULL,
          supersedes_id        uuid,
          created_at           timestamptz NOT NULL DEFAULT now(),
          CONSTRAINT uq_price_book_id_tenant UNIQUE (id, tenant_id),
          CONSTRAINT fk_price_book_supersedes FOREIGN KEY (supersedes_id, tenant_id)
            REFERENCES erp.metering_price_book (id, tenant_id),
          CONSTRAINT ck_price_book_no_self_supersede
            CHECK (supersedes_id IS NULL OR supersedes_id <> id),
          CONSTRAINT ck_price_book_currency_iso CHECK (currency ~ '^[A-Z]{3}$')
        )
        """,
        # 唯一索引列序即查询列序；ORDER BY effective_from DESC 走反向扫描，
        # 不再额外建 DESC 索引（重复索引只增加写放大）。
        """
        CREATE UNIQUE INDEX uq_price_book_tenant_currency_from
          ON erp.metering_price_book (tenant_id, currency, effective_from)
        """,
        # 2) 批次上的计价快照：金额单位、来源与价目行必须自洽
        """
        ALTER TABLE erp.recon_batch ADD COLUMN unit_price numeric(18,4)
        """,
        """
        ALTER TABLE erp.recon_batch ADD COLUMN currency char(3)
        """,
        """
        ALTER TABLE erp.recon_batch ADD COLUMN unit_price_source text
        """,
        """
        ALTER TABLE erp.recon_batch ADD COLUMN price_book_id uuid
        """,
        # 币种格式在快照表上也要挡：手写 SQL 补录历史批次时同样会写 'CN' / 'cny'
        """
        ALTER TABLE erp.recon_batch
          ADD CONSTRAINT ck_recon_batch_currency_iso
          CHECK (currency IS NULL OR currency ~ '^[A-Z]{3}$')
        """,
        """
        ALTER TABLE erp.recon_batch
          ADD CONSTRAINT fk_recon_batch_price_book
          FOREIGN KEY (price_book_id, tenant_id)
          REFERENCES erp.metering_price_book (id, tenant_id)
        """,
        """
        ALTER TABLE erp.recon_batch
          ADD CONSTRAINT ck_recon_batch_price_snapshot CHECK (
            unit_price_source IS NULL
            OR (unit_price_source = 'contract_price'
                AND unit_price IS NOT NULL AND price_book_id IS NOT NULL
                AND currency IS NOT NULL)
            OR (unit_price_source = 'env_fallback'
                AND unit_price IS NOT NULL AND price_book_id IS NULL
                AND currency IS NOT NULL)
          )
        """,
        # 3) 授权：REVOKE 必须在建表后显式执行（owner 是 erp_migrator 才有效）
        """
        DO $$
        BEGIN
          IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'erp_app') THEN
            GRANT USAGE ON SCHEMA erp TO erp_app;
            REVOKE ALL ON erp.metering_price_book FROM erp_app;
            GRANT SELECT, INSERT ON erp.metering_price_book TO erp_app;
          END IF;
        END $$
        """,
    )


def downgrade() -> None:
    # 警告：以下 DROP COLUMN 会丢弃已快照的计价依据，执行前必须先备份
    # erp.recon_batch 的 unit_price / currency / unit_price_source / price_book_id。
    _execute(
        """
        ALTER TABLE erp.recon_batch
          DROP CONSTRAINT IF EXISTS ck_recon_batch_price_snapshot
        """,
        """
        ALTER TABLE erp.recon_batch
          DROP CONSTRAINT IF EXISTS fk_recon_batch_price_book
        """,
        """
        ALTER TABLE erp.recon_batch
          DROP CONSTRAINT IF EXISTS ck_recon_batch_currency_iso
        """,
        """
        ALTER TABLE erp.recon_batch DROP COLUMN IF EXISTS price_book_id
        """,
        """
        ALTER TABLE erp.recon_batch DROP COLUMN IF EXISTS unit_price_source
        """,
        """
        ALTER TABLE erp.recon_batch DROP COLUMN IF EXISTS currency
        """,
        """
        ALTER TABLE erp.recon_batch DROP COLUMN IF EXISTS unit_price
        """,
        """
        DROP TABLE IF EXISTS erp.metering_price_book
        """,
    )
