# Agent 运行时混沌演练报告

- 时间：2026-11-11 01:06:14 中国标准时间
- 环境：Python 3.11.9 / PostgreSQL 15（Docker 临时容器，演练后删除）
- 范围：`apps/agents/` 运行时（intent→verify、租约、补偿队列、事件溯源）
- 本轮结论：**4/4 通过**（基线 1/4，见下节）

## 基线：首次演练（修复前）**1/4 通过**

首次演练于 2026-11-11 00:54 执行，仅 D2 通过。三条未通过的理由**原样保留**（后续任何「通过」
都必须来自实现真的改好，而不是放宽期望）：

| 演练 | 基线结果 | 未通过理由（基线原文） |
| --- | --- | --- |
| D1 外部写入成功但本地未提交 | **未通过** | 与任务书期望不一致：verify 返回 match 时实现按技术架构 §3.1.4 判为 succeeded(recovered) 并直接完成，而不是 unresolved→人工；且当时 verify 只证明「外部有记录」，没有按幂等键/外部单号唯一确认，也没有 recovered 证据事件与阈值。 |
| D2 租约双跑 | 通过 | —（赢家 worker-B，输家 LeaseNotHeld、零写入，外部只写一次） |
| D3 补偿中断（第 2 步） | **未通过** | 真缺陷：崩溃后任务停在 `{3: done, 2: running, 1: queued}`，重启补偿未执行任何一步；`claim_next` 只取 queued，遗留 running 无租约/无回收，且被 `compensation_task_one_running_idx` 挡住抛 `IntegrityError: duplicate key value violates unique constraint "compensation_task_one_running_idx"`，工单永久停在补偿中。 |
| D4 审计链断链（篡改 ticket_event） | **未通过** | 真缺陷：DB 触发器只挡住普通 UPDATE、显式重放能抛 ReplayError，但执行热路径完全不校验事件链——篡改 seq=2 后 execute 仍「静默完成 → 已完成」并产生外部写入，没有巡检/告警接线。 |

基线结论：**1/4 通过**（D1 属任务书期望写错；D3、D4 属真缺陷）。

## 本轮复跑结果（修复后）

| 演练 | 结果 | 一句话业务后果 |
| --- | --- | --- |
| D1 外部写入成功但本地未提交 | 通过 | 若 verify 只凭「外部看着有记录」而不按幂等键唯一确认，崩溃恢复会把只写了一半、或别人写的记录当成成功直接结单，造成错付且无人复核。 |
| D2 租约双跑 | 通过 | 若两个 worker 同时写成功，同一条付款/改单会被执行两次（重复付款）。 |
| D3 补偿中断（第 2 步） | 通过 | 补偿卡在第 2 步不往前走，前面已撤销、后面仍在生效，账实永远对不上且无人处置。 |
| D4 审计链断链（篡改 ticket_event） | 通过 | 事件被篡改后如果继续执行，Agent 会基于被污染的记录付款/改单，而且事后无法证明哪一版数据是真的——审计链等于失效。 |

## D1 外部写入成功但本地未提交

**注入方式**：步骤先落 intent（本地已提交）→ WriteGateway 调外部 write 成功 → 在 verify 阶段抛 WorkerCrash(BaseException) 模拟进程死亡（外部已写、本地未提交，进程在 verify 时死亡）。

**期望行为**：崩溃后步骤停在 intent；重启走 intent→verify，**按幂等键唯一确认**后判 succeeded(recovered)，外部只写一次、不盲目重放；必须写显式 recovered 事件（带 step_no + idem_key 证据），工单完成。（原任务书「崩溃即转人工」的期望已撤回，§3.1.4 的 match→succeeded 才是规格。）

**实际行为**：崩溃时：步骤=intent、工单=执行中、外部写入=1 次（内存计数 1）；重启后：步骤=succeeded、工单=已完成、外部写入累计=1 次、重放写入=0 次、执行报告=已完成（recovered=1）；recovered 事件=1 条，证据={'verify': {'ok': True}, 'step_no': 1, 'idem_key': 'idem-s1'}

**是否通过**：是

**业务后果**：若 verify 只凭「外部看着有记录」而不按幂等键唯一确认，崩溃恢复会把只写了一半、或别人写的记录当成成功直接结单，造成错付且无人复核。

