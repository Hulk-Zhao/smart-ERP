# 观测层（S7）｜埋点约定、trace 还原与成本归因

面向排障与验收的最小观测实现：**一个工单 = 一条 trace**，能看到「决策 → 预演 → 写入 → 校验 → 补偿」五段，能按工单 / Agent / 步骤算调用次数与耗时；同时把 trace 折算成 Prometheus 指标、配套告警规则与 SLO 草案，供监控系统消费。

不接 SaaS APM、不启动外部服务、不引第三方依赖（标准库实现）。导出只写本地 JSON Lines。

---

## 1. 怎么开

| 环境变量 | 默认 | 说明 |
|---|---|---|
| `ERP_TRACE_ENABLED` | 关 | `1/true/yes/on` 才落盘；关闭时 span 仍保留在内存，便于测试与排障 |
| `ERP_TRACE_FILE` | `logs/traces.jsonl` | 追加写；每行一个 span（JSON） |

```python
from observability import Tracer, exporter_from_env

tracer = Tracer(exporter=exporter_from_env())   # 生产装配一行
trace = tracer.start_trace(ticket_id="T-1001", agent="AG-1")
```

导出失败不会影响业务：`JsonlExporter.errors` 记录 IO 异常，工单照常执行。

---

## 2. 冻结约定

### 2.1 标识

- `trace_id`：32 位小写十六进制（W3C），一次工单执行一个；可透传 `traceparent` 头。
- `span_id`：16 位小写十六进制，父子关系由 `parent_id` 表达。
- `format_traceparent()` / `parse_traceparent()` 用于跨进程透传；非法或全零 id 解析返回 `None`。

### 2.2 span 名（不得自造）

| span 名 | 位置 | 必带属性（除 ticket_id / agent） |
|---|---|---|
| `ticket.step` | 工单步骤 | `step_no`、`action_key`、`level`、`system` |
| `agent.decision` | 步骤内 | `decision_kind`（match/attribution/route/draft）、可选 `rule_id`、`model`、`prompt_version`、`confidence` |
| `gateway.preview` | 步骤内 | `connector`、`field_count` |
| `gateway.write` | 步骤内 | `level`、`idem_key`、`connector`、`action_key`；成功后有 `external_id`、`http_status` |
| `gateway.verify` | 步骤内 | `verify_kind`、`verify_result`（match/absent/mismatch） |
| `gateway.compensate` | 步骤内或工单级 | `action_key`、`idem_key`、`compensate_status` |
| `llm.chat` | 步骤内 | `model`、`prompt_version`；**只有实测才写** `input_tokens`/`output_tokens`/`cost_micros` |
| `audit.write` | 步骤内 | `image_id`、`hash_prev` |

子 span 自动继承当前步骤的 `step_no / level / system / action_key`，所以任意一行日志都能按步骤号、写入等级、系统检索。

`status` 只有三种：

- `ok`：正常；
- `error`：技术异常（连接失败、校验不符等），异常仍原样抛出；
- `rejected`：策略拒绝（W3 / 越权 / 审批未满足），附 `reject_code`。它**不是**技术错误，统计时要分开看。

---

## 3. 怎么接

写入路径不变，`WriteGateway` 仍是唯一写出口；只把连接器包一层代理：

```python
from observability import TracedConnector, Tracer

trace = tracer.start_trace(ticket_id=ticket_id, agent="AG-1")
gateway = WriteGateway(
    TracedConnector(connector, tracer, trace),
    approvals=approvals,
)

with tracer.step(trace, step_no=1, action_key=action.key,
                 level=action.level, system="u8"):
    with tracer.decision(trace, kind="match", rule_id="rule.match.supplier",
                         confidence=0.88):
        ...  # 决策逻辑
    await gateway.execute(action, payload, ticket_id=..., idem_key=..., scene=...)

with tracer.span(trace, "audit.write", image_id=image_id, hash_prev=prev_hash):
    ...  # 审计落库
```

补偿通常由运行时在步骤之外触发，`build_decision_chain()` 会按 `action_key` 把它归回对应步骤；不能唯一匹配时保持为「工单级补偿」，不硬塞。

---

## 4. 怎么看

```python
from observability import build_decision_chain, render_decision_chain, render_trace

print(render_trace(trace))
print(render_decision_chain(build_decision_chain(trace)))
```

`render_trace`（父子树）：

```text
trace 12199adb05051d09ae671c0c4027abf2 ticket=T-1001 agent=AG-1 spans=6 status=ok duration=0.3ms
├─ ticket.step #1 action=u8.purchase_order.create_draft level=W1 system=u8 (ok, 0.2ms)
│  ├─ agent.decision kind=match rule=rule.match.purchase_order confidence=0.88 (ok, 0.0ms)
│  ├─ gateway.preview connector=u8 fields=2 (ok, 0.0ms)
│  ├─ gateway.write level=W1 idem=idem-1 external_id=EXT-1 (ok, 0.0ms)
│  └─ gateway.verify result=match (ok, 0.0ms)
└─ gateway.compensate action=u8.purchase_order.create_draft status=compensated (ok, 0.0ms)
```

`render_decision_chain`（按五段还原，缺段显式标注）：

```text
决策链 trace=12199adb05051d09ae671c0c4027abf2 ticket=T-1001 agent=AG-1
└─ 步骤 ticket.step #1 action=u8.purchase_order.create_draft level=W1 system=u8
   ├─ 决策 agent.decision kind=match rule=rule.match.purchase_order confidence=0.88
   ├─ 预演 gateway.preview connector=u8 fields=2
   ├─ 写入 gateway.write level=W1 idem=idem-1 external_id=EXT-1
   ├─ 校验 gateway.verify result=match
   └─ 补偿 gateway.compensate action=u8.purchase_order.create_draft status=compensated
链路覆盖: 决策 → 预演 → 写入 → 校验 → 补偿（5/5，完整）
```

缺段时输出 `（缺失：写入、校验、补偿）` 与 `（2/5，不完整）`——**不补假数据**。

---

## 5. 成本归因

```python
from observability import CostLedger

ledger = CostLedger()
ledger.record_call(ticket_id="T-1001", agent="AG-1", step_no=1,
                   calls=1, duration_ms=12.5, scene_code="AG-1", trace_id=trace.trace_id)
ledger.record_llm_call(ticket_id="T-1001", agent="AG-1", step_no=1,
                       model="rule-based", prompt_version="prompt-v1",
                       duration_ms=3.2)   # token 暂缺就先不传
ledger.summary()          # 按 (工单, Agent, 步骤) 汇总
ledger.llm_usage_rows()   # 结构对齐 llm_usage 表
```

口径：

- `calls`：调用次数（一次 `record_call` 可带 `calls>1`）；
- `duration_ms`：耗时累加；
- `token_coverage`：`实测调用数/总调用数`。token 与成本只对实测调用求和，**未实测保持 `None`，不反推、不补零**；
- `llm_usage_rows()` 只导出有 `model` 的调用，字段与 tech 01 §3.7.3 的 `llm_usage` 对齐（`tenant_id` 由调用方传入）。

---

## 6. 指标映射与 `/metrics`

