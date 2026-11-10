# 观测层（S7）｜埋点约定、trace 还原与成本归因

面向排障与验收的最小观测实现：**一个工单 = 一条 trace**，能看到「决策 → 预演 → 写入 → 校验 → 补偿」五段，能按工单 / Agent / 步骤算调用次数与耗时；同时把 trace 折算成 Prometheus 指标、配套告警规则与 SLO 草案，供监控系统消费。

不接 SaaS APM、不启动外部服务、不引第三方依赖（标准库实现）。导出只写本地 JSON Lines。

---

## 1. 怎么开

| 环境变量 | 默认 | 说明 |
|---|---|---|
| `ERP_TRACE_ENABLED` | 关 | `1/true/yes/on` 落盘（大小写与首尾空白不敏感）；`0/false/no/off` 或空值关闭；**其他取值启动即报错**；关闭时 span 仍保留在内存，便于测试与排障 |
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
| `ERP_METRICS_ENABLED` | 关 | `1/true/yes/on` 才挂载（大小写与首尾空白不敏感）；`0/false/no/off` 或空值关闭；**其他取值启动即报错**。与 `ERP_TRACE_ENABLED` 同一实现，生产默认不暴露 |
| `ERP_METRICS_PATH` | `/metrics` | 端点路径；必须先由反向代理/网络策略限定为内网 |

两个开关都由 `parse_env_flag()`（`observability/metrics.py`）解析，语义不再各写一套。
它**只许在装配期调用**：`exporter_from_env()` 在构造 `Tracer` 时执行，`install_metrics()`
在注册路由时执行，两者都不在请求路径上。非法取值让进程启动失败是刻意的——拼错
（`enabled`、`ture`）被静默当成关闭，就是「以为开了、其实没开」的生产事故；而放到
请求路径上读，非法配置会变成每个请求 500。

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

| 指标 | 类型 | 标签 | 多副本聚合 | 口径 |
|---|---|---|---|---|
| `smart_erp_tickets_total` | counter | `status`、`agent` | `sum()` / `sum by (status,agent)` | 工单终态计数；`status ∈ succeeded/failed/rejected`，`rejected` 是策略拒绝，独立于技术失败 |
| `smart_erp_ticket_failures_total` | counter | `failure_class`、`agent` | `sum()` / `sum by (failure_class)` | 失败工单按 F1–F8 计数（口径见 PRD §5.0） |
| `smart_erp_ticket_failures_unclassified_total` | counter | `agent` | `sum()` | 失败但未给 F 码的工单；这是**口径缺口**，应长期为 0 |
| `smart_erp_write_stage_total` | counter | `stage` | `sum()` | `WriteGateway` 阶段执行次数，`stage ∈ preview/write/verify/compensate` |
| `smart_erp_write_stage_failures_total` | counter | `stage` | `sum()` | 阶段失败次数（`error` 或 `rejected`），与上者相除即失败率 |
| `smart_erp_write_stage_duration_seconds` | histogram | `stage` | 先 `sum by (le)` 再 `histogram_quantile` | 阶段耗时（秒），默认桶 5ms–10s |
| `smart_erp_recon_batches_total` | counter | `result` | `sum()` / `sum by (result)` | 对账批次结果：`matched`（已对账）/`pending_manual`（待人工）/`unreconcilable`（无法对账） |
| `smart_erp_recon_batch_duration_seconds` | histogram | — | 先 `sum by (le)` 再 `histogram_quantile` | 批次耗时（秒），默认桶 1s–600s |
| `smart_erp_unresolved_tickets` | gauge | — | 全局口径 `max()`；分片队列 `sum()` | 当前 unresolved 工单数 |
| `smart_erp_compensation_backlog` | gauge | — | `sum()` | 当前待处理补偿任务数（各副本各自的补偿队列相加） |
| `smart_erp_audit_chain_broken` | gauge | — | `max()` | 审计链断链标志：0 正常 / 1 断链（P0） |

> **多副本必须聚合**：以上指标都是**进程内累加**，N 个副本就产生 N 条同标签序列。只看单个 pod 的数字会低估全局量；Prometheus 查询必须按上表聚合。规则文件已按聚合口径编写（`sum(...)`、`sum by (le)`、`max(...)`）。

span → 指标的折算规则：包含任一 `rejected` span 的 trace 计为 `rejected`，否则有 `error` 计为 `failed` 并取第一个 `failure_class`/`fail_code`，都没有则进 `unclassified`；`gateway.preview|write|verify|compensate` 分别累加到对应 `stage`。

