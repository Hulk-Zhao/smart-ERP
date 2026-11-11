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
mounted = install_metrics(app, registry)  # 默认读 ERP_METRICS_ENABLED，缺省不挂载
if mounted:
    log.info("metrics enabled at %s (internal only)", "/metrics")
```

| 环境变量 | 默认 | 说明 |
|---|---|---|
| `ERP_METRICS_ENABLED` | 关 | `1/true/yes/on` 才挂载；与 `ERP_TRACE_ENABLED` 同一原则，生产默认不暴露 |
| `ERP_METRICS_PATH` | `/metrics` | 端点路径；必须先由反向代理/网络策略限定为内网 |

`GET /metrics` 与 `HEAD` 返回文本，其他方法 405；不进入 OpenAPI schema。`apps/` 由其他流维护，本层只提供 `install_metrics()`，由接入方在装配处显式调用。

### 6.0 暴露面：只允许内网

- `/metrics` 是内部运维接口，**不得随业务公网端口发布**。开启后若挂在业务 app 上，它会继承业务端口的全部暴露面。
- 反向代理**不得**把 `/metrics` 转发到公网。本仓库业务入口是 hk-web 的 nginx（`/erp/api/ → 127.0.0.1:8010`），因此 nginx 必须显式拒绝该路径：

```nginx
location = /erp/api/metrics { deny all; }   # 或 return 404;
```

- 本仓库现状（`deploy/smart-erp/docker-compose.yml`，main `e78422c`）：
  - api 的发布是 `"${ERP_API_BIND:-127.0.0.1}:${ERP_API_PORT:-8010}:8010"`，**默认只绑回环**，宿主不监听 `0.0.0.0`；
  - 公网流量只能经 hk-web 的 nginx 进入，所以 `/metrics` 的实际控制点是 **nginx 的 location 规则**，不是安全组；
  - 若显式设置 `ERP_API_BIND=0.0.0.0`（演示机宿主直连），`/metrics` 就会落到宿主可达网络，此时**不得开启** `ERP_METRICS_ENABLED`；
  - `ERP_METRICS_PORT` / 8011 独立监听在 `e1926cf` 已作为死键删除，**不存在**单独 metrics 端口，不要再按旧文档配置。
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
| `smart_erp_write_stage_total` | counter | `stage` | `sum()` | 无 | `WriteGateway` 阶段执行次数，`stage ∈ preview/write/verify/compensate` |
| `smart_erp_write_stage_failures_total` | counter | `stage` | `sum()` | 无 | 阶段失败次数（`error` 或 `rejected`），与上者相除即失败率 |
| `smart_erp_write_stage_duration_seconds` | histogram | `stage` | 先 `sum by (le)` 再 `histogram_quantile` | 无 | 阶段耗时（秒），默认桶 5ms–10s |
| `smart_erp_recon_batches_total` | counter | `result` | `sum()` / `sum by (result)` | `apps/api/service.py:991` | 对账批次结果：`matched`（已对账）/`pending_manual`（待人工）/`unreconcilable`（无法对账） |
| `smart_erp_recon_batch_duration_seconds` | histogram | — | 先 `sum by (le)` 再 `histogram_quantile` | `apps/api/service.py:991` | 批次耗时（秒），默认桶 1s–600s |
| `smart_erp_recon_supplier_lines_total` | counter | `outcome` | `sum()` / `sum by (outcome)` | `apps/api/service.py:991` | 供应商侧行按行级结果计数（封闭词表见 §6.1.1）；自动匹配率与人工介入率的分子分母 |
| `smart_erp_recon_batch_auto_match_ratio` | histogram | — | 先 `sum by (le)` 再 `histogram_quantile` | `apps/api/service.py:991` | 单批次自动匹配率（0–1），桶 0.5/0.7/0.8/0.9/0.95/0.99/1.0。**不是准确率**，见 §6.1.1 |
| `smart_erp_recon_degraded_batches_total` | counter | `reason` | `sum()` / `sum by (reason)` | `apps/api/service.py:991` | 降级（无法对账）批次按缺失项种类归因：`scope`/`supplier`/`item`/`base_unit`/`unit_conversion`/`line_data`/`other` |
| `smart_erp_recon_runs_failed_total` | counter | `stage`、`code` | `sum()` / `sum by (stage,code)` | `apps/api/service.py:983`、`:986` | 对账请求失败次数，`stage ∈ input/engine/persist`，`code` 取 `ErrorCode.value` 或 `unclassified` |
| `smart_erp_unresolved_tickets` | gauge | — | 全局口径 `max()`；分片队列 `sum()` | 无 | 当前 unresolved 工单数 |
| `smart_erp_compensation_backlog` | gauge | — | `sum()` | 无 | 当前待处理补偿任务数（对应 P1 规则已删除，见 §6.1.3） |
| `smart_erp_compensation_overdue` | gauge | — | `max()` | 无（待 apps/agents） | 到期（>60s）却仍是 `queued` 的补偿任务数；**必须含 `next_retry_at IS NULL` 分支**，否则修复后的无闸门行会静默 |
| `smart_erp_compensation_future_retry` | gauge | — | `max()` | 无（待 apps/agents） | `next_retry_at > now()+18s` 的 queued 数；唯一能直接抓到墙钟回拨的队列信号 |
| `smart_erp_db_clock_skew_seconds` | gauge | — | `max(abs(...))` | 无（待 apps/agents） | 应用单调时钟与墙钟之差：`Δwall − Δtick`，**负值=回拨**（唯一允许负值的 gauge） |
| `smart_erp_db_clock_skew_backwards_seconds_max` | gauge | — | `max()` | 无（待 apps/agents） | 进程启动以来最大回拨幅度；只做排查/看板，**不单独告警**（单调最大值置位后不会回落） |
| `smart_erp_audit_chain_broken` | gauge | — | `max()` | 无 | 审计链断链标志：0 正常 / 1 断链（P0） |

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
   `smart_erp_audit_chain_broken` 就是反例：写入点在 scheduler，而 `/metrics` 只挂在 api/web
   （`apps/main.py:35`）与补偿执行器（`apps/agents/compensation_worker.py:302`），唯独没有 scheduler，
   两边不通，所以这条 P0 继续保持注释——不明就里地取消注释，等于恢复一条哑告警。
6. **规则不评估 = 规则不存在**：仓库里没有任何抓取/评估进程时，写好的规则与注释掉的规则在故障当天
   没有区别。2026-11-11 核实：`deploy/` 全目录没有 Prometheus / Alertmanager，也没有
   `scrape_configs` / `rule_files`；compose 的 10 个服务里没有任何进程读取 `alerts.rules.yaml`。
   当前 2 条「生效」规则的真实状态只是「语法合法 + 有采集点」，**不构成可观测性**。
   评估器（含抓取配置）接入前，验收与文档不得写「已告警」「线上有告警」，只能写「规则已定义、待评估器」。

### 6.1.3 采集点审计表（基准 `origin/main` `0f597b3`，2026-11-11）

可复现的审计命令（rebase 到 `origin/main` 后执行）：

```powershell
git grep -n -E "\.(record_ticket|record_stage|record_recon_batch|observe_recon_result|record_recon_failure|set_unresolved|set_compensation_backlog|set_compensation_overdue|set_compensation_future_retry|observe_clock_skew|set_audit_chain_broken|observe_trace)\(" -- . ':(exclude)observability' ':(exclude)tests' ':(exclude)docs'
```

命中 8 行，两个文件：

- `apps/api/service.py`：`:991`（`observe_recon_result`）、`:999` 与 `:1002`（`record_recon_failure`）；
- `apps/agents/compensation_worker.py`：`:183`（`observe_clock_skew`）、`:288`（backlog）、`:289`（overdue）、
  `:290`（future_retry）、`:291`（unresolved）。

其余写入方法在 `observability/`、`tests/`、`docs/` 之外零命中。
`/metrics` 的挂载点有两处：api/web 进程 `apps/main.py:35`，补偿执行器 `apps/agents/compensation_worker.py:302`。

> 注意：`apps/audit/monitor.py` 的 `set_audit_chain_broken()` 只在分支
> `feat/audit-chain-metric-wiring`（`4473014`）上，未合入 main，故不计入上面的 8 行。
> 即便合入，它写的是 scheduler 进程的内存，而 `/metrics` 只在 web 进程——见第 5 条判据。

> **更进一步：连那 2 条「生效」规则也没有评估器。** 2026-11-11 核实：`deploy/` 全目录无
> Prometheus / Alertmanager，无 `scrape_configs` / `rule_files`，compose 的 10 个服务里没有进程
> 读取 `alerts.rules.yaml`。它们的序列写得出、promtool 也能通过，但没有任何进程会评估规则，
> 因此故障发生时与注释掉的规则没有区别。这就是 §6.1.2 第 6 条的由来。

| 指标 | 生产采集点 | 是否仍被 `alerts.rules.yaml` 引用 | 取消注释的前置条件 |
|---|---|---|---|
| `smart_erp_recon_batches_total` | `apps/api/service.py:991` | 是，启用（SmartErpReconBatchFailureRateHigh；但部署内无评估器） | 采集点已满足；仍缺抓取与评估器（§6.1.2 第 6 条） |
| `smart_erp_recon_batch_duration_seconds` | `apps/api/service.py:991` | 是，启用（SmartErpReconBatchP95Slow；但部署内无评估器） | 同上 |
| `smart_erp_recon_supplier_lines_total` | `apps/api/service.py:991` | 否（有采集点、无规则） | — |
| `smart_erp_recon_batch_auto_match_ratio` | `apps/api/service.py:991` | 否（有采集点、无规则） | — |
| `smart_erp_recon_degraded_batches_total` | `apps/api/service.py:991` | 否（有采集点、无规则） | — |
| `smart_erp_recon_runs_failed_total` | `apps/api/service.py:999`、`:1002` | 否（建议规则已写、未启用） | 跑满一个结账周期拿到真实失败率基线后再定阈值（候选 5% / 15m） |
| `smart_erp_tickets_total` | 无 | 否（无规则） | 需要时先接 `record_ticket()` |
| `smart_erp_ticket_failures_total` | 无 | 否（无规则） | 同上 |
| `smart_erp_ticket_failures_unclassified_total` | 无 | 否（无规则） | 同上 |
| `smart_erp_write_stage_total` | 无 | **是 → 已注释**（SmartErpWriteFailureRateHigh） | 归属 `apps/write`：`WriteGateway.execute()` 按阶段埋 `record_stage()`，且 `/metrics` 上出现 `stage="write"` 的非零序列 |
| `smart_erp_write_stage_failures_total` | 无 | **是 → 已注释**（同上） | 同上 |
| `smart_erp_write_stage_duration_seconds` | 无 | 否（无规则） | 同 `record_stage()`；接线后可直接用于阶段 SLO |
| `smart_erp_unresolved_tickets` | `apps/agents/compensation_worker.py:291`（full 档；同进程 `:302` serve `/metrics`） | **是 → 已注释**（SmartErpUnresolvedTicketsGrowing） | ① 抓取目标 + `up`/`absent()` 守卫；② 规则用 `offset 30m`，需连续 30 分钟真实历史；③ 增长 5 / 底线 10 两个阈值待基线校准 |
| `smart_erp_audit_chain_broken` | `apps/audit/monitor.py:123`、`:144`（经 `core/runtime/scheduler.py::Scheduler.tick`；**仅在未合入的分支 `feat/audit-chain-metric-wiring` 上，main 无**） | **是 → 已注释（P0）**（SmartErpAuditChainBroken） | ① 该分支合入 main；② **scheduler 进程自己 serve `/metrics`**（端点当前只在 api/web `apps/main.py:35` 与补偿执行器 `apps/agents/compensation_worker.py:302`，scheduler 没有，内存里的 gauge 抓不到）；③ 完成一次人为断链的 **0→1 演练**：首次写入 0 之前序列是 `absent`，规则必须把它当**未知**、不能当 0（`max(...) > 0` 在 absent 时不触发，≠ 链完好） |
| `smart_erp_compensation_backlog` | `apps/agents/compensation_worker.py:288`（full 档；同进程 `:302` serve） | 否（规则已删：当时零采集点） | 采集点已具备，但恢复规则同样要先有抓取目标 + `up` 守卫 + 一个重试周期的积压基线 |
| `smart_erp_compensation_overdue` | `apps/agents/compensation_worker.py:289`（full 档；写入进程即 serve 进程：`:302 install_metrics(..., enabled=True)`） | 是 → **已注释**（3 条） | ① 抓取目标 `erp_compensation:8013` + `up`/`absent()` 守卫；② demo 档无该容器，须写明「不适用」而非「一直绿」；③ 跑满一个重试周期拿基线后再定 0 / 5 / 15m |
| `smart_erp_compensation_future_retry` | `apps/agents/compensation_worker.py:290`（full 档；同上，同进程 serve） | 是 → **已注释**（1 条） | ① 同上的抓取目标与守卫；② 基线应为 0，需实跑确认无假阳性；③ 执行器 `_METRIC_SQL` 用 `future=18.0`，与规则口径对齐 |
| `smart_erp_db_clock_skew_seconds` | `apps/agents/compensation_worker.py:183`（`ClockSampler.sample`，由 `:292` 每轮触发；full 档同进程 serve） | 是 → **已注释**（2 条） | ① 抓取目标 + `up` 守卫；② 5s / 30s 阈值无实测数据；③ 采样间隔随 `ERP_COMPENSATION_POLL_SECONDS`（默认 1s） |
| `smart_erp_db_clock_skew_backwards_seconds_max` | `apps/agents/compensation_worker.py:183`（同上一行） | 否（不单独告警） | 只用于看板与排查：进程生命周期内单调最大，不随恢复归零，不适合做告警 |

单独盯住的四类：

- **有采集点但规则已删**：`compensation_backlog`（`apps/agents/compensation_worker.py:288`）。
  它当时是按「零采集点」删的，现在采集点有了，但恢复规则仍需抓取目标与基线；其余无。
- **规则引用但无采集点、且仍在启用**：**无**。3 条历史规则（write 失败率 / unresolved / 审计链 P0）
  已与 6 条新规则同样处理——保留完整语义并注释掉，每条上方都有「采集点待接线（归属流：…）」。
  当前生效集合只剩 2 条对账规则，依赖的指标都有 `apps/api/service.py` 的真实采集点。
- **有写入点、但写入进程不 serve `/metrics`**：`smart_erp_audit_chain_broken`（1 条 P0）。
  这是最容易误判的一类：`git grep` 能搜到采集点、代码 review 看起来也接上了，但 scrape 永远抓不到。
  判据同 §6.1.2 第 5 条，只看「哪个进程写」与「那个进程是否暴露 `/metrics`」两件事。
- **有采集点但无任何规则**：`recon_supplier_lines_total`、`recon_batch_auto_match_ratio`、
  `recon_degraded_batches_total`、`recon_runs_failed_total`、`compensation_backlog`。

#### 补偿 5 条指标 / 6 条规则现在能不能取消注释？——不能（`0f597b3`，2026-11-11）

按 §6.1.2 第 5 条逐项核对：

| 判据 | 结论 | 证据 |
|---|---|---|
| 哪个进程写 | ✅ 补偿执行器 | `apps/agents/compensation_worker.py:288-292`（backlog / overdue / future_retry / unresolved / clock） |
| 该进程是否 serve `/metrics` | ✅ 是，写入与暴露是同一个进程 | `:295-302` `install_metrics(app, metrics, enabled=True)`，uvicorn 监听 `ERP_COMPENSATION_PORT`（默认 8013） |
| 默认开关与档位 | ❌ 档位不满足；**开关不是阻塞项** | 该调用显式 `enabled=True`，不受 `ERP_METRICS_ENABLED` 默认关闭影响；但容器只在 `profiles: ["full"]`，`demo` 档没有这个进程，端口也只 `expose: "8013"`，仓库内无抓取目标 |

结论：**仍不能取消注释**，阻塞点已从「没有端点」变成三条：
1. **没有抓取目标**：没有 scrape 就没有序列，`max(...) > 0` 不触发，看上去和「队列一直健康」完全一样；
2. **demo 档无执行器**：规则在 demo 部署上永远 absent，必须显式写「不适用」，不能用「一直绿」冒充；
3. **阈值无真实基线**：overdue 0 / 5、future_retry 0、skew 5s / 30s 都是分析值，full 档还没跑满一个重试周期。

解锁条件（三条全满足才动）：① 抓取目标指向 `erp_compensation:8013`，并有 `up` / `absent()` 守卫；
② 明确只对 full 档生效，demo 档标注不适用；③ 跑满一个重试周期、拿到真实基线后复核阈值。

`recon_runs_failed_total` 的建议规则（已写进规则文件、**未启用**）：
`sum(rate(smart_erp_recon_runs_failed_total[15m])) / clamp_min(sum(rate(smart_erp_recon_batches_total[15m])), 0.001) > 0.05`，
`for: 15m`，P1。依据：失败是 `stage=input/engine/persist` 的硬错误，比 `pending_manual` 这类业务分流更严重，
阈值可比「批次未自动完成率 2%」宽；分母用已处理批次数，避免夜间空窗把 1 次失败放大成高失败率。
**5% / 15m 只是候选**，必须等一个结账周期的真实基线再定，不凭猜启用。

> 结论：`/metrics` 已由 `apps/main.py:35` 与补偿执行器 `apps/agents/compensation_worker.py:302` 挂载
> （api/web 仍受 `ERP_METRICS_ENABLED` 控制），但只有对账链路与（full 档）补偿执行器真正在产出序列。
> 更关键的是：**部署内没有任何评估器**——没有抓取、没有 Prometheus / Alertmanager，
> 规则写了也不会被评估。所以「接线」需同时满足两件事：指标写入进程 === serve `/metrics` 的进程，
> 且存在抓取与规则评估进程；缺一即未接线。当前 2 条「生效」规则同样不产生告警。

### 6.1.4 补偿队列与时钟口径

**A 到期未领取**（抓 claim 停摆）：

```sql
SELECT count(*) FROM erp.compensation_task
WHERE state = 'queued'
  AND (
    (next_retry_at IS NOT NULL AND next_retry_at < now() - interval '60 seconds')
    OR (next_retry_at IS NULL
        AND coalesce(updated_at, created_at) < now() - interval '60 seconds')
  );