指标在**标准库**里实现 Prometheus text exposition format 0.0.4，没有引入 `prometheus_client`（许可证与体积未报备前不新增依赖）。

```python
from observability import MetricsRegistry, install_metrics

registry = MetricsRegistry()
mounted = install_metrics(app, registry)  # 读 ERP_METRICS_ENABLED；代码缺省关闭，compose 注入 true
if mounted:
    log.info("metrics enabled at %s (internal only)", "/metrics")
```

| 环境变量 | 默认 | 说明 |
|---|---|---|
| `ERP_METRICS_ENABLED` | 开（compose 注入 `true`；代码缺省为关） | `1/true/yes/on` 才挂载。**关掉等于让该进程承载的规则永远 absent**，不是“省资源” |
| `ERP_METRICS_PATH` | `/metrics` | 端点路径；改值必须同步 `deploy/smart-erp/monitoring/prometheus.yml` 的 `metrics_path` |
| `ERP_METRICS_PORT` | `8011` | **仅 scheduler**：审计链 gauge 写在 scheduler 内存，必须由同一进程 serve；只内网 `expose` |

`GET /metrics` 与 `HEAD` 返回文本，其他方法 405；不进入 OpenAPI schema。`apps/` 由其他流维护，本层只提供 `install_metrics()`，由接入方在装配处显式调用。

### 6.0 暴露面：只允许内网

- `/metrics` 是内部运维接口，**不得随业务公网端口发布**。开启后若挂在业务 app 上，它会继承业务端口的全部暴露面。
- 反向代理**不得**把 `/metrics` 转发到公网。本仓库业务入口是 hk-web 的 nginx（`/erp/api/ → 127.0.0.1:8010`），因此 nginx 必须显式拒绝该路径：

```nginx
location = /erp/api/metrics { deny all; }   # 或 return 404;
```

- 本仓库现状（`deploy/smart-erp/docker-compose.yml`，`feat/alerting-live`）：
  - api 的发布是 `"${ERP_API_BIND:-127.0.0.1}:${ERP_API_PORT:-8010}:8010"`，**默认只绑回环**，宿主不监听 `0.0.0.0`；
  - 公网流量只能经 hk-web 的 nginx 进入，所以 `/metrics` 的实际控制点是 **nginx 的 location 规则**，不是安全组；
    本仓库 `web-nginx.conf` 已加 `location = /erp/api/metrics { return 404; }`（exact match 优先于前缀反代）；
  - 若显式设置 `ERP_API_BIND=0.0.0.0`（演示机宿主直连），`/metrics` 就会落到宿主可达网络，
    此时依赖 nginx 的 deny 已失效（直连不过 nginx），必须用防火墙/安全组再挡一层；
  - `ERP_METRICS_PORT`（默认 8011）在 `feat/alerting-live` 重新引入：只给 scheduler 用，
    compose 只 `expose`、不 `ports`，不会被宿主或公网看到；它不是 api 的端口。
- 推荐拓扑（任选其一）：
  1. **同网络内抓取**（首选）：Prometheus 容器加入 `smart-erp-net`，直接抓 `api:8010/metrics`。容器端口 8010 在栈内本就可达，无需宿主发布，也不经过 nginx。
  2. **宿主本地抓取**：Prometheus 跑在宿主上抓 `127.0.0.1:8010/metrics`，与默认回环绑定一致，公网不可达。
  3. **经 nginx 暴露**：不推荐；如确需，必须 `deny` 上述精确 location，并在变更单里标注临时方案，不得作为长期配置。

### 6.1 指标清单

「采集点」列是硬信息：`无` 表示仓库里**没有任何生产代码调用该写入方法**，指标永远返回 0/空。
规则里引用未接采集点的指标等于挂了一条永不触发的告警（见 §6.1.2）。
本列按 `origin/main`（`0f597b3`，2026-11-11）全仓审计填写；写成 `文件:行` 的必须能直接跳过去，
`无` 的必须在 §6.1.3 缺口清单里再出现一次。

| 指标 | 类型 | 标签 | 多副本聚合 | 采集点 | 口径 |
|---|---|---|---|---|---|
| `smart_erp_tickets_total` | counter | `status`、`agent` | `sum()` / `sum by (status,agent)` | 无 | 工单终态计数；`status ∈ succeeded/failed/rejected`，`rejected` 是策略拒绝，独立于技术失败 |
| `smart_erp_ticket_failures_total` | counter | `failure_class`、`agent` | `sum()` / `sum by (failure_class)` | 无 | 失败工单按 F1–F8 计数（口径见 PRD §5.0） |
| `smart_erp_ticket_failures_unclassified_total` | counter | `agent` | `sum()` | 无 | 失败但未给 F 码的工单；这是**口径缺口**，应长期为 0 |
| `smart_erp_write_stage_total` | counter | `stage` | `sum()` | `apps/platform/policy/gateway.py:226`（写入进程是 api，`apps/main.py:54` serve） | `WriteGateway` 阶段执行次数，`stage ∈ preview/write/verify/compensate` |
| `smart_erp_write_stage_failures_total` | counter | `stage` | `sum()` | `apps/platform/policy/gateway.py:226`（同上） | 阶段失败次数（`error` 或 `rejected`），与上者相除即失败率 |
| `smart_erp_write_stage_duration_seconds` | histogram | `stage` | 先 `sum by (le)` 再 `histogram_quantile` | `apps/platform/policy/gateway.py:226`（同上） | 阶段耗时（秒），默认桶 5ms–10s |
| `smart_erp_recon_batches_total` | counter | `result` | `sum()` / `sum by (result)` | `apps/api/service.py:1137` | 对账批次结果：`matched`（已对账）/`pending_manual`（待人工）/`unreconcilable`（无法对账） |
| `smart_erp_recon_batch_duration_seconds` | histogram | — | 先 `sum by (le)` 再 `histogram_quantile` | `apps/api/service.py:1137` | 批次耗时（秒），默认桶 1s–600s |
| `smart_erp_recon_supplier_lines_total` | counter | `outcome` | `sum()` / `sum by (outcome)` | `apps/api/service.py:1137` | 供应商侧行按行级结果计数（封闭词表见 §6.1.1）；自动匹配率与人工介入率的分子分母 |
| `smart_erp_recon_batch_auto_match_ratio` | histogram | — | 先 `sum by (le)` 再 `histogram_quantile` | `apps/api/service.py:1137` | 单批次自动匹配率（0–1），桶 0.5/0.7/0.8/0.9/0.95/0.99/1.0。**不是准确率**，见 §6.1.1 |
| `smart_erp_recon_degraded_batches_total` | counter | `reason` | `sum()` / `sum by (reason)` | `apps/api/service.py:1137` | 降级（无法对账）批次按缺失项种类归因：`scope`/`supplier`/`item`/`base_unit`/`unit_conversion`/`line_data`/`other` |
| `smart_erp_recon_runs_failed_total` | counter | `stage`、`code` | `sum()` / `sum by (stage,code)` | `apps/api/service.py:983`、`:986` | 对账请求失败次数，`stage ∈ input/engine/persist`，`code` 取 `ErrorCode.value` 或 `unclassified` |
| `smart_erp_unresolved_tickets` | gauge | — | 全局口径 `max()`；分片队列 `sum()` | `apps/agents/compensation_worker.py:287`（同进程 `:302` serve `/metrics`） | `erp.ticket` 中 state 不在终态集合（已完成/已回滚/已终止/已驳回）的工单数 |
| `smart_erp_compensation_backlog` | gauge | — | `sum()` | `apps/agents/compensation_worker.py:288`（同进程 serve） | 当前待处理补偿任务数（规则仍不启用，见 §6.1.3） |
| `smart_erp_compensation_overdue` | gauge | — | `max()` | `apps/agents/compensation_worker.py:289`（同进程 serve） | 到期（>60s）却仍是 `queued` 的补偿任务数；**必须含 `next_retry_at IS NULL` 分支**，否则修复后的无闸门行会静默 |
| `smart_erp_compensation_future_retry` | gauge | — | `max()` | `apps/agents/compensation_worker.py:290`（同进程 serve） | `next_retry_at > now()+18s` 的 queued 数；唯一能直接抓到墙钟回拨的队列信号 |
| `smart_erp_db_clock_skew_seconds` | gauge | — | `max(abs(...))` | `apps/agents/compensation_worker.py:183`（`ClockSampler.sample`，同进程 serve） | 应用单调时钟与墙钟之差：`Δwall − Δtick`，**负值=回拨**（唯一允许负值的 gauge） |
| `smart_erp_db_clock_skew_backwards_seconds_max` | gauge | — | `max()` | `apps/agents/compensation_worker.py:183`（同上） | 进程启动以来最大回拨幅度；只做排查/看板，**不单独告警**（单调最大值置位后不会回落） |
| `smart_erp_audit_chain_broken` | gauge | — | `max()` | `apps/audit/monitor.py:123`、`:144`（经 `core/runtime/scheduler.py::tick`；scheduler 自身 serve `/metrics`，`core/runtime/scheduler.py::_amain`） | 审计链断链标志：0 正常 / 1 断链（P0） |

