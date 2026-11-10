# 阻断豁免：有期限、可审计、不可覆盖结构性问题

S9 主数据体检第四轮能力，仍为**只读**：豁免只改变本次报告里差异的严重度（阻断→提示），
不修改快照、不写回 ERP、不新增 API 路由。

豁免不是放宽规则。规则仍然照常命中、差异照常出清单；豁免只是对「已人工核实无误」的
个别主体 + 个别规则开一个有到期日的口子，到期自动恢复阻断。

## 1. 可审计字段

一条豁免（`Exemption` / 报告里的 `AppliedExemption`）必须写全下列字段：

| 字段 | 含义 | 约束 |
| --- | --- | --- |
| `exemption_id` | 豁免编号 | 全局唯一；重复直接报错 |
| `subject_type` | 主体类型 | 只允许 `供应商` / `客户` / `物料` |
| `subject_code` | 主体编码 | 必须与快照编码一致；禁止 `*`/`ALL`/`全部` 等通配 |
| `rule_id` | 豁免哪条规则 | 必须在规则目录中，且不在「不允许豁免」清单 |
| `submitted_by` | 谁提单 | 对账运营人员，必填 |
| `submitted_role` | 提单角色 | 只允许 `对账运营` |
| `submitted_at` | 提单日 | `YYYY-MM-DD`，不得晚于生效日 |
| `approved_by` | 谁批准 | 必填，且不得与提单人同一人 |
| `approved_role` | 批准角色 | 只允许 `财务` / `内控`；`商务`/`销售`/`客户成功` 直接拒绝 |
| `basis.type` | 依据类型 | 必须是 `客户书面确认` |
| `basis.ref` | 依据编号 | 客户确认函/邮件归档编号，必填；**全局唯一，不得跨豁免复用** |
| `basis.valid_from` | 确认函有效期起 | `YYYY-MM-DD`，必填；不得晚于豁免生效日 |
| `basis.valid_to` | 确认函有效期止 | `YYYY-MM-DD`，必填；不得早于到期日 |
| `reason` | 豁免原因 | 说明为什么核实为无误，必填 |
| `granted_at` | 生效日 | `YYYY-MM-DD` |
| `expires_at` | 到期日 | `YYYY-MM-DD`，不得早于生效日 |
| `second_reviewer` | 二次复核人 | 单批豁免影响行占比 > 15% 时必填 |
| `second_reviewed_role` | 二次复核角色 | 只允许 `财务` / `内控`，且独立于提单/批准人 |
| `escalated_review_by` | 升级复核人 | 同一主体同规则累计第 4 次起必填，独立于前三类角色 |
| `cumulative_count` | 累计豁免次数 | 只读，由同主体同规则的历史记录自动计算 |

报告额外记录 `findings` 与 `findings_count`：本次被这条豁免实际降级的差异，
形如 `["单位换算:IT-1004"]`。

## 2. 角色分离：批准人不能是计费受益角色

豁免会让更多行变成自动放行、也就是更多计费行。如果批准人同时能从计费增长中获益，
豁免就会从「纠错」变成「增收工具」。因此：

- **提单**：对账运营（`submitted_role = 对账运营`）；
- **批准/复核**：财务或内控（`approved_role ∈ {财务, 内控}`）；
- **禁止**：商务、销售、客户成功作为 `approved_by`，加载时直接抛
  `ExemptionInvalid`；
- **客户书面确认**是唯一允许的依据类型，不能用内部工单或口头说明代替。

## 3. 到期自动失效与确认函边界

- 生效条件：`granted_at <= 评估日 <= expires_at`；
- 过期（`评估日 > expires_at`）与未到期（`评估日 < granted_at`）都不生效，**无需人工清理**；
- 同一主体 + 同一规则不允许有效期重叠（防止「以新盖旧」无限续期）；
  相邻窗口（上一条到期日 = 下一条生效日）允许。

**客户确认函边界**（防止一份函被无限复用）：

- `basis.ref` 全局唯一：同一份确认函编号只能出现在一条豁免里，重复即
  `ExemptionInvalid`（「同一确认函不得跨豁免复用」），不能用一份函支撑十条豁免；
- 确认函自身有有效期 `basis.valid_from` / `basis.valid_to`，且必须
  `valid_from <= granted_at`、`expires_at <= valid_to`；豁免期只要越出确认函有效期，
  加载即拒绝（「越界豁免一律拒绝」）；
- `basis.valid_from` 晚于 `basis.valid_to` 同样直接拒绝。

CLI 用 `--as-of YYYY-MM-DD` 指定评估日，默认今天。

## 4. 上限、告警与拒绝条件

