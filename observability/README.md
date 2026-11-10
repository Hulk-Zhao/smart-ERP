# 观测层（S7）｜埋点约定、trace 还原与成本归因

面向排障与验收的最小观测实现：**一个工单 = 一条 trace**，能看到「决策 → 预演 → 写入 → 校验 → 补偿」五段，能按工单 / Agent / 步骤算调用次数与耗时。

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

## 6. 指标口径基础

NFR-O-01 需要的维度已由 span 属性覆盖：`ticket_id / agent / step_no / level / system / action_key / status / duration_ms`。

- Agent 成功率：`status != error` 的 `ticket.step` 占比（`rejected` 单独统计，避免把策略拒绝算成故障）；
- 时延：`gateway.write` / `gateway.verify` 的 `duration_ms` 分布；
- 越权与 W3：`status=rejected` + `reject_code` 计数（P0 告警输入）；
- 连接器健康度、限流余量、熔断次数、队列积压：由各连接器 / worker 的端点与行数据提供，本包只提供埋点位。

> 本里程碑未引入 OpenTelemetry SDK 与 `/metrics` 端口（避免新增第三方依赖、避免外网组件）。当前是**同语义的进程内 span 模型 + 本地 JSONL**；接 Prometheus / OTel Collector 时以本约定为唯一字段口径，不需要改业务埋点。

---

## 7. 测试与验证

```powershell
.\.venv\Scripts\python.exe -m pytest tests/observability -q   # 18 条：约定 / 树 / 链路 / 成本 / 导出
.\.venv\Scripts\python.exe -m pytest tests/e2e -q             # 11 条：健康检查 / 只读 / W3 / 越权 / 审批 / 路由门禁
```

E2E 说明：健康检查直接打真实的 `apps.main`；只读接口与写入接口是测试专用 `/_e2e/*` 路由，内部调用真实的 `AccessGate`（四道闸）与 `WriteGateway`（唯一写出口），连接器为进程内替身，不启动 PG / 网络。

**测试路由收口**：`apps/main.py` 不含任何 `/_e2e` 路由，也不 import 测试模块；路由只存在于 `tests/e2e/e2e_support.py`，且仅当 `ERP_APP_ENV=test` 时挂到测试进程内新建的 app 实例上。默认、`prod`、`staging`、`dev` 等配置下路径**不存在**（`GET /_e2e/...` 返回 404），也不会出现在 `GET /openapi.json` 的 paths 中。C12 镜像级全链路用例待 S8 镜像产物就绪后补。

---

## 8. 边界

- 只读观测，不改变 `WriteGateway`、状态机、审计的任何行为；异常原样抛出。
- W3 没有执行路径；`rejected` span 只证明「拒绝发生了」，不产生任何写入。
- 不记录凭据明文：含 `credential/password/secret/authorization` 的键写为 `[REDACTED]`。
- 不存全量输入；模型调用只记 `model`、`prompt_version`、实测 token / 成本（与 `agent_decision` 的「存输出不存全量输入」一致）。