> **多副本必须聚合**：以上指标都是**进程内累加**，N 个副本就产生 N 条同标签序列。只看单个 pod 的数字会低估全局量；Prometheus 查询必须按上表聚合。规则文件已按聚合口径编写（`sum(...)`、`sum by (le)`、`max(...)`）。

span → 指标的折算规则：包含任一 `rejected` span 的 trace 计为 `rejected`，否则有 `error` 计为 `failed` 并取第一个 `failure_class`/`fail_code`，都没有则进 `unclassified`；`gateway.preview|write|verify|compensate` 分别累加到对应 `stage`。

### 6.1.1 对账质量口径（自动匹配率 / 人工介入率 / 降级率）

> **自动匹配率 ≠ 准确率。** 仓库内没有人工标注的 ground truth（没有任何一份「正确答案」批次），
> 所以 accuracy **不可计算**。指标名、告警名、对外材料与口头口径中**一律不得出现 accuracy**；
> 被问「准确率多少」时，只能回答下面三项，并把分母说清楚。此前出现过的「准确率 0.70」
> 在两个仓库里都找不到可复现来源，属虚构数字，不得再引用。

唯一计算口径（PromQL 形状，已按多副本聚合）：

| 比率 | 分子 | 分母 |
|---|---|---|
| 自动匹配率（行级） | `sum(rate(smart_erp_recon_supplier_lines_total{outcome=~"matched_.*"}[1h]))` | `sum(rate(smart_erp_recon_supplier_lines_total[1h]))` |
| 人工介入率（行级） | `sum(rate(smart_erp_recon_supplier_lines_total{outcome=~"needs_review\|ambiguous\|candidate\|unmatched\|duplicate\|unparseable\|unknown"}[1h]))` | 同上 |
| 降级率（批次级） | `sum(rate(smart_erp_recon_batches_total{result="unreconcilable"}[1h]))` | `sum(rate(smart_erp_recon_batches_total[1h]))` |

批次分布（避免小批次被大批次稀释）：`histogram_quantile(0.5, sum by (le) (rate(smart_erp_recon_batch_auto_match_ratio_bucket[6h])))`。

行级 `outcome` 是**封闭词表**（`RECON_LINE_OUTCOMES`）。新增值必须同时改代码、本文与测试，否则序列会悄悄长出来：

| outcome | 来源（`LineStatus.name` / `MatchType.name`） | 计入自动匹配 |
|---|---|---|
| `matched_exact` / `matched_fuzzy` / `matched_aggregated` | `MATCHED`，按 `match_type` 拆分 | 是 |
| `needs_review` | `NEEDS_REVIEW`（含多行合并匹配） | 否 |
| `ambiguous` | `AMBIGUOUS` | 否 |
| `candidate` | `CANDIDATE_ONLY` | 否 |
| `unmatched` | `UNMATCHED_OURS` / `UNMATCHED_SUPPLIER` | 否 |
| `duplicate` | `DUPLICATE` | 否 |
| `unparseable` | `UNPARSEABLE`（金额/数量无法解析，不静默当 0） | 否 |
| `unaccounted` | 合成值：`supplier_lines − 已归类行数` | 否 |
| `unknown` | 引擎新增、本层未识别的状态 | 否 |

三条实现约束（写在 `MetricsRegistry.observe_recon_result` 的文档里，测试逐条覆盖）：

- **只统计供应商侧行**（`supplier_ref` 非空）。我方单边未匹配不进分母，否则「我方有单、供应商没来」会把匹配率压成假低。
- **`unaccounted` 与 `unknown` 长期必须为 0**：前者说明引擎声明了行数却少给行，后者说明引擎加了状态但没同步本层。二者与 `ticket_failures_unclassified_total` 同级，属口径缺口，不是正常波动。
- **分子不用 `ReconMetrics.matched_rate`**：引擎把 merged（状态是「待人工」）也算进 `matched`，与行级口径不一致。本层按 `status` 重算；两边不一致时以行级为准，差异记在引擎侧待修。

`BatchStatus`（中文枚举）→ `result`（既有英文词表）的映射集中在 `BATCH_STATUS_TO_RESULT`：

| `BatchStatus` | `result` |
|---|---|
| `RECONCILED`（已对账） | `matched` |
| `NEEDS_REVIEW`（待人工复核） | `pending_manual` |
| `NO_CONCLUSION`（无法对账） | `unreconcilable` |

不改 `result` 词表的原因：它已被 T708 的规则文件与看板引用，改名会同时打穿测试与查询；
映射只写一处，未知状态直接抛 `MetricError`，不允许悄悄多出第四个值。

调用方式（每批次一次，**不要**再叠加 `record_recon_batch`，否则批次计数翻倍）：

```python
registry.observe_recon_result(result, duration_seconds=elapsed)  # result 为 apps.recon 的 ReconResult
```

本函数按鸭子类型读取 `batch_status` / `lines` / `metrics.supplier_lines` / `missing_items`，
**不 import `apps.recon`**，观测层不反向依赖业务流（`tests/observability/test_recon_quality.py` 用 AST 断言这一点）。