```

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

规则文件：[alerts.rules.yaml](./alerts.rules.yaml)（Prometheus rule file，`groups[0].rules` 共 12 条：生效 2、已注释 9、建议未启用 1）。

> **部署内没有评估器**：`deploy/` 全目录没有 Prometheus / Alertmanager，也没有 `scrape_configs` /
> `rule_files` 配置，compose 的 10 个服务没有任何进程读取本文件。下表「生效」只表示**未被注释、
> 且依赖指标有真实采集点**，不表示线上有告警——在评估器（含抓取配置）接入前，这 2 条与注释掉的
> 规则在故障当天没有区别。依据见 §6.1.2 第 6 条。

| 告警 | 状态 | 级别 | `for` | 触发口径 | 为什么值得半夜叫醒人 |
|---|---|---|---|---|---|
| `SmartErpReconBatchFailureRateHigh` | 生效·**无评估器** | P1 | 15m | 待人工 + 无法对账占比 > 2% | 对账是关账前置，夜间不修复会让人工队列指数级放大 |
| `SmartErpReconBatchP95Slow` | 生效·**无评估器** | P2 | 10m | 批次 P95 > 120s | 只影响速度不影响正确性，工作时间处理；与失败率告警同发时升级 P1 |
| `SmartErpWriteFailureRateHigh` | 已注释（待 apps/write） | P1 | 10m | `write` 阶段失败率 > 5% | 写入是唯一产生外部副作用的动作，失败即数据分叉，越晚核对范围越大 |
| `SmartErpUnresolvedTicketsGrowing` | 已注释（待抓取 + 30m 历史） | P1 | 10m | `max()` 后 30 分钟增量 > 5 且总量 > 10 | unresolved 只增不减 = 没人接住的失败在堆积，会变成对账差异与付款错误 |
| `SmartErpCompensationOverdueP2` | 已注释（full 档待抓取） | P2 | 5m | `overdue > 0` | 补偿到期未被领取，说明 claim 侧开始停摆 |
| `SmartErpCompensationOverdueP1Volume` | 已注释 | P1 | 5m | `overdue > 5` | 量级说明不是单条卡住，而是领取链路大面积不工作 |
| `SmartErpCompensationOverdueP1Duration` | 已注释 | P1 | 15m | `overdue > 0` 持续 15m | 持续即停摆：该撤未撤的数据留在对方系统，可能已被下游消费 |
| `SmartErpCompensationFutureRetry` | 已注释 | P1 | 1m | `future_retry > 0` | 唯一能抓到时钟回拨的信号：补偿被推到未来等于永久不执行 |
| `SmartErpClockSkewP2` | 已注释 | P2 | 1m | `|skew| ≥ 5s` | 根因信号，先于「任务卡住」出现，留出止损窗口 |
| `SmartErpClockSkewP1` | 已注释 | P1 | 1m | `|skew| ≥ 30s` | 回拨幅度已足以让补偿、超时与租约判断同时失真 |
| `SmartErpAuditChainBroken` | 已注释（P0，等分支合入 + 端点） | **P0** | 1m | `max(smart_erp_audit_chain_broken) > 0` | 审计链断链后「谁改了什么」无法自证，每小时都产生新的不可信记录 |
| `SmartErpReconRunFailureRateHigh` | 建议·未启用（等基线） | P1 | 15m | 对账硬失败率 > 5% | 硬错误比业务分流严重，但阈值需真实基线，不凭猜启用 |

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
  SUCCESS: 2 rules found
```

