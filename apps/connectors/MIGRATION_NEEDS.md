# S1 连接器：迁移需求（交集成会话转 S0，S1 不直接改 `db/migrations/`）

当前 S1 的限流 / 熔断 / 幂等键都是**进程内实现**，接口分别是
`apps/connectors/ratelimit.py::RateLimiter`、`apps/connectors/breaker.py::CircuitBreaker`、
`apps/connectors/idempotency.py::IdempotencyStore`，换 PG 实现时调用方不需要改。

要在多进程/多副本下成立（gateway + worker 共用一个连接器配额），需要下面三张表。
PG15 语法；不使用 PG16 的 `MERGE ... RETURNING` / `any_value()`。

## 1. `erp.connector_idempotency`

写入幂等的真源（G-02 第二层键）。**不可丢**：一旦记录丢失，同键请求会被当成新请求再发一次。

| 列 | 类型 | 约束 | 说明 |
|---|---|---|---|
| `idempotency_key` | `text` | PK | 上游传入的幂等键（一次意图一个） |
| `connector` | `text` | NOT NULL | 连接器别名，如 `u8` |
| `action_key` | `text` | NOT NULL | 动作键，如 `u8.po.create_draft` |
| `request_hash` | `text` | NOT NULL | 规范化报文 sha256；同键不同参 → `E-IDEM-CONFLICT` |
| `status` | `text` | NOT NULL, CHECK IN (`in_flight`,`succeeded`,`failed`) | 在途/成功/失败 |
| `response` | `jsonb` | NULL | 成功后存 `{external_id, body, before_ref, after_ref}`，用于 replay |
| `created_at` | `timestamptz` | NOT NULL DEFAULT now() | |
| `updated_at` | `timestamptz` | NOT NULL DEFAULT now() | |
| `expires_at` | `timestamptz` | NULL | 保留期建议 ≥ 30 天（覆盖重试/补偿窗口） |

索引：`(connector, action_key, created_at)`；过期清理走定时任务，不要 `DELETE` 在请求路径上。

实现提示：`begin` 用 `INSERT ... ON CONFLICT (idempotency_key) DO NOTHING RETURNING ...`，
冲突后 `SELECT` 判定 hash 是否一致，避免「先查再插」的竞态。

## 2. `erp.connector_rate_bucket`

跨进程令牌桶 + 日配额（FR-109 / FR-1205）。**可重建**，丢了最多短暂超发。

| 列 | 类型 | 约束 | 说明 |
|---|---|---|---|
| `bucket_key` | `text` | PK | 动作键或 `u8.read` |
| `tokens` | `numeric(12,4)` | NOT NULL | 当前令牌数（含小数） |
| `updated_at` | `timestamptz` | NOT NULL | 上次补充令牌的时间 |
| `quota_day` | `date` | NOT NULL | 配额所属日（UTC） |
| `quota_used` | `integer` | NOT NULL DEFAULT 0 | 当日已用调用数 |
| `quota_limit` | `integer` | NOT NULL | 当日上限（来自 `ActionSpec.rate_limit.daily_quota`） |

实现提示：单条 `UPDATE erp.connector_rate_bucket SET tokens = LEAST(capacity, tokens + ...), ... WHERE bucket_key = $1 AND ... RETURNING tokens`；
拿不到令牌时由调用方 `sleep` 重试，不要让事务长期持有行锁。

## 3. `erp.connector_breaker_state`

熔断状态（FR-1107 / FR-1206）。**可重建**，重启后保守从 `open` 或 `closed` 由 SWEEPER 决定。

| 列 | 类型 | 约束 | 说明 |
|---|---|---|---|
| `connector` | `text` | PK | 连接器别名 |
| `state` | `text` | NOT NULL CHECK IN (`closed`,`open`,`half_open`) | |
| `consecutive_failures` | `integer` | NOT NULL DEFAULT 0 | |
| `opened_at` | `timestamptz` | NULL | 打开时间，用于半开计时 |
| `updated_at` | `timestamptz` | NOT NULL DEFAULT now() | |

## 与本次交付的边界

- S1 未新增/修改任何 `db/migrations/` 文件，也未改 `pyproject.toml`；
- 上述表由 S0 在下一版迁移中创建，S1 再补 `PgIdempotencyStore` / `PgRateLimiter` /
  `PgBreakerStore` 三个实现（接口已就绪，替换装配即可）；
- 连接器健康度目前不落库（`HealthTracker` 内存滑动窗口），如要 90 天趋势再另开需求，
  不要顺手塞进上面三张表。