### 6.1.2 指标定义 ≠ 指标可观测（硬规矩）

只有**存在生产采集点**的指标才允许进告警规则。定义了却没人调用的指标永远返回 0/空，
挂上去的规则永远不触发——比没有告警更坏，因为它制造「已经监控住了」的假象。
2026-11-11 一晚撞到**四次**同一类问题，表面各不相同，实质都是同一条链只在纸面上连通：
`/metrics` 曾未挂载、`set_compensation_backlog` 零调用、11 条规则零采集点，
以及采集点终于有了、却写在没有 `/metrics` 监听的那个进程里（`smart_erp_audit_chain_broken`）。

1. **同批合入**：规则里引用的每一个指标，都必须在**同一次 PR** 里有采集点；做不到时，
   显式标注 `# 采集点待接线（<归属流>）` 并**把该条规则注释掉**，同时登记到 §6.1.3。
   规则、指标、采集点三者不许在三个地方各自演进。
2. §6.1 的「采集点」列必须如实填写（`文件:行` 或 `无`），不得写「预计」「待定」；
   写成 `文件:行` 的，换行/重构后要同批修正，否则等同过期事实。
3. 未接线前，该指标/规则的覆盖度记为 **0**，验收里不得写「已覆盖」「已监控」。
4. 每次增删规则都要重跑 §9 的检查并刷新 §6.1.3；清单里有的指标不允许出现「已验收」。
5. **写入点存在 ≠ 可观测**：每条规则的解锁条件必须同时回答两个问题，缺一不可——
   (a) **哪个进程**写这个指标（`文件:行`）？(b) 那个进程**是否 serve `/metrics`**（给出挂载点 `文件:行`）？
   指标值是进程内内存，只有 serve `/metrics` 的进程才会被 scrape；写在别的进程里的 gauge，
   抓到的一直是「没有这条序列」，与「一直正常」在告警上看起来完全一样。
   `smart_erp_audit_chain_broken` 就是反例：写入点在 scheduler，而 `/metrics` 原本只挂在 api/web
   （`apps/main.py:54`）与补偿执行器（`apps/agents/compensation_worker.py:302`），唯独没有 scheduler。
   **2026-11-11 已补上**：scheduler 在 `core/runtime/scheduler.py::_amain` 用
   `core/runtime/metrics_server.py` 复用 `install_metrics` 起独立监听（默认 8011），P0 规则才真正可评估。
6. **规则不评估 = 规则不存在**：仓库里没有任何抓取/评估进程时，写好的规则与注释掉的规则在故障当天
   没有区别。2026-11-11 核实：`deploy/` 全目录没有 Prometheus / Alertmanager，也没有
   `scrape_configs` / `rule_files`；compose 的服务里没有任何进程读取 `alerts.rules.yaml`。
   **2026-11-11 已补上（仅 full 档）**：`deploy/smart-erp/docker-compose.yml` 的 `prometheus` 加载
   `observability/alerts.rules.yaml`（直接挂载，不复制），`alertmanager` 接收告警；
   抓取目标为 `api:8010`、`scheduler:8011`、`compensation:8013`。
   demo 档刻意不装监控（保持 1200MiB 预算）：在 demo 上只能说「规则已定义、无评估器」，
   不得写「已告警」。评估器本身掉线由规则 `SmartErpScrapeTargetDown` 兜住——
   否则目标消失时所有 `max()/rate()` 规则都会安静，与健康同形。

### 6.1.3 采集点审计表（基准 `feat/alerting-live`，2026-11-11）

判据只有三问：**谁写**（`文件:行`）→ **它跑在哪个进程** → **那个进程是否 serve `/metrics`**。
三问任一为否，规则就是哑的；有抓取与评估进程只是第四步。

| 指标 | 生产写入点（进程） | serve `/metrics` | 规则状态 |
|---|---|---|---|
| `smart_erp_recon_batches_total` / `_duration_seconds` | `apps/api/service.py:1137`（api） | `apps/main.py:54` → `api:8010` | 生效（2 条） |
| `smart_erp_write_stage_*` | `apps/platform/policy/gateway.py:226`（api） | `apps/main.py:54` → `api:8010` | **已删除（2026-11-11 实测）**：演示档 48/48 工单 `pending_write=null`，`WriteGateway.execute` 从未执行，指标恒 absent |
| `smart_erp_unresolved_tickets` | `apps/agents/compensation_worker.py:287`（compensation） | `:302` → `compensation:8013` | 生效 |
| `smart_erp_compensation_overdue` | `:289`（compensation） | `:302` → `compensation:8013` | 生效（3 条） |
| `smart_erp_compensation_future_retry` | `:290`（compensation） | `:302` → `compensation:8013` | 生效 |
| `smart_erp_db_clock_skew_seconds` | `:183` `ClockSampler.sample`（compensation） | `:302` → `compensation:8013` | 生效（2 条） |
| `smart_erp_audit_chain_broken` | `apps/audit/monitor.py:123`、`:144`（scheduler） | `core/runtime/scheduler.py::_amain` → `scheduler:8011` | 生效（P0） |
| `up`（内置） | Prometheus | — | 生效（`SmartErpScrapeTargetDown`） |
| `smart_erp_compensation_backlog` | `:288`（compensation） | `:302` → `compensation:8013` | 否（等重试周期基线） |
| `smart_erp_recon_runs_failed_total` | `apps/api/service.py:1145`、`:1148`（api） | `apps/main.py:54` → `api:8010` | **已删除（2026-11-11 实测）**：46/46 请求 503 `E-CONN-UNAVAILABLE`，`recon_batches_total` 恒 absent |
| `smart_erp_tickets_*` | 无 | — | 无规则，需要时先接 `record_ticket()` |

> **2026-11-11 实测：两条注释规则都删除，而不是启用**（方法与环境见 §9.2，原始记录见 `docs/product/tech/ledger/S7.jsonl`）
>
> | 规则 | 实测分布 | 为什么不能定阈值 |
> |---|---|---|
> | 写失败率 `SmartErpWriteFailureRateHigh` | `smart_erp_write_stage_total/_failures_total` 在空载、三套 demo 场景、1,000 与 10,000 行对账后**全部 absent**；工单 48 条，`pending_write` 48/48 为 `null` | 分母不是 0 而是不存在。`clamp_min(...,0.001)` 会把任何一次失败放大成 100% 常驻告警，而当前连一次失败都产生不了——留着只是「已覆盖写入」的错觉 |
> | 对账请求失败率 `SmartErpReconRunFailureRateHigh` | 46 次真实 `POST /v1/recon/runs`：**46/46 返回 503**，`code=E-CONN-UNAVAILABLE`、`stage=input`；`smart_erp_recon_batches_total` 在三个 api 进程里全 absent | 指标 100% 反映「api 进程未装配连接器」这一个缺陷（全仓没有读取 `ERP_CONNECTOR_GW_URL` 的代码），不是业务失败率；分母缺失时失败率无法定义 |
>
> 两条都改成规则文件里的 tombstone（附重新引入条件）。判据是同一句：
> **恒不触发的规则也是不存在的规则**——留着一条永远不响的规则，比没有这条规则更危险，
> 因为它会让人以为这条链路已经被覆盖。
> 另注：请求体校验失败（422）在进入 handler 前就被拒，本来就不计入 `recon_runs_failed_total`，
> 该指标的分母天生偏小，这也是重建基线时必须一并修的采集缺口。