### 6.2 标签克制（硬约束）

- `ticket_id`、`idem_key`、`external_id`、`trace_id`、`span_id`、`batch_id`、`doc_no` 以及任何 `*_id` 后缀**禁止**作为标签，传入即抛 `MetricLabelError`；
- 未声明或缺失的标签同样报错，避免悄悄产生新序列；
- 工单级细节留在 trace / JSONL 里按需检索，时序库只保留可聚合的低基数维度。

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
smart_erp_unresolved_tickets 7
smart_erp_compensation_backlog 3
smart_erp_audit_chain_broken 0
```

---

## 7. 告警规则

规则文件：[alerts.rules.yaml](./alerts.rules.yaml)（Prometheus rule file，`groups[0].rules` 共 6 条）。

| 告警 | 级别 | `for` | 触发口径 | 为什么值得半夜叫醒人 |
|---|---|---|---|---|
| `SmartErpWriteFailureRateHigh` | P1 | 10m | `write` 阶段失败率 > 5% | 写入是唯一产生外部副作用的动作，失败即数据分叉，越晚核对范围越大 |
| `SmartErpUnresolvedTicketsGrowing` | P1 | 10m | `max()` 后 30 分钟增量 > 5 且总量 > 10 | unresolved 只增不减 = 没人接住的失败在堆积，会变成对账差异与付款错误 |
| `SmartErpCompensationBacklogHigh` | P1 | 10m | 10 分钟内积压 > 20 | 补偿积压意味着外部系统保留着该撤未撤的数据，可能已被下游消费 |
| `SmartErpReconBatchFailureRateHigh` | P1 | 15m | 待人工 + 无法对账占比 > 2% | 对账是关账前置，夜间不修复会让人工队列指数级放大 |
| `SmartErpAuditChainBroken` | **P0** | 1m | `max(smart_erp_audit_chain_broken) > 0` | 审计链断链后「谁改了什么」无法自证，每小时都产生新的不可信记录 |
| `SmartErpReconBatchP95Slow` | P2 | 10m | 批次 P95 > 120s | 只影响速度不影响正确性，工作时间处理；与失败率告警同发时升级 P1 |

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
  SUCCESS: 6 rules found
```

> **没有 docker / 拉不到镜像时，不得宣称规则已验证**。此时只能写“YAML 结构已校验，PromQL 未验证”，并把它列为发布阻塞项；配置错误会让 Prometheus 拒绝加载整个 rules 文件，而不是跳过单条。

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

---

## 9. 测试与验证

```powershell
.\.venv\Scripts\python.exe -m pytest tests/observability -q   # 52 条：约定 / 树 / 链路 / 成本 / 导出 / 指标 / 告警 / 暴露面
.\.venv\Scripts\python.exe -m pytest tests/e2e -q             # 11 条：健康检查 / 只读 / W3 / 越权 / 审批 / 路由门禁
```

全量回归（本次）：`tests=637 failures=0 errors=0 skipped=25`（跳过均为未提供 `ERP_TEST_DATABASE_URL` 的 PG 用例）。

`tests/observability/test_metrics.py` 覆盖：高基数标签拒绝、文本格式与累计桶、span→指标折算（成功 / F6 / rejected / unclassified）、业务指标、`/metrics` 的 200 与 OpenAPI 排除、非 GET 405、默认不挂载与 `ERP_METRICS_ENABLED` 开关、开关 fail-fast（大小写/首尾空白接受，非法值抛 `MetricError` 且不挂路由）。
`tests/observability/test_alerts.py` 覆盖：YAML 解析、规则数量与 `why_paged`、P0 审计断链的 `for: 1m`、表达式引用的指标全部存在、健康/断链两种快照下的触发求值。

### 9.1 发布前检查（全部通过才可上线）

1. `pytest tests/observability tests/e2e` 全绿；
2. 规则文件必须跑真实 PromQL 校验：

```powershell
docker run --rm --entrypoint promtool `
  -v "E:\tools\Factroy_Agents\smart-ERP:/rules" `
  prom/prometheus:latest check rules /rules/observability/alerts.rules.yaml
```

必须看到 `SUCCESS: 6 rules found`。**没有 docker / 拉不到镜像时，不得宣称规则已验证**：只能标注“YAML 结构已校验，PromQL 未验证”，并按发布阻塞项处理。

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
