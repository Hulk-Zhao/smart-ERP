# S4 迁移需求（交 S0 合并，S4 不修改 `db/migrations/`）

目标版本：`0002_s4_audit_iam.py`（`down_revision = "0001_s0_baseline"`）。
S4 只提供模型（`apps/audit/models.py`）与本文件，迁移链的编排、命名与合并窗口归 S0。

## 1. 表清单

| 表 | 用途 | 依据 |
|---|---|---|
| `erp.audit_image` | 字段级 diff + 前像/后像指针 + 哈希链 | FR-1301 / FR-1307 |
| `erp.audit_anchor` | 每日链锚点（链尾 hash + 记录数） | FR-1307 |
| `erp.approval_record` | 审批/复核记录，含自审 CHECK 与一人一单唯一约束 | FR-1103 / FR-1403 |
| `erp.config_change` | 角色/范围/阈值/映射/白名单变更审计，带哈希链 | FR-1402 / FR-1405 / FR-1408 |
| `erp.credential_lease` | 用户态令牌/服务账号凭据租约（只存加密引用） | FR-1401 |

## 2. DDL（PG15）

```sql
-- ---------------------------------------------------------------- audit_image
CREATE TABLE erp.audit_image (
    id           uuid PRIMARY KEY,
    chain_seq    bigint GENERATED ALWAYS AS IDENTITY,
    tenant_id    uuid NOT NULL,
    ticket_id    uuid NOT NULL REFERENCES erp.ticket(id),
    step_id      uuid NOT NULL,
    system       text NOT NULL,
    object_type  text NOT NULL,
    object_id    text NOT NULL,
    before_ref   text,
    after_ref    text,
    field_diff   jsonb NOT NULL DEFAULT '[]'::jsonb,
    diff_missing boolean NOT NULL DEFAULT false,
    prev_hash    text NOT NULL,
    hash         text NOT NULL,
    created_at   timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_audit_image_chain_seq UNIQUE (tenant_id, chain_seq),
    CONSTRAINT ck_audit_image_hash_sha256 CHECK (length(hash) = 64),
    CONSTRAINT ck_audit_image_prev_hash_sha256
        CHECK (length(prev_hash) = 0 OR length(prev_hash) = 64)
);
CREATE INDEX audit_image_tenant_ticket_created_idx
    ON erp.audit_image (tenant_id, ticket_id, created_at);
CREATE INDEX audit_image_tenant_step_idx ON erp.audit_image (tenant_id, step_id);
CREATE INDEX audit_image_tenant_created_idx ON erp.audit_image (tenant_id, created_at);

-- --------------------------------------------------------------- audit_anchor
CREATE TABLE erp.audit_anchor (
    tenant_id    uuid NOT NULL,
    anchor_date  date NOT NULL,
    last_hash    text NOT NULL,
    record_count integer NOT NULL CHECK (record_count >= 0),
    created_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, anchor_date)
);

-- ---------------------------------------------------------- approval_record
CREATE TABLE erp.approval_record (
    id              uuid PRIMARY KEY,
    tenant_id       uuid NOT NULL,
    ticket_id       uuid NOT NULL REFERENCES erp.ticket(id),
    step_id         uuid,
    role            text NOT NULL CHECK (role IN ('reviewer', 'approver', 'cfo')),
    approver_id     uuid NOT NULL,
    initiator_id    uuid NOT NULL,
    approver_roles  text[] NOT NULL DEFAULT '{}',
    decision        text NOT NULL CHECK (decision IN ('approved', 'rejected')),
    comment         text NOT NULL CHECK (length(btrim(comment)) > 0),
    exception_reason text,
    decided_at      timestamptz NOT NULL DEFAULT now(),
    -- FR-1403：自审自批 DB 级兜底
    CONSTRAINT ck_approval_record_sod_self CHECK (approver_id <> initiator_id),
    -- 同一人不得在同一工单担任两个审批角色（复核人 ≠ 审批人）
    CONSTRAINT uq_approval_record_one_per_person UNIQUE (ticket_id, approver_id),
    -- 平台管理员不得参与业务复核
    CONSTRAINT ck_approval_record_no_platform_admin
        CHECK (NOT ('platform_admin' = ANY (approver_roles)))
);
CREATE INDEX approval_record_ticket_idx ON erp.approval_record (tenant_id, ticket_id);

-- -------------------------------------------------------------- config_change
CREATE TABLE erp.config_change (
    id          uuid PRIMARY KEY,
    tenant_id   uuid NOT NULL,
    kind        text NOT NULL,
    target      text NOT NULL,
    before      jsonb NOT NULL,
    after       jsonb NOT NULL,
    actor_id    uuid NOT NULL,
    approver_id uuid,
    ticket_id   uuid,
    emergency   boolean NOT NULL DEFAULT false,
    prev_hash   text NOT NULL,
    hash        text NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT ck_config_change_dual CHECK (approver_id IS NULL OR approver_id <> actor_id)
);
CREATE INDEX config_change_tenant_created_idx ON erp.config_change (tenant_id, created_at);

-- ----------------------------------------------------------- credential_lease
CREATE TABLE erp.credential_lease (
    id                   uuid PRIMARY KEY,
    tenant_id            uuid NOT NULL,
    ticket_id            uuid NOT NULL,
    system               text NOT NULL,
    mode                 text NOT NULL CHECK (mode IN ('delegated', 'service_account')),
    subject_id           uuid NOT NULL,
    scope                jsonb NOT NULL,
    narrowed_fields      text[] NOT NULL DEFAULT '{}',
    requires_dual_review boolean NOT NULL DEFAULT false,
    credential_ref       text NOT NULL,       -- KMS/Vault 加密引用，绝不明文凭据
    degradation_reason   text,
    issued_at            timestamptz NOT NULL DEFAULT now(),
    expires_at           timestamptz NOT NULL,
    CONSTRAINT ck_credential_lease_ttl CHECK (expires_at > issued_at),
    -- B 级（服务账号）必须收窄字段并强制双人复核
    CONSTRAINT ck_credential_lease_b_mode
        CHECK (mode = 'delegated'
               OR (requires_dual_review AND cardinality(narrowed_fields) > 0))
);
CREATE INDEX credential_lease_ticket_idx ON erp.credential_lease (tenant_id, ticket_id);
```