> **2026-11-11 的两次修正**（都曾在文档里写错，必须留痕）：
> 1. 「2 条生效 / 9 条注释」这个口径是错的——`ERP_METRICS_ENABLED` 当时没有在 compose 注入，
>    api 的 `/metrics` 实测 **404**，所以真实生效数是 **0**。现已注入并默认 `true`，
>    `tests/deploy/test_compose_deploy.py::test_metrics_env_injected_wherever_code_reads_it`
>    会扫描「代码真读的 `ERP_METRICS_*` 键」，compose 漏注入直接红。
> 2. 写失败率的旧理由「`record_stage` 生产零调用」也是错的：`gateway.py:226` 有真实调用、
>    api 也 serve。它仍注释，唯一原因是 5% / 10m 没有真实基线，不是没有采集点。

**补偿 5 条指标 / 6 条规则现在能不能取消注释？——已取消注释（2026-11-11）**

| 判据 | 结论 | 证据 |
|---|---|---|
| 哪个进程写 | ✅ 补偿执行器 | `apps/agents/compensation_worker.py:288-292` |
| 该进程是否 serve `/metrics` | ✅ 写入与暴露同一进程 | `:302` `install_metrics(app, metrics, enabled=True)`，端口 `ERP_COMPENSATION_PORT`（默认 8013） |
| 是否有抓取目标 | ✅ `deploy/smart-erp/monitoring/prometheus.yml` | `erp-compensation → compensation:8013` |
| 是否有评估器 | ✅ full 档 Prometheus 加载规则文件 | `deploy/smart-erp/docker-compose.yml` 的 `prometheus` 服务 |
| demo 档怎么办 | ⚠️ 不装监控，规则不评估 | demo 上不得写「已告警」；这是刻意的档位差异，不是遗漏 |
| 阈值基线 | ⚠️ 0 / 5 / 15m 与 5s / 30s 仍是分析值 | 已生效但需在真实数据上复核；`overdue` 的 P2/P1 分档保证单条不会半夜叫醒人 |

`recon_runs_failed_total` 的建议规则（原为注释，**2026-11-11 实测后已删除**，理由见本节上表）：
`sum(rate(smart_erp_recon_runs_failed_total[15m])) / clamp_min(sum(rate(smart_erp_recon_batches_total[15m])), 0.001) > 0.05`，
`for: 15m`，P1。分母用已处理批次数、避免夜间空窗把 1 次失败放大成高失败率的思路仍然成立；
但它依赖「有成功批次」这个前提，而当前 api 进程没有装配连接器，46/46 请求都在 `input` 阶段 503，
`recon_batches_total` 恒 absent。**在修好装配并跑出一个成功批次之前，这条规则不具备可评估性**，
故不保留空壳；重新引入时沿用上面的表达式与本节记录的采集缺口（422 不计入分母）。

> 结论：full 档已具备「写入进程 === serve 进程 + 抓取 + 规则评估 + Alertmanager 接收」四段闭环。
> demo 档仍然没有评估器，只能写「规则已定义、无评估器」。
> 「会响」的唯一证据是 Prometheus 里规则 `inactive → pending → firing` 的状态迁移，
> 以及 Alertmanager `/api/v2/alerts` 里的实际条目；只贴 YAML 不算验证。

### 6.1.4 补偿队列与时钟口径

**A 到期未领取**（抓 claim 停摆）：

```sql
SELECT count(*) FROM erp.compensation_task
WHERE state = 'queued'
  AND (next_retry_at IS NULL OR next_retry_at <= now())
  AND coalesce(next_retry_at, updated_at) <= now() - interval '60 seconds';
```

> 实现以 `apps/agents/compensation_worker.py::_METRIC_SQL` 为准。`updated_at` 由
> `0009_compensation_updated_at` 补齐，claim / fail / done / heartbeat 都会刷新，
> 并带 `(state, updated_at) WHERE state='queued'` 局部索引。
> 2026-11-11 修正：原实现用 `created_at`，会把「刚被重新入队、created_at 很旧」的行
> 立刻算成 overdue（假阳性）；已改为 `updated_at`。

`next_retry_at IS NULL` 是 `retry_in <= 0` 修复后的正常形态（写入 NULL 表示立即可领），
漏掉这个分支等于给「本该立即领取却没人领」造一个新的静默面。60s 是扫描周期的余量。

**B 未来重试**（唯一的时钟回拨信号）：

```sql
SELECT count(*) FROM erp.compensation_task
WHERE state = 'queued' AND next_retry_at > now() + interval '18 seconds';
```

18s = 最大重试退避 8s + 10s 余量；正常退避不可能写出这么远的未来，超出即时间线错乱。

**C 应用侧时钟偏移**（根因信号，任务卡住之前就能报）：同一采样点各取一次
`time.monotonic()` 与 `time.time()` 的增量，调用
`registry.observe_clock_skew(monotonic_delta_seconds=Δtick, wall_delta_seconds=Δwall)`。
`skew = Δwall − Δtick`，负值即回拨；回拨幅度同时并入进程内最大值。

### 6.2 标签克制（硬约束）

- `ticket_id`、`idem_key`、`external_id`、`trace_id`、`span_id`、`batch_id`、`doc_no`、`supplier_code`、`org_id`、`period`、`status_reason` 以及任何 `*_id` 后缀**禁止**作为标签，传入即抛 `MetricLabelError`；
- 未声明或缺失的标签同样报错，避免悄悄产生新序列；
- 工单级细节留在 trace / JSONL 里按需检索，时序库只保留可聚合的低基数维度。

后四个（`supplier_code` / `org_id` / `period` / `status_reason`）是 T709 追加的红线，理由分两类：

- **基数爆炸**：供应商数 × 组织数 × 期间数本身就是笛卡尔积，再乘副本数，几天就能把时序库打穿；这类维度只适合按需查询，不适合常驻序列。
- **自由文本**：`status_reason` 是中文叙述（「供应商『华东某某』未绑定」），几乎每批次都不同，等价于给每个批次建一条序列；降级原因只能取封闭的 `MissingKind` 归类（见 `smart_erp_recon_degraded_batches_total`）。

需要按供应商 / 期间看质量时，用 `ReconBatchRecord` 做明细查询，不要把它塞进 Prometheus。

### 6.3 真实输出片段（节选）

