# contracts/ —— 冻结契约件

本目录是**只增不改**的契约真源：代码（Pydantic / 常量）是唯一来源，JSON 由代码生成，
不允许手改。改契约的流程固定为：改代码 → 重新生成 → 在 PR 里说明版本与破坏性。

## 生成与校验

```bash
python -m apps.platform.contracts.export_contracts --write   # 生成/更新
python -m apps.platform.contracts.export_contracts --check   # CI 校验是否漂移
```

`tests/platform/test_contracts_frozen.py` 会对 6 份文件做同样的等值断言。

## 清单（v1.0）

| 文件 | 契约 | 负责任务 | 说明 |
|---|---|---|---|
| `agent/ag1.reconcile.input.v1.json` | C7 | T004 | AG-1 入参：`scope` 必填、`additionalProperties: false` |
| `agent/ag1.reconcile.output.v1.json` | C7 | T004 | AG-1 出参：`ticket_id` / `status` / `evidence[]` 必填，金额对象化 |
| `policy/write_levels.v1.json` | C4 | T003 | W0–W3 语义、四路取严口径、禁止动词、只读关键字段 |
| `connector/action_spec.v1.json` | C2 | T003 | ActionSpec 字段与注册期拒绝规则 |
| `ticket/state_machine.v1.json` | C3 | T006 | 12 状态、迁移白名单、步骤状态 |
| `errors/codes.v1.json` | C10 | T004 | 错误码 → HTTP / F 码 / 是否可重试 / 下一步 |

## 约定

- `$id` 形式：`https://contracts/<domain>/<name>/<version>`；
- 版本号只在**破坏性变更**时递增（新增可选字段不算破坏性）；
- `ticket/state_machine.v1.json` 同时被 `db/migrations/versions/0001_s0_baseline.py`
  用来生成 `ticket_event` 的迁移白名单触发器，改状态机必须重新生成迁移或追加新迁移；
- 金额字段一律 `string`（`numeric(18,4)` 的十进制文本），禁止 `number`，避免浮点误差。