> **没有 docker / 拉不到镜像时，不得宣称规则已验证**。此时只能写“YAML 结构已校验，PromQL 未验证”，并把它列为发布阻塞项；配置错误会让 Prometheus 拒绝加载整个 rules 文件，而不是跳过单条。

> **这两层都只证明文件本身**。`SUCCESS: 2 rules found` 不等于「有 2 条告警在盯着线上」——
> 部署内没有 Prometheus / Alertmanager，也没有抓取配置，没有任何进程会评估它们。
> 详见 §6.1.2 第 6 条；评估器接入前，验收只能写「规则已定义、待评估器」。

P0 表达式另做真实指标快照求值（不依赖 docker，用于证明告警确实会触发）：

```text
audit_chain_broken=0  max()=0  firing=False
audit_chain_broken=1  max()=1  firing=True
```

`/metrics` 里出现 `smart_erp_audit_chain_broken 1` 后，`max(...) > 0` 立即成立，配合 `for: 1m` 即触发 P0。

---

## 8. SLO 草案

| SLO | 目标 | 指标口径（PromQL 形状） |
|---|---|---|
| SLO-1 工单成功率 | ≥ 99.5%（30 天滚动） | `1 - sum(rate(smart_erp_tickets_total{status="failed"}[30d])) / clamp_min(sum(rate(smart_erp_tickets_total{status="failed"}[30d])) + sum(rate(smart_erp_tickets_total{status="succeeded"}[30d])), 0.001)`；`rejected` 单列，不计入分母 |
| SLO-2 对账批次 P95 耗时 | ≤ 120s | `histogram_quantile(0.95, sum by (le) (rate(smart_erp_recon_batch_duration_seconds_bucket[30d])))` |
| SLO-3 审计链完整 | 100%，错误预算 0 | `avg_over_time(smart_erp_audit_chain_broken[30d]) == 0`；任一时刻为 1 即消耗全部预算 |