```text
smart_erp_tickets_total{status="failed",agent="AG-1"} 1
smart_erp_tickets_total{status="rejected",agent="AG-2"} 1
smart_erp_ticket_failures_total{failure_class="F6",agent="AG-1"} 1
smart_erp_write_stage_total{stage="preview"} 2
smart_erp_write_stage_total{stage="write"} 2
smart_erp_write_stage_failures_total{stage="write"} 1
smart_erp_write_stage_duration_seconds_bucket{stage="write",le="0.005"} 2
smart_erp_write_stage_duration_seconds_bucket{stage="write",le="+Inf"} 2
smart_erp_write_stage_duration_seconds_sum{stage="write"} 8e-06
smart_erp_write_stage_duration_seconds_count{stage="write"} 2
smart_erp_recon_batches_total{result="matched"} 1
smart_erp_recon_batches_total{result="pending_manual"} 1
smart_erp_recon_batch_duration_seconds_bucket{le="60"} 2
smart_erp_recon_supplier_lines_total{outcome="matched_exact"} 9
smart_erp_recon_supplier_lines_total{outcome="unmatched"} 1
smart_erp_recon_batch_auto_match_ratio_bucket{le="0.5"} 0
smart_erp_recon_batch_auto_match_ratio_bucket{le="0.9"} 1
smart_erp_recon_batch_auto_match_ratio_bucket{le="+Inf"} 1
smart_erp_recon_batch_auto_match_ratio_sum 0.9
smart_erp_recon_batch_auto_match_ratio_count 1
smart_erp_recon_degraded_batches_total{reason="scope"} 1
smart_erp_unresolved_tickets 7
smart_erp_compensation_backlog 3
smart_erp_audit_chain_broken 0
```

---

## 7. 告警规则

规则文件：[alerts.rules.yaml](./alerts.rules.yaml)（Prometheus rule file，`groups[0].rules` 共 11 条：**全部生效**；2026-11-11 实测后删除了原先注释的 2 条，理由见 §6.1.3）。

> **评估器只在 full 档存在**：`deploy/smart-erp/docker-compose.yml` 的 `prometheus` 服务
> （profile `full`）通过 `rule_files` 直接挂载本文件，抓取 `api:8010`、`scheduler:8011`、
> `compensation:8013` 三个目标，`alertmanager` 接收告警。`demo` 档刻意不装评估器
> （保持 1200MiB 预算），在 demo 上只能说「规则已定义、无评估器」，**不得写「已告警」**。
> 2026-11-11 之前 `deploy/` 全目录没有 Prometheus / Alertmanager，那 12 条规则没有任何进程评估。

> **本期不接外部通知渠道**：Alertmanager 的 receiver `erp-unrouted` 是空接收器，告警
> 进入 Alertmanager 后不发往任何人（无邮件 / 飞书 / 短信 / webhook）。值班只能靠人看
> firing 面板；验收与看板**不得写「已告警」「已通知」**，只能写「已进入 Alertmanager，
> receiver 为空，无人接收」。将来接通道必须用接收端实际记录证明送达，Alertmanager 显示
> `sent` 不算证据。详见 `deploy/smart-erp/RUNBOOK.md` §7.1。

| 告警 | 状态 | 级别 | `for` | 触发口径 | 为什么值得半夜叫醒人 |
|---|---|---|---|---|---|
| `SmartErpScrapeTargetDown` | 生效 | P1 | 5m | `up == 0` | 目标掉线时 `max()/rate()` 全部安静，与健康同形；不单独兜住会让整套告警变装饰 |
| `SmartErpWriteFailureRateHigh` | **已删除（2026-11-11 实测）** | P1 | 10m | `write` 阶段失败率 > 5% | 演示档 `pending_write` 48/48 为 null，指标恒 absent；分母不存在，留着是空壳（理由与重引入条件见 §6.1.3 / 规则文件 tombstone） |
| `SmartErpUnresolvedTicketsGrowing` | 生效 | P1 | 10m | `max()` 后 30 分钟增量 > 5 且总量 > 10 | unresolved 只增不减 = 没人接住的失败在堆积，会变成对账差异与付款错误 |
| `SmartErpCompensationOverdueP2` | 生效 | P2 | 5m | `overdue > 0` | 补偿到期未被领取，说明 claim 侧开始停摆 |
| `SmartErpCompensationOverdueP1Volume` | 生效 | P1 | 5m | `overdue > 5` | 量级说明不是单条卡住，而是领取链路大面积不工作 |
| `SmartErpCompensationOverdueP1Duration` | 生效 | P1 | 15m | `overdue > 0` 持续 15m | 持续即停摆：该撤未撤的数据留在对方系统，可能已被下游消费 |
| `SmartErpCompensationFutureRetry` | 生效 | P1 | 1m | `future_retry > 0` | 唯一能抓到时钟回拨的信号：补偿被推到未来等于永久不执行 |
| `SmartErpClockSkewP2` | 生效 | P2 | 1m | `|skew| ≥ 5s` | 根因信号，先于「任务卡住」出现，留出止损窗口 |
| `SmartErpClockSkewP1` | 生效 | P1 | 1m | `|skew| ≥ 30s` | 回拨幅度已足以让补偿、超时与租约判断同时失真 |
| `SmartErpReconBatchFailureRateHigh` | 生效 | P1 | 15m | 待人工 + 无法对账占比 > 2% | 对账是关账前置，夜间不修复会让人工队列指数级放大 |
| `SmartErpAuditChainBroken` | 生效 | **P0** | 1m | `max(smart_erp_audit_chain_broken) > 0` | 审计链断链后「谁改了什么」无法自证，每小时都产生新的不可信记录 |
| `SmartErpReconBatchP95Slow` | 生效 | P2 | 10m | 批次 P95 > 120s | 只影响速度不影响正确性，工作时间处理；与失败率告警同发时升级 P1 |
| `SmartErpReconRunFailureRateHigh` | **已删除（2026-11-11 实测）** | P1 | 15m | 对账硬失败率 > 5% | 46/46 请求 503 `E-CONN-UNAVAILABLE`，`recon_batches_total` 恒 absent；指标当前测的是装配缺陷，不是业务失败率（理由见 §6.1.3） |

每条规则都带 `annotations.why_paged` 与 `runbook`，解释「为什么叫醒」和「先看哪里」。

**校验方式（两层，缺一不可）**：

1. **YAML 结构**（PyYAML + 结构断言）：只证明文件能解析、字段齐全，**不证明 `expr` 里的 PromQL 合法**。
2. **PromQL 真校验**（发布前必跑，需要 docker；镜像入口是 `prometheus`，必须用 `--entrypoint promtool`）：

```powershell
docker run --rm --entrypoint promtool `
  -v "E:\tools\Factroy_Agents\smart-ERP:/rules" `
  prom/prometheus:latest check rules /rules/observability/alerts.rules.yaml
```

本机实测输出：

```text
Checking /rules/observability/alerts.rules.yaml
  SUCCESS: 11 rules found
```

> **没有 docker / 拉不到镜像时，不得宣称规则已验证**。此时只能写“YAML 结构已校验，PromQL 未验证”，并把它列为发布阻塞项；配置错误会让 Prometheus 拒绝加载整个 rules 文件，而不是跳过单条。

> **这两层都只证明文件本身**。`SUCCESS: 11 rules found` 不等于「有 11 条告警在盯着线上」——
> 还要确认 full 档的 `prometheus` 已加载本文件、三个抓取目标 `up`。
> `tests/deploy/test_compose_deploy.py::test_monitoring_scrapes_every_metrics_serving_process`
> 会断言抓取目标集合与规则文件挂载路径，防止「加了评估器却没抓某个端点」。