| 条件 | 行为 |
| --- | --- |
| 主体编码为 `*`/`ALL`/`全部` | 加载即拒绝：`ExemptionInvalid`（整批/通配） |
| 同一 `basis.ref` 出现在多条豁免 | 加载即拒绝：`ExemptionInvalid`（不得跨豁免复用） |
| 豁免期超出 `basis.valid_from`~`basis.valid_to` | 加载即拒绝：`ExemptionInvalid`（越界） |
| 对账行缺少 `billable` 标注 | 读取行级数据即抛 `TypeError`：计费行默认不计费，必须显式标注 |
| `billable=false` 的行又被 `auto_released` | 输出 `BILLABLE_FLAG_CONFLICT` 告警：自相矛盾，不得静默取任一边 |
| 豁免覆盖本期全部行（整批豁免） | 应用即拒绝：`ExemptionPolicyViolation` |
| 单批豁免影响行占比 > **15%** 且缺少 `second_reviewer` | 应用即拒绝：`ExemptionPolicyViolation`；补齐后放行并输出 `EXEMPTION_LINE_RATIO_EXCEEDED` 告警 |
| 同一主体同规则累计次数 > **3** 且缺少 `escalated_review_by` | 加载即拒绝：`ExemptionInvalid`；补齐后输出 `EXEMPTION_RENEWAL_EXCEEDED` 告警 |
| 未提供行级数据（`--lines`） | **不静默跳过**：报告 `exemption_cap_checked=false`，输出 `EXEMPTION_CAP_UNCHECKED` 告警与「未提供行级数据 → 豁免影响面上限未校验」 |

「整批豁免」与降级清单里的 `downgrade_all` 是相反方向的两个极端：一个把整批强制转人工，
一个把整批自动放行，都属于不可接受的默认值。

## 5. 生效规则：全部主体都豁免才降级

一条差异可能涉及多个主体（例如单据同时挂供应商和物料）。只有**全部**主体都有同规则
生效豁免时，这条差异才从阻断降为提示；任一主体未豁免则保持阻断，避免
「一个主体被豁免、连带放过另一个主体」。

无法归属主体的差异（例如部分期间级问题）不会被任何豁免降级，保持阻断。

## 6. 配置格式（schema 2.0）

```json
{
  "schema_version": "2.0",
  "kind": "masterdata_exemptions",
  "source": "fixtures/masterdata/exemptions_sample.json",
  "entries": [
    {
      "exemption_id": "EX-2026-001",
      "subject_type": "物料",
      "subject_code": "IT-1004",
      "rule_id": "UOM-003",
      "submitted_by": "对账运营-李示例",
      "submitted_role": "对账运营",
      "submitted_at": "2026-09-28",
      "approved_by": "财务经理-张示例",
      "approved_role": "财务",
      "basis": {
        "type": "客户书面确认",
        "ref": "客户确认函-2026-0912-包装规格",
        "valid_from": "2026-09-12",
        "valid_to": "2027-01-31"
      },
      "reason": "包装规格已核实为 1000 PCS/BOX，偏离同类属正常业务口径",
      "granted_at": "2026-10-01",
      "expires_at": "2026-12-31"
    }
  ]
}
```

`schema_version` 或 `kind` 不匹配、字段缺失、角色不符、依据不是客户书面确认、
规则不存在或不允许豁免、日期非法、有效期重叠、累计超 3 次未升级复核，一律抛
`ExemptionInvalid`；整批/超 15% 未二次复核抛 `ExemptionPolicyViolation`。

## 7. 迁移：schema 1.0 → 2.0

1.0 没有角色与客户确认字段，**不能自动猜**。检测到 1.0 时错误信息会直接给出命令：

```bash
python -m apps.masterdata --migrate-exemptions old.json \
  --roles roles.json --out new.json
```

`roles.json` 必须按 `exemption_id` 手工补齐：

```json
{
  "EX-2026-001": {
    "submitted_by": "对账运营-李示例",
    "submitted_role": "对账运营",
    "submitted_at": "2026-09-28",
    "approved_role": "财务",
    "basis_ref": "客户确认函-2026-0912",
    "basis_valid_from": "2026-09-12",
    "basis_valid_to": "2026-12-31",
    "second_reviewer": "",
    "second_reviewed_role": "",
    "escalated_review_by": ""
  }
}
```

函数入口 `migrate_v1_to_v2(raw, mappings)`；缺少任一映射直接拒绝，迁移结果仍会过
`parse_exemptions` 校验。

## 8. 计量就绪契约（API / 计量表）

本模块不直接改计量表；它输出「计量就绪 JSON + CSV」，API 可以按下列字段直接消费。
每条豁免对应一条记录：

