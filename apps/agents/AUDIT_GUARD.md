# 审计链守护与出表闸门

> 归属：S3 运行时（C15）。代码：`apps/agents/audit_guard.py`；
> 迁移：`db/migrations/versions/0004_audit_chain_anchor.py`。

## 1. 对外口径（统一表述，勿改）

> **审计链保证篡改可被发现并立即冻结写入，不承诺篡改不可能。**

哈希链不是密码学意义上的不可篡改。它保证两件事：
改了会被发现（全链重放 + 锚点核对），以及发现后系统立即停止写入，
避免基于被污染的记录继续出表、付款或改单。任何文档、销售材料、对客说明
都不要写「防篡改」「不可篡改」「tamper-proof」。

## 2. 三层防线

| 层 | 触发时机 | 做什么 | 失败行为 |
| --- | --- | --- | --- |
| 热路径 O(1) | 每次 `TicketRuntime.execute` 前 | 读该工单锚点 + 链尾事件，比对 `seq/hash` 与 `prev_hash` | 抛 `AuditChainBroken`，该工单 **0 写入**，发 P0 |
| 定时全链巡检 | 常驻进程按周期（建议 1h，可配） | 批量取事件 → 逐工单 `replay` → 比对锚点 | **先**置全局 `readonly`，**再**发 P0 |
| 出表闸门 | 生成计量表/对账单前 | 对本次出表涉及的工单做全链重放 + 锚点核对 | 抛 `AuditChainBroken`，**拒绝出表**，并已冻结写入 + 发 P0 |

全局只读时：写类动作（level > W0）抛 `WriteDegraded`；**W0 只读查询不受影响**。
巡检全绿只刷新锚点，**不自动恢复** `writable`，须人工确认后调用
`AuditGuard.set_global_mode("writable", reason)`。

## 3. 出表前全链校验（给 API 侧的接入说明）

> <strong style="color:#c00">红字警告：异步 API（FastAPI 请求处理中）**必须** `await AuditGuard(store).assert_exportable(ticket_ids)`，**禁止**调用同步的 `verify_before_export`。</strong>
> 请求里已有运行中的事件循环，`asyncio.run()` 会抛
> `RuntimeError: asyncio.run() cannot be called from a running event loop`。
> 同步包装器只给 **CLI / 定时任务 / 离线脚本** 用；误用时函数会主动抛带指引的
> `RuntimeError`，不会静默进入错误路径。

### 3.1 异步 API（请求内，唯一正确用法）

```python
from apps.agents.audit_guard import AuditChainBroken, ExportLimitExceeded

guard = AuditGuard(ticket_store, alert_sink=alert_sink)   # 进程内复用
report = await guard.assert_exportable(batch_ticket_ids)  # 必须在 statement 之前
# 到这里才允许继续生成计量表/对账单
```

### 3.2 同步 CLI / 定时任务

```python
from apps.agents.audit_guard import AuditChainBroken, ExportLimitExceeded, verify_before_export

report = verify_before_export(
    database_url,                 # postgresql+asyncpg://...
    ticket_ids=batch_ticket_ids,  # Sequence[UUID]，建议按出表批次传
    alert_sink=alert_sink,        # 接到分页通道；不传则 P0 只落本地
)
```

| 项 | 说明 |
| --- | --- |
| 异步方法 | `AuditGuard.assert_exportable(ticket_ids)`（返回 `ScanReport`） |
| 同步函数 | `apps.agents.audit_guard.verify_before_export(database_url, *, ticket_ids=None, alert_sink=None)`（内部 `asyncio.run` + 自建引擎；**仅 CLI/定时任务**） |
| 成功返回 | `ScanReport(checked, broken=(), degraded=False, elapsed_ms)` |
| 断链失败 | 抛 `AuditChainBroken`（`code = ErrorCode.STATE_ILLEGAL`），消息含断链工单列表 |
| 超规模失败 | 抛 `ExportLimitExceeded`（`code = ErrorCode.STATE_ILLEGAL`），消息含事件数与上限 |
| 耗时量级 | 与本次覆盖的事件总数近似线性；本机 PG15 实测：5 千条约 0.2s、2 万条约 0.65s；一次批量查询，无 N+1 |
| 副作用 | 断链时先把全局写入置 `readonly`、再发 P0，然后抛异常 |
| 建议 | 按出表批次传 `ticket_ids`，别每次扫全库；对账/计量任务与在线 API 分开跑 |

### 3.3 规模上限（硬闸门，不允许降级放行）

- 单批上限 **50,000 条事件**（约 2s 预算）；超过即抛 `ExportLimitExceeded`，**拒绝出表**；
- 处理方式只有一个：**按工单/时间拆成多批**分别校验、分别出表；
- **不允许**改成「后台异步校验、先出表后补报告」。出表是收钱依据，校验没通过就不能出；
- 若未来要支持超大客户年度批次，应拆批并行校验（每批仍受 50k 上限约束），
  而不是提高单批上限或取消闸门。

## 4. 巡检周期与承诺时延

- `daily_verify()`：每日全链巡检（保留旧名）；
- `hourly_verify()`：小时级全链巡检；
- `run_periodic(interval_seconds=..., max_scans=None)`：可配置周期循环，启动即扫一次，
  常驻进程建议 `interval_seconds=3600`（最小建议 300s），部署在 worker/scheduler 进程，
  不要放在 API 请求路径里。

```python
await guard.run_periodic(interval_seconds=3600)   # 常驻；断链后写入已在返回前冻结
```

**时延口径：**

- **P0 触发 → 写入冻结：≈ 0**。巡检代码先提交 `readonly`，再发 P0；收到 P0 时写入已冻结。
- **篡改发生 → 写入冻结**：最长 = 巡检间隔 + 单次全链扫描耗时（默认 1h + O(事件数)）。
  热路径会在下一次 `execute` 时立刻拒绝该工单，不等巡检。

## 5. 迁移与部署要点

- `0004_audit_chain_anchor`：建 `erp.ticket_event_anchor`、`erp.audit_guard_state`，
  给 `compensation_task` 加 `lease_owner/heartbeat_at` 与 stale 索引；
- 升级时从 `ticket_event` 链尾回填锚点。**不回填会让存量工单被热路径全部判为断链**；
  回填只是链尾快照，链是否可信由首次全链巡检确认；
- 应用账号对两表只需 `SELECT/INSERT/UPDATE`，**不得给 DELETE**（审计证据）；
- 上线顺序：先迁移 → 再启动巡检跑一次全绿 → 再开放执行写入。

## 6. 验证

```powershell
python -m pytest tests/agents/test_audit_guard.py tests/agents/test_schema_parity.py -q
```

- `test_audit_guard.py`：链尾篡改拒绝 execute、中间篡改由巡检发现并全局只读、
  出表闸门拒绝断链、超规模批次拒绝出表、同步接口误用于事件循环时快速失败、
  可配置巡检次数、同步接口成功/失败；
- `test_schema_parity.py`：迁移 head 表集合 == ORM metadata（双向差集为空）。
- 迁移回填校验：在 0003 的库上篡改一条事件后 `alembic upgrade head` 会中止并打印
  `seq=N 哈希自校验失败`，版本停在 0003、不创建锚点表；合法链则回填成功。