P0 表达式另做真实指标快照求值（不依赖 docker，用于证明告警确实会触发）：

```text
audit_chain_broken=0  max()=0  firing=False
audit_chain_broken=1  max()=1  firing=True
```

`/metrics` 里出现 `smart_erp_audit_chain_broken 1` 后，`max(...) > 0` 立即成立，配合 `for: 1m` 即触发 P0。

**2026-11-11 端到端实测**（一次性环境，与 full 档同配置）：

```text
# 三条抓取目标全部 up
erp-api          http://api:8010/metrics            up
erp-scheduler    http://scheduler:8011/metrics      up
erp-compensation http://compensation:8013/metrics   up

# 篡改 erp.audit_image 一条记录后（临时禁用 append-only 触发器 → UPDATE → 恢复）
t+  0s state=inactive  metric=1
t+ 11s state=pending   active_alerts=1
t+ 83s state=firing    active_alerts=1

# Alertmanager /api/v2/alerts 实际条目
alertname=SmartErpAuditChainBroken  severity=P0  status.state=active
startsAt=2026-11-11T07:32:02Z  fingerprint=d35411f308a167ef  receiver=erp-unrouted
```

> receiver 为空 = 告警到此为止，无人被叫醒（§7 开头）。这条证据证明「规则会评估、会 firing、
> 会送达 Alertmanager」，**不证明「有人收到」**。

---

## 8. SLO 草案

| SLO | 目标 | 指标口径（PromQL 形状） |
|---|---|---|
| SLO-1 工单成功率 | ≥ 99.5%（30 天滚动） | `1 - sum(rate(smart_erp_tickets_total{status="failed"}[30d])) / clamp_min(sum(rate(smart_erp_tickets_total{status="failed"}[30d])) + sum(rate(smart_erp_tickets_total{status="succeeded"}[30d])), 0.001)`；`rejected` 单列，不计入分母 |
| SLO-2 对账批次 P95 耗时 | ≤ 120s | `histogram_quantile(0.95, sum by (le) (rate(smart_erp_recon_batch_duration_seconds_bucket[30d])))` |
| SLO-3 审计链完整 | 100%，错误预算 0 | `avg_over_time(smart_erp_audit_chain_broken[30d]) == 0`；任一时刻为 1 即消耗全部预算 |

错误预算：SLO-1 按 30 天窗口允许 0.5% 失败；预算耗尽后暂停 W1/W2 自动写入、只保留预演。SLO-2 超阈值不阻断业务，先扩批次切分与并发；SLO-3 无预算，触发即按 P0 冻结写入并保全现场。

> 以上 SLO 都依赖查询/评估端。full 档已装 Prometheus 并抓取三个目标，**可以计算**；
> demo 档没有评估器，SLO **暂时不可计算**，不能当作「已达成的指标」引用（§6.1.2 第 6 条）。

---

## 9. 测试与验证

```powershell
.\.venv\Scripts\python.exe -m pytest tests/observability -q   # 82 条：约定 / 树 / 链路 / 成本 / 导出 / 指标 / 对账质量 / 告警 / 暴露面
.\.venv\Scripts\python.exe -m pytest tests/e2e -q             # 11 条：健康检查 / 只读 / W3 / 越权 / 审批 / 路由门禁
```

本地实跑（本分支基于 main `9502d83`）：`pytest tests/observability` **82 passed**、
`pytest tests/deploy`（4 skipped，需 docker 断言）、`promtool check rules` → `SUCCESS: 11 rules found`。
全量回归以 CI 为准：本机默认 Python 3.11 无法收集使用 PEP 695 语法的用例（`uv --python 3.12` 可跑观测面，全量未跑）。

`tests/observability/test_metrics.py` 覆盖：高基数标签拒绝、文本格式与累计桶、span→指标折算（成功 / F6 / rejected / unclassified）、业务指标、补偿到期/未来重试 gauge、时钟偏移的有符号值与进程内最大回拨、非法采样值拒绝、`/metrics` 的 200 与 OpenAPI 排除、非 GET 405、默认不挂载与 `ERP_METRICS_ENABLED` 开关。
`tests/observability/test_recon_quality.py` 覆盖：词表封闭且无 accuracy 指标、观测层不 import `apps.recon`（AST 断言）、只统计供应商侧行、`unaccounted = supplier_lines − 各行之和 == 0` 的自检、`BatchStatus→result` 映射与未知状态报错、降级原因取缺失项而非自由文本、直方图桶边界（`le` 为小于等于）、四条标签红线。
`tests/observability/test_alerts.py` 覆盖：YAML 解析、生效规则恰为 11 条且每条都引用有**生产写入点**的指标（`WRITER_LOCATIONS` 逐条核对）、`why_paged`/`runbook` 完整；注释态规则集合必须为**空**（2026-11-11 起停用只有「删除 + 理由」一种形态）、被删的写失败率/对账请求失败率必须留下实测数字与重新引入条件、补偿/时钟 6 条的 severity/`for`/表达式原样保留、P0 审计链为生效且 `for: 1m`、已删除的 `SmartErpCompensationBacklogHigh` 不再被引用；以及 overdue（P2/P1 量级/P1 时长）、future_retry、clock_skew（P2/P1 分档）在真实 `/metrics` 文本上的触发演示。

`tests/deploy/test_compose_deploy.py` 覆盖：full 档含 `prometheus` / `alertmanager` 且只在 full 出现、镜像 tag 固定、健康检查、卷、总内存预算；`test_monitoring_scrapes_every_metrics_serving_process` 断言抓取目标恰为 api/scheduler/compensation 三个、规则文件直接挂载、Alertmanager 作为告警接收方；`test_metrics_env_injected_wherever_code_reads_it` 扫描代码里真正读取的 `ERP_METRICS_*` 键，compose 漏注入直接失败（这条专治「配置键存在、代码会读、运行时没注入」）。

### 9.1 发布前检查（全部通过才可上线）

1. `pytest tests/observability tests/e2e` 全绿；
2. 规则文件必须跑真实 PromQL 校验：

```powershell
docker run --rm --entrypoint promtool `
  -v "E:\tools\Factroy_Agents\smart-ERP:/rules" `
  prom/prometheus:latest check rules /rules/observability/alerts.rules.yaml
```

必须看到 `SUCCESS: 11 rules found`（**全部生效**；原注释的 2 条已于 2026-11-11 实测后删除，见 §6.1.2/§6.1.3）。**没有 docker / 拉不到镜像时，不得宣称规则已验证**：只能标注“YAML 结构已校验，PromQL 未验证”，并按发布阻塞项处理。注意这只证明语法；要证明「线上有告警」还得按 §7 的端到端方式看到 `inactive → pending → firing` 与 Alertmanager 条目。

3. 确认 `ERP_METRICS_ENABLED` 目标状态：full 档默认 `true`（Prometheus 需要抓）；demo 档如不接内网抓取可关。开启时确认 nginx 已 `deny /erp/api/metrics`、且未设置 `ERP_API_BIND=0.0.0.0`（§6.0）。
4. 部署后逐个确认抓取端点（2026-11-11 实测均为 200）：`api:8010/metrics`、`scheduler:8011/metrics`、`compensation:8013/metrics`；任一不是 200，对应规则就是哑的。
5. 若本次要宣称「已告警」，必须先完成 §7 的通道接入并拿到接收端记录；否则只能写「已进入 Alertmanager，无人接收」（§7.1）。