| 字段 | 说明 |
| --- | --- |
| `exemption_id` | 豁免编号 |
| `subject_type` | `供应商` / `客户` / `物料` |
| `subject_code` | 主体编码 |
| `rule_id` | 被豁免的规则 |
| `approved_by` | 批准人 |
| `approved_by_role` | 批准角色（财务/内控） |
| `basis_ref` / `basis_valid_from` / `basis_valid_to` | 客户确认函编号与有效期 |
| `findings_count` | 本次降级的差异条数 |
| `affected_billable_lines` | 豁免放过的计费行数 |
| `billable_conflict_lines` | 标记不计费却被放过的行数（需人工核对） |
| `covered_lines` / `freed_lines` | 覆盖行数 / 实际放过行数 |
| `cumulative_count` | 同主体同规则累计豁免次数 |
| `second_reviewer` / `escalated_review_by` | 复核留痕 |

JSON 顶层还给出 `total_lines`、`covered_lines`、`freed_lines`、
`affected_billable_lines`、`billable_conflict_lines`、`billable_conflict_line_ids`、
`covered_ratio_pct`、`threshold_pct`、`threshold_exceeded`、`exemption_cap_checked`、
`exemption_cap_note` 与 `alerts[]`。

CSV 列：`豁免编号,主体类型,主体编码,规则ID,提单人,批准角色,批准人,依据类型,依据编号,
生效日,到期日,累计次数,命中差异数,覆盖行数,放过行数,影响计费行数,计费冲突行数,二次复核人,
升级复核人,影响面上限已校验`。

**计费行口径：默认不计费，必须显式标注。** `ReconLine.billable` 没有默认值，调用方漏传
直接 `TypeError`，`recon_lines_from_dict` 缺 `billable` 或非布尔值同样报错。少算一行是
少收钱、可以补；多算一行是客户发现我们多收钱，会伤信任并触发争议流程。因此宁可报错，
也绝不默认计入。

**计费标记冲突：**若某行 `billable=false` 却被豁免自动放过（`auto_released`），属于
自相矛盾状态，输出 `BILLABLE_FLAG_CONFLICT` 告警并列出该行 ID，不允许静默按任一边处理。

**关键约定**：未提供行级数据时，`exemption_cap_checked=false`，并在汇总行写
「未校验」，API/计量表必须显式展示，不得把 0 当作「没有影响」。

## 9. CLI 用法

```bash
python -m apps.masterdata -i fixtures/masterdata/dirty.json \
  --lines fixtures/masterdata/recon_lines_sample.json \
  --exemptions fixtures/masterdata/exemptions_sample.json \
  --as-of 2026-11-11 \
  --exemption-csv dist/masterdata_exemption_impact.csv

python -m apps.masterdata -i fixtures/masterdata/dirty.json \
  --exemptions fixtures/masterdata/exemptions_sample.json --format json
```

样例效果（`dirty.json` + `exemptions_sample.json`，评估日 2026-11-11）：

- 阻断差异 21 → 20，豁免覆盖 1 条（UOM-003 / IT-1004 降为提示）；
- 降级行 4/10（40%）→ 3/10（30%），默认 30% 阈值下告警解除；
- 豁免放过 1 行、其中计费行 1 行；影响行占比 10%，未触发 15% 二次复核；
- `EX-2026-000` 已于 2026-09-30 到期，只出现在「已过期」审计里，不影响本次结果。

累计第 4 次样例（`exemptions_renewal_sample.json`）会输出：
`EX-2026-104 ... 累计 4 次` 与 `[告警] EXEMPTION_RENEWAL_EXCEEDED`。

## 10. API 侧接入约定

- **入口**：`load_exemptions(path)`（或 `parse_exemptions(raw)`），不要自己读 JSON 拼字段；
- **顺序**：`inspect()` → `apply_exemptions(..., lines=本期行)` →
  `evaluate_exemption_impact()` → `evaluate_block_impact()` / 导出降级清单；
- **时点**：每批对账开始前加载一次，用批次日期判断生效；
- **失败策略：不应用任何豁免（阻断照常）**。配置读不到、损坏、schema 不兼容或策略校验
  失败时，绝不把豁免当作默认放行；这是比放宽更安全的一侧；
- **不缓存过期结果**：下一批重新加载，到期豁免自动从生效集合消失；
- **没有行级数据时**：`exemption_cap_checked=false`，API 必须把
  「豁免影响面上限未校验」传给计量表，不能静默按 0 处理；
- **与降级清单的关系**：先应用豁免，再导出/读取降级清单；被豁免的主体不会出现在
  降级清单的 `entries` 里。