**证据**：

- 崩溃后 erp.ticket_step.state = intent（intent 已提交、结果未回写）
- 崩溃后 erp.ticket.state = 执行中
- chaos.write_log 崩溃时计数 = 1
- 重启后 chaos.write_log 累计计数 = 1（证明外部只写了一次）
- 重启后 step.state = succeeded，ticket.state = 已完成
- recovered 事件 = 1 条，证据坐标 = {'verify': {'ok': True}, 'step_no': 1, 'idem_key': 'idem-s1'}

**发现**：任务书原期望「崩溃即转人工」写错，规格 §3.1.4 的 match→succeeded 是对的；已按决策收口，并补上唯一键确认、recovered 证据事件、阈值转人工三项条件。

## D2 租约双跑

**注入方式**：同一工单进入执行中后，拉起两个独立 Python 进程（各自建引擎、各自连接器），对齐到同一时刻调用 execute；赢家外部写故意延迟 2s，放大并发窗口。

**期望行为**：两个 worker 只有一个拿到租约并执行；输家抛 LeaseNotHeld、不得产生任何外部写入；工单最终只被写一次并完成。

**实际行为**：赢家=worker-A；输家=worker-B（LeaseNotHeld: 工单 d08549d8-b09b-48ef-93a3-752e7dd2cb7f 已被 'worker-B' 之外的 worker 持有租约）；外部写入按工单计数=1、输家写入=0；最终工单=已完成

**是否通过**：是

**业务后果**：若两个 worker 同时写成功，同一条付款/改单会被执行两次（重复付款）。

**证据**：

- worker 结果：[{"worker": "worker-A", "ok": true, "status": "已完成", "writes": 1, "error": null}, {"worker": "worker-B", "ok": false, "status": null, "writes": 0, "error": "LeaseNotHeld: 工单 d08549d8-b09b-48ef-93a3-752e7dd2cb7f 已被 'worker-B' 之外的 worker 持有租约"}]
- chaos.write_log 该工单累计 = 1
- 输家写入次数 = 0
- 最终 erp.ticket.state = 已完成

## D3 补偿中断（第 2 步）

**注入方式**：4 步计划：1–3 成功、第 4 步失败 → 自动进入补偿中并按 compensate_seq 倒序入队；补偿器在 compensate_seq=2 时抛 WorkerCrash 模拟进程死亡，任务停在 running。

**期望行为**：重启后从第 2 步继续，顺序严格 3→2→1，不跳步、不重复补偿，全部完成后工单进入已回滚。

**实际行为**：第 4 步失败后工单=补偿中；入队顺序=[3, 2, 1]；崩溃前已执行=[3, 2]；崩溃后任务状态={3: 'done', 2: 'running', 1: 'queued'}、工单=补偿中；启动恢复回收=[2]；重启后补偿顺序=[2, 1]、错误=无、最终任务状态={3: 'done', 2: 'done', 1: 'done'}、最终工单=已回滚（报告=已回滚）

**是否通过**：是

**业务后果**：补偿卡在第 2 步不往前走，前面已撤销、后面仍在生效，账实永远对不上且无人处置。

**证据**：

- 补偿任务入队顺序（ORDER BY compensate_seq DESC）= [3, 2, 1]
- 崩溃后任务状态 = {3: 'done', 2: 'running', 1: 'queued'}
- 崩溃后工单状态 = 补偿中
- 启动恢复回收的 stale running = [2]
- 重启后补偿调用顺序 = ['idem-s2', 'idem-s1']，异常 = 无
- 最终任务状态 = {3: 'done', 2: 'done', 1: 'done'}，最终工单 = 已回滚

**发现**：已修复并复跑通过：补偿任务增加 owner + heartbeat_at + attempts 租约，claim_next 用 FOR UPDATE SKIP LOCKED 且跳过 stale running 重新入队，启动时有全库恢复入口；attempt 超限转人工 + P0 告警；单跑由状态机保证，唯一索引只做兜底。

## D4 审计链断链（篡改 ticket_event）

**注入方式**：先尝试普通 UPDATE（应被 append-only 触发器拒绝）；再用超级用户 DISABLE TRIGGER USER 分别篡改「链尾最新事件」与「中间事件」的 payload，随后恢复触发器（模拟磁盘损坏）。