E2E 说明：健康检查直接打真实的 `apps.main`；只读接口与写入接口是测试专用 `/_e2e/*` 路由，内部调用真实的 `AccessGate`（四道闸）与 `WriteGateway`（唯一写出口），连接器为进程内替身，不启动 PG / 网络。

**测试路由收口**：`apps/main.py` 不含任何 `/_e2e` 路由，也不 import 测试模块；路由只存在于 `tests/e2e/e2e_support.py`，且仅当 `ERP_APP_ENV=test` 时挂到测试进程内新建的 app 实例上。默认、`prod`、`staging`、`dev` 等配置下路径**不存在**（`GET /_e2e/...` 返回 404），也不会出现在 `GET /openapi.json` 的 paths 中。C12 镜像级全链路用例待 S8 镜像产物就绪后补。

### 9.2 对账性能基线（2026-11-11 实测）

**测的是哪一段**：`POST /v1/recon/runs` 的服务端处理链——
请求体校验（JSON→Pydantic）→ 引擎（`build_request` + `reconcile`，含重复计费识别、匹配、归因）→
落库（批次 1 行 + 每条对账行 1 行 INSERT）→ `GET /v1/recon/runs/{id}/diffs` 首页 50 条（含 `result_view` 反序列化）。
**不含**：网络传输、文件上传/解压、连接器拉数（当前 api 未装配连接器）、前端渲染。
因此对外不能说「系统处理 10 万行 X 秒」，只能说这一段服务端耗时。

**为什么在进程内计时而不是打 HTTP**：当前 api 进程没有装配连接器，
`POST /v1/recon/runs` 一律 503 `E-CONN-UNAVAILABLE`（见 §6.1.3），HTTP 端到端不可能产出成功批次。
故在 app 镜像内直接调用同一份 `apps.api.service.run_recon`，只绕过 `pick_meta` 的装配断言与 `_authorize`
（权限校验不参与计时），校验、引擎、store、真实 PG 全部是生产代码。口径缺陷已如实写在上面，不冒充 HTTP 端到端。

**环境**：Windows 11（10.0.26200）· AMD Ryzen 9 9955HX 16C/32T · 31.2 GiB RAM ·
Docker Desktop 32 CPU / 15.2 GiB · PostgreSQL 15 容器同机 · api 单进程单 worker · 对账引擎单线程。

**数据集**：字段与数量/金额分布取自 `demo/scenarios/manufacturing-multi-plant/recon_case.csv`；
每行 `(supplier_code, item_code)` 唯一（200 供应商 × 500 物料），我方与供应商 1:1，每 100 行注入 1 行数量差异。
**不含**「同供应商同物料多单」这类会产生候选笛卡尔积的形态——那属于另一个问题（见瓶颈第 1 条）。

| 档位（每侧行数） | 校验 | 引擎 | 落库 | 结果可查 | 服务端合计 |
|---|---|---|---|---|---|
| 1 万（2 万行对象） | 0.25s | 28.7s | 14.3s | 0.48s | **44.4s** |
| 5 万（10 万行对象） | 0.78s | 598.2s | 57.2s | 2.0s | **674.3s（11 分 14 秒）** |
| 10 万（20 万行对象） | 1.39s | 2268.9s | 63.5s | 3.0s | **2380.1s（39 分 40 秒）** |

明细（10 万档原始输出）：`engine_parse` 2.72s、`engine_reconcile` 2266.21s、`_batch_view` 41.85s、
`snapshot` 0.46s、`price` 0.04s、`save` 63.54s、`run_recon` 合计 2377.04s、首页 3.04s；
批次状态「待人工复核」，首页 50 条差异，`batch_id 2544c22e`。

**瓶颈判断**
1. **引擎匹配是 O(n²)，且随数据量放大到压倒性**：`reconcile` 占服务端耗时 65%（1 万）→ 89%（5 万）→ **95.5%（10 万）**；
   行数 5 倍时引擎耗时 20.8 倍（28.7s → 598.2s），再翻倍时 3.79 倍（598.2s → 2268.9s，n² 理论值 4 倍）。
   代码在 `apps/recon/matching.py:448-452`：每个供应商行都 `enumerate(our_lines)` 全量打分；
   `matching.py:278-285` 的合单候选也是全量扫描。
2. **落库是 N 次往返**：`apps/api/recon_store.py` 的 `save()` 每行一条 INSERT（约 0.3–0.6ms/行），
   5 万行 57.2s、10 万行 63.5s。线性，但常数大，行数再往上会顶到第二。
3. 视图投影（`_batch_view`）10 万行 41.9s、结果首页 3.0s，都不是主因。
4. 内存：5 万行/侧时进程 RSS 约 1.0 GiB；10 万行/侧引擎阶段（第 18 分钟采样）RSS 850 MiB，
   容器无内存上限、未 OOM、未见交换——**当前瓶颈是 CPU 计算，不是内存**。

**优化建议（一条，先做第一个）**：把匹配从「全表扫描」改成「按 `(supplier_code, item_code, period)` 分桶后桶内打分」，
`matching.py:448-452` 的双层循环即可从 O(n²) 降到 O(n·桶内行数)；
随后把 `save()` 的逐行 INSERT 换成 `executemany` / 多值 VALUES。前者决定复杂度，后者决定常数，两者都不需要改协议。

**4C4G 目标的差距**：本基线是 16C/32T 工作站，但引擎单线程，核数不是变量；真正的变量是 PG 与宿主 IO。
4C4G 上落库会更慢，**必须重测后再对外报数**，不要把本表数字直接贴给 POC 客户。

> 测法、可复现脚本、重复性（1 万档暖机后 22.3–28.1s，冷库首轮 44.4s）与目标规格补齐清单见
> [`tests/perf/README.md`](../tests/perf/README.md)；脚本为 `tests/perf/recon_baseline.py`。
> 该节还记录了 S7 对 S2 分桶优化的同机复核：**「两个数量级」只对引擎阶段成立**
> （5/10 万档 174–316 倍），端到端 10 万档约 24 倍，不能写成整体提升两个数量级。

---

## 10. 边界

- 只读观测，不改变 `WriteGateway`、状态机、审计的任何行为；异常原样抛出。
- W3 没有执行路径；`rejected` span 只证明「拒绝发生了」，不产生任何写入。
- 不记录凭据明文：含 `credential/password/secret/authorization` 的键写为 `[REDACTED]`。
- 不存全量输入；模型调用只记 `model`、`prompt_version`、实测 token / 成本（与 `agent_decision` 的「存输出不存全量输入」一致）。
- `/metrics` 默认不挂载，必须由接入方用 `install_metrics()` 并显式开启 `ERP_METRICS_ENABLED`；该端点只允许内网访问，不得随公网端口发布。指标标签不含任何工单号 / 幂等键 / 外部单号，高基数细节只留在 trace。