错误预算：SLO-1 按 30 天窗口允许 0.5% 失败；预算耗尽后暂停 W1/W2 自动写入、只保留预演。SLO-2 超阈值不阻断业务，先扩批次切分与并发；SLO-3 无预算，触发即按 P0 冻结写入并保全现场。

> 以上 SLO 都依赖查询/评估端。当前部署内没有 Prometheus，也没有抓取配置，所以这些 SLO
> **暂时不可计算**，不能当作「已达成的指标」引用（§6.1.2 第 6 条）。

---

## 9. 测试与验证

```powershell
.\.venv\Scripts\python.exe -m pytest tests/observability -q   # 81 条：约定 / 树 / 链路 / 成本 / 导出 / 指标 / 对账质量 / 告警 / 暴露面
.\.venv\Scripts\python.exe -m pytest tests/e2e -q             # 11 条：健康检查 / 只读 / W3 / 越权 / 审批 / 路由门禁
```

全量回归（本分支 rebase 到 `2dac800` 后实跑）：`tests=1219 failures=0 errors=0 skipped=78`（跳过均为未提供 `ERP_TEST_DATABASE_URL` 的 PG 用例：集成 52 / 权限 20 / 守卫 4 / 并发 2）。

`tests/observability/test_metrics.py` 覆盖：高基数标签拒绝、文本格式与累计桶、span→指标折算（成功 / F6 / rejected / unclassified）、业务指标、补偿到期/未来重试 gauge、时钟偏移的有符号值与进程内最大回拨、非法采样值拒绝、`/metrics` 的 200 与 OpenAPI 排除、非 GET 405、默认不挂载与 `ERP_METRICS_ENABLED` 开关。
`tests/observability/test_recon_quality.py` 覆盖：词表封闭且无 accuracy 指标、观测层不 import `apps.recon`（AST 断言）、只统计供应商侧行、`unaccounted = supplier_lines − 各行之和 == 0` 的自检、`BatchStatus→result` 映射与未知状态报错、降级原因取缺失项而非自由文本、直方图桶边界（`le` 为小于等于）、四条标签红线。
`tests/observability/test_alerts.py` 覆盖：YAML 解析、生效规则恰为 2 条且都引用有采集点指标、`why_paged`/`runbook` 完整；9 条被注释规则必须带「采集点待接线（归属流：…）」或「待真实基线」标记（不允许无理由静默停用）、补偿/时钟 6 条的 severity/`for`/表达式原样保留、P0 审计链以注释形态保留 `for: 1m`、已删除的 `SmartErpCompensationBacklogHigh` 不再被引用；以及 overdue（P2/P1 量级/P1 时长）、future_retry、clock_skew（P2/P1 分档）在真实 `/metrics` 文本上的触发演示。