**期望行为**：每次 execute 前做 O(1) 校验：链尾哈希与当日锚点不一致就拒绝执行并报 P0；离线全链巡检发现断链升 P0 并把全局写入降级为只读，不得静默继续。

**实际行为**：普通 UPDATE 被拒绝=DBAPIError: (sqlalchemy.dialects.postgresql.asyncpg.Error) ticket_event 只允许追加，拒绝 UPDATE；链尾篡改后 execute=拒绝 → AuditChainBroken: 审计链异常（工单 b92be90d-6eb4-407d-8703-4d1e615965d8）：seq=3 哈希自校验失败（事件被改写）（写入 0 次）；中间篡改后 execute=已完成（O(1) 边界，写入 1 次）；离线巡检 broken=['b92be90d-6eb4-407d-8703-4d1e615965d8', 'f30811e5-b3e5-43f3-ae5b-09dafcea1081']、全局模式=readonly；降级后 execute=WriteDegraded: 审计链巡检已把系统降级为只读，工单 605ceee9-3c89-40c3-8949-57b40d2bf0ec 的写类动作拒绝执行（写入 0 次）；P0 告警=3 条

**是否通过**：是

**业务后果**：事件被篡改后如果继续执行，Agent 会基于被污染的记录付款/改单，而且事后无法证明哪一版数据是真的——审计链等于失效。

**证据**：

- 正常 UPDATE 结果：DBAPIError: (sqlalchemy.dialects.postgresql.asyncpg.Error) ticket_event 只允许追加，拒绝 UPDATE
- 链尾篡改后 execute：AuditChainBroken: 审计链异常（工单 b92be90d-6eb4-407d-8703-4d1e615965d8）：seq=3 哈希自校验失败（事件被改写），外部写入=0
- 中间篡改后 execute：已完成（O(1) 只核对链尾，中间事件由离线巡检负责）
- 离线巡检：checked=3，broken=['b92be90d-6eb4-407d-8703-4d1e615965d8', 'f30811e5-b3e5-43f3-ae5b-09dafcea1081']
- 全局写入模式：readonly
- 降级后 execute：WriteDegraded: 审计链巡检已把系统降级为只读，工单 605ceee9-3c89-40c3-8949-57b40d2bf0ec 的写类动作拒绝执行，外部写入=0
- P0 告警：['AUDIT_CHAIN_BROKEN', 'AUDIT_CHAIN_BROKEN', 'AUDIT_CHAIN_BROKEN']

**发现**：已修复并复跑通过：热路径在 execute 前做链尾/锚点 O(1) 校验并 fail-closed；离线全链巡检发现断链升 P0 且全局降级只读。注意 O(1) 热校验按设计看不到中间事件篡改（哈希链必须从头重算），这一层由离线巡检兜底。

## 决策与修复记录

基线三条未通过的处置（均由人决策，非为让演练变绿而放宽期望）：

- **D1**：保留技术架构 §3.1.4 的 `match→succeeded`，**撤回任务书「崩溃即转人工」的期望（任务书期望写错，规格是对的）**。收口三条件已实现：verify 必须按幂等键/外部单号唯一确认（非唯一 verify 规范 fail-closed 判 mismatch）；每次 recovered 写显式事件并带`step_no/idem_key/verify_observed` 证据坐标；recovered 进指标与阈值（同步骤 2 次 / 单日3 次）→ 超阈值 unresolved + P0，补偿后转人工。
- **D3**：真缺陷，已修。补偿任务加 `lease_owner + heartbeat_at + attempts` 租约；`claim_next` 用 `FOR UPDATE SKIP LOCKED` 并跳过 stale running 重新入队；attempt 超限转人工 + P0；单跑由状态机保证，唯一索引只做兜底；新增启动恢复入口`recover_stale_compensations()`。
- **D4**：真缺陷，已修。热路径每次 execute 前做 O(1) 校验（链尾哈希 vs 当日锚点），不一致直接拒绝（fail-closed，不静默跑完）；离线巡检 `daily_verify()` 全链校验 + 锚点核对，发现断链升 P0 并全局降级只读（W0 只读不受影响）。O(1) 看不见中间事件篡改，这一层由离线巡检兜底，属设计边界而非缺口。

## 仍需关注

四条演练均符合预期（基线 1/4 → 本轮见上表）。