## 3. 只追加与哈希链触发器

```sql
-- audit_image / audit_anchor / approval_record / config_change：拒绝 UPDATE/DELETE
CREATE OR REPLACE FUNCTION erp.fn_append_only() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP <> 'INSERT' THEN
        RAISE EXCEPTION '% 只允许追加，拒绝 %', TG_TABLE_NAME, TG_OP;
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER audit_image_append_only
    BEFORE INSERT OR UPDATE OR DELETE ON erp.audit_image
    FOR EACH ROW EXECUTE FUNCTION erp.fn_append_only();
CREATE TRIGGER audit_anchor_append_only
    BEFORE INSERT OR UPDATE OR DELETE ON erp.audit_anchor
    FOR EACH ROW EXECUTE FUNCTION erp.fn_append_only();
CREATE TRIGGER approval_record_append_only
    BEFORE INSERT OR UPDATE OR DELETE ON erp.approval_record
    FOR EACH ROW EXECUTE FUNCTION erp.fn_append_only();
CREATE TRIGGER config_change_append_only
    BEFORE INSERT OR UPDATE OR DELETE ON erp.config_change
    FOR EACH ROW EXECUTE FUNCTION erp.fn_append_only();

-- 链连续性：新记录的 prev_hash 必须等于同租户链尾 hash（并发写需串行化）
CREATE OR REPLACE FUNCTION erp.fn_audit_image_chain_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    tail text;
BEGIN
    SELECT hash INTO tail
      FROM erp.audit_image
     WHERE tenant_id = NEW.tenant_id
     ORDER BY chain_seq DESC
     LIMIT 1;
    IF NEW.prev_hash IS DISTINCT FROM COALESCE(tail, '') THEN
        RAISE EXCEPTION 'audit_image 链不连续: prev_hash=% 链尾=%',
            NEW.prev_hash, COALESCE(tail, '');
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER audit_image_chain_guard
    BEFORE INSERT ON erp.audit_image
    FOR EACH ROW EXECUTE FUNCTION erp.fn_audit_image_chain_guard();
```