### 9.1 发布前检查（全部通过才可上线）

1. `pytest tests/observability tests/e2e` 全绿；
2. 规则文件必须跑真实 PromQL 校验：

```powershell
docker run --rm --entrypoint promtool `
  -v "E:\tools\Factroy_Agents\smart-ERP:/rules" `
  prom/prometheus:latest check rules /rules/observability/alerts.rules.yaml
```

必须看到 `SUCCESS: 2 rules found`（当前 12 条里：2 条生效、9 条因「采集点待接线」注释、1 条建议规则待基线，见 §6.1.2/§6.1.3）。**没有 docker / 拉不到镜像时，不得宣称规则已验证**：只能标注“YAML 结构已校验，PromQL 未验证”，并按发布阻塞项处理。注意这只证明语法：评估器（Prometheus + 抓取）未接入前，`SUCCESS` 也不代表线上有告警。

3. 确认 `ERP_METRICS_ENABLED` 目标状态：生产如未接入内网抓取就保持关闭；开启时确认 nginx 已 `deny /erp/api/metrics`、且未设置 `ERP_API_BIND=0.0.0.0`（§6.0）。

E2E 说明：健康检查直接打真实的 `apps.main`；只读接口与写入接口是测试专用 `/_e2e/*` 路由，内部调用真实的 `AccessGate`（四道闸）与 `WriteGateway`（唯一写出口），连接器为进程内替身，不启动 PG / 网络。