> 并发：已由 `0006_audit_image_prev_uniq` 的 `UNIQUE (tenant_id, prev_hash)` 兜底
> （库层保证，不依赖调用方加锁）：两个并发事务读到同一链尾时，后提交者被唯一约束
> 拒绝，`AuditLedger.append_image` 重读链尾重试一次后接在链上。
> 触发器 `fn_audit_image_chain_guard` 继续负责「prev_hash 必须等于当前链尾」。

## 4. 账号授权（NFR-C-01）

```sql
-- 0002：四张审计表
REVOKE UPDATE, DELETE, TRUNCATE ON erp.audit_image     FROM erp_app;
REVOKE UPDATE, DELETE, TRUNCATE ON erp.audit_anchor    FROM erp_app;
REVOKE UPDATE, DELETE, TRUNCATE ON erp.approval_record FROM erp_app;
REVOKE UPDATE, DELETE, TRUNCATE ON erp.config_change   FROM erp_app;
GRANT INSERT, SELECT ON erp.audit_image, erp.audit_anchor,
      erp.approval_record, erp.config_change TO erp_app;

-- 0006：事件链与租约表
REVOKE UPDATE, DELETE, TRUNCATE ON erp.ticket_event FROM erp_app;
GRANT INSERT, SELECT ON erp.ticket_event TO erp_app;
REVOKE UPDATE, DELETE ON erp.credential_lease FROM erp_app;
GRANT INSERT, SELECT ON erp.credential_lease TO erp_app;

-- 0006：步骤表只保留列级 UPDATE（可变执行投影）
REVOKE UPDATE, DELETE, TRUNCATE ON erp.ticket_step FROM erp_app;
GRANT SELECT, INSERT ON erp.ticket_step TO erp_app;
GRANT UPDATE (state, response_snapshot, error_code, latency_ms,
              before_ref, after_ref) ON erp.ticket_step TO erp_app;
```

> 守卫：`tests/security/test_privilege_guard.py` 扫整个 `erp` schema，证据类表一旦
> 有写权限就红；非证据表要写权限必须逐表登记理由。新增审计表漏 REVOKE 会立刻失败，
> 不再依赖「记得」。

### 4.1 `credential_lease` 为什么收回 UPDATE

当前租约的失效是**读时判定**：`CredentialLease.expires_at` 在取用凭据时与 `now`
比较（`apps/security/identity.py`），`CredentialLeaseRow` 除定义外没有任何调用方，
库里不存在 UPDATE 路径。整表 UPDATE 只意味着「应用角色能改 scope / 有效期」。

**若将来需要吊销、续约或抢占，不要给这张表开 UPDATE**：应改为 append-only 的
lease event 表（每次签发 / 续约 / 吊销各追加一行，读时取最后一行并校验
`expires_at`）。理由与 `ticket_step` 相同——给可变表开写权限，等于把「谁改过租约」
这件事从证据里抹掉。

## 5. 代码侧已实现、待迁移落地后联调

- `apps/audit/models.py`：`AuditBase` 独立 metadata，不并入 S0 的 `Base`，避免 autogenerate 误带。
- `apps/audit/store.py::SqlAuditStore`：只发 INSERT/SELECT，按 `chain_seq` 排序。
- `apps/security/approvals.py`：代码层先拒绝自审、同人多角色、平台管理员复核；DB CHECK 为第二道。
- `apps/security/identity.py`：`credential_ref` 仅为不透明引用，明文凭据不落库、不进日志。

## 6. 尚未覆盖（需 S0/S8 决策）

1. `audit_image` 与 `ticket` 的 FK 在历史数据回填/冷归档时可能冲突，归档表需单独迁移。
2. 外部时间戳（RFC 3161）不在 MVP；`audit_anchor` 先满足「可校验任意时点」。
3. `config_change` 的哈希链复用 `contracts/audit.py` 的同一口径，需在迁移后补一条链校验任务（scheduler 归 S3/S7）。