**测试路由收口**：`apps/main.py` 不含任何 `/_e2e` 路由，也不 import 测试模块；路由只存在于 `tests/e2e/e2e_support.py`，且仅当 `ERP_APP_ENV=test` 时挂到测试进程内新建的 app 实例上。默认、`prod`、`staging`、`dev` 等配置下路径**不存在**（`GET /_e2e/...` 返回 404），也不会出现在 `GET /openapi.json` 的 paths 中。C12 镜像级全链路用例待 S8 镜像产物就绪后补。

---

## 10. 边界

- 只读观测，不改变 `WriteGateway`、状态机、审计的任何行为；异常原样抛出。
- W3 没有执行路径；`rejected` span 只证明「拒绝发生了」，不产生任何写入。
- 不记录凭据明文：含 `credential/password/secret/authorization` 的键写为 `[REDACTED]`。
- 不存全量输入；模型调用只记 `model`、`prompt_version`、实测 token / 成本（与 `agent_decision` 的「存输出不存全量输入」一致）。
- `/metrics` 默认不挂载，必须由接入方用 `install_metrics()` 并显式开启 `ERP_METRICS_ENABLED`；该端点只允许内网访问，不得随公网端口发布。指标标签不含任何工单号 / 幂等键 / 外部单号，高基数细节只留在 trace。
