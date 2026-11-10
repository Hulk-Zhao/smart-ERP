# 降级清单：对外导出与 API 接入说明

本文件描述 S9 主数据体检第三轮产出的两个只读能力：**阻断影响面评估**与**降级清单导出**。
本轮只定义与导出，**没有**接进 API、没有新增路由、没有写回。

## 1. 阻断语义

「阻断」= 该主体涉及的对账行强制降级为待人工。数据照常加载、差异照常出清单，
不拒绝加载，也不拒绝出清单。常量定义在 `apps/masterdata/models.py` 的 `BLOCK_SEMANTICS`，
报告、CLI、降级清单引用同一份文案。

## 2. 阻断影响面评估

函数：

```python
from apps.masterdata import evaluate_block_impact, load_recon_lines

lines = load_recon_lines("fixtures/masterdata/recon_lines_sample.json")
impact = evaluate_block_impact(report, lines, snapshot=snapshot)
```

- `report`：`inspect()` 的返回结果（`InspectionReport`）。
- `lines`：行级只读视图 `ReconLine`，字段 `line_id`、`supplier_code`、`customer_code`、
  `item_code`、`period`、`billable`。JSON 形态为 `{"lines": [...]}` 或裸数组；只要给行级数据就能算，
  不依赖数据库。
- **`billable` 必须显式标注，默认不计费。** `ReconLine.billable` 是无默认值的关键字参数，
  调用方漏传直接 `TypeError`；JSON 输入缺 `billable` 或值不是布尔时，
  `recon_lines_from_dict` / `load_recon_lines` 同样报错。少算一行是少收钱、可以补；
  多算一行是客户发现我们多收钱，会伤信任并触发争议流程，所以宁可报错也不默认计入。
- `snapshot`：传入时，税码类阻断会反查到引用该税码的供应商/客户/物料；
  不传也能算，但税码类阻断会进入 `unattributed_rule_ids` 并把 `downgrade_all` 置为 `True`。
- `threshold`：降级行占比阈值，缺省取 `InspectionOptions.block_line_ratio_threshold`
  （默认 `0.30`，CLI 可用 `--block-threshold 0.25` 或 `--block-threshold 25%` 覆盖）。

口径：

- 只统计本期行：`line.period` 为空视为本期，有值则必须等于 `report.as_of_period`；
  其余计入 `out_of_period_lines`，不参与占比。
- 主体聚合：供应商 / 客户 / 物料。单据类阻断按单据上的供应商、物料分别归属；
  单位换算类按差异携带的 `item_code` 归属；税码类按快照反查引用方。
- 无法归属主体的阻断（例如期间级）按 fail-closed 保守处理：`global_downgrade=True`，
  整批行都算降级，并输出 `UNATTRIBUTED_BLOCK` 告警。
- 超过阈值输出 `BLOCK_LINE_RATIO_EXCEEDED` 告警，结论句形如
  「当前规则下本期 40% 的行会被降级（4/10 行）」。

CLI：

```bash
python -m apps.masterdata -i fixtures/masterdata/dirty.json \
  --lines fixtures/masterdata/recon_lines_sample.json \
  --block-threshold 30%
```

## 3. 阻断命中率（单独指标）

定义在 `InspectionReport.metrics()`，随 `to_dict()["metrics"]` 输出：

- `block_hit_rate` = 阻断差异数 / 差异总数（按条数，不按行数），如 `"0.525"`；
- `block_hit_rate_pct`，如 `"52.5%"`；
- `blocked_rules` / `blocked_rule_count`：哪些规则贡献了阻断；
- `rules_hit` / `total_rules` / `rules_hit_rate`：规则覆盖情况。

它是判断阻断规则是否定得过严的**唯一依据**。降级行占比（`affected_ratio`）回答的是
「这一期会被吃掉多少行」，两者不能互相替代。

### 3.1 「40%」这个数字的来源与量级

`fixtures/masterdata/dirty.json` 是 **24/24 条规则全命中的合成最坏情形**，
`recon_lines_sample.json` 只有 10 行，4 行命中阻断主体，因此得出 `40%`。
它**不是真实客户样本**，不能直接当作客户的降级比例对外引用。

为了让读者知道量级怎么随数据质量变化，报告同时给出三档合成模拟
（`apps/masterdata/simulate.py`，200 供应商 / 120 客户 / 300 物料 / 1000 行）：

| 档位 | 注入缺陷率 | 降级行占比区间 |
| --- | --- | --- |
| 轻脏 | 2% | 4.6% – 7.9% |
| 中脏 | 8% | 18.6% – 27.6% |
| 重脏 | 20% | 40.7% – 60% |

区间下限假设对账行在主体间均匀分布；上限假设脏主体的行量是均值的 1.5 倍
（脏数据通常集中在少数高频主体上）。`dirty.json` 的 40% 落在「重脏」区间内，
说明它代表的是最坏情形而不是常态。

CLI 文本输出固定打印 `SAMPLE_CAVEAT` 与三档区间；JSON 输出的 `dirt_tiers` 字段
同时给出 `caveat`、`note` 与每档的 `range_pct`。真实客户占比必须用其本期行级数据
跑 `--lines` 得到。

### 3.2 豁免对降级清单的影响

若报告应用了有期限豁免（见 `EXEMPTIONS.md`），被豁免的阻断差异已降为提示，
不会再进入降级清单的 `entries`，也不会计入 `subject_count`。
正确顺序是：`inspect()` → `apply_exemptions(..., lines=本期行)` →
`evaluate_exemption_impact()` → `evaluate_block_impact()` / `export_downgrade_list()`。
豁免配置读不到时应按「不应用豁免」处理，阻断照常生效。

豁免影响面另出一份计量就绪 JSON + CSV（字段见 `EXEMPTIONS.md` §8），
`affected_billable_lines` 回答「豁免让多少计费行自动放行」。如果调用方没有提供行级数据，
报告会给出 `exemption_cap_checked=false` 与 `EXEMPTION_CAP_UNCHECKED` 告警，
**不得把 0 当作「没有影响」**；15% 二次复核与整批豁免判定在缺行级数据时明确为「未校验」。

计费标记与放行状态必须一致：`billable=false` 的行如果又被豁免自动放过（`auto_released`），
输出 `BILLABLE_FLAG_CONFLICT` 告警并给出 `billable_conflict_lines` /
`billable_conflict_line_ids`，不得静默取其中一边；调用方应先核对 `billable` 标注。


## 4. 降级清单导出与读取

导出（只读，本地 JSON）：

```python
from apps.masterdata import export_downgrade_list

export_downgrade_list(report, "dist/masterdata_downgrades.json", snapshot)
```

CLI：

```bash
python -m apps.masterdata -i fixtures/masterdata/dirty.json \
  --lines fixtures/masterdata/recon_lines_sample.json \
  --downgrades dist/masterdata_downgrades.json
```

读取（API 侧唯一入口）：

```python
from apps.masterdata import DowngradeListUnavailable, load_downgrade_list

try:
    downgrades = load_downgrade_list("/data/masterdata/downgrades.json")
except DowngradeListUnavailable:
    ...  # fail-closed：整批转人工
```

`load_downgrade_list` 在文件缺失、JSON 损坏、`schema_version`/`kind` 不匹配、
条目缺字段、严重度不是阻断、主体类型不是供应商/客户/物料时一律抛
`DowngradeListUnavailable`。

### 4.1 格式（schema_version 1.0）

```json
{
  "schema_version": "1.0",
  "kind": "masterdata_downgrade_list",
  "generated_from": {
    "source": "fixtures\\masterdata\\dirty.json",
    "org_id": "ORG-A",
    "as_of_period": "2026-10"
  },
  "block_semantics": "该主体涉及的对账行强制降级为待人工；数据照常加载、差异照常出清单，不拒绝加载，也不拒绝出清单",
  "policy": {
    "on_unavailable": "fail_closed",
    "on_unavailable_detail": "读不到、解析失败或 schema 不兼容时整批转人工；不得把空清单或上一次缓存当成无降级",
    "on_unattributed": "downgrade_all",
    "on_unattributed_detail": "存在无法归属主体的阻断时整批转人工"
  },
  "downgrade_all": false,
  "unattributed_rule_ids": [],
  "summary": {"subject_count": 15, "entry_count": 25},
  "entries": [
    {
      "subject_type": "供应商",
      "subject_code": "SUP-012",
      "subject_name": "示例无效税码供应商",
      "rule_id": "TAX-001",
      "rule_name": "引用无效税码",
      "severity": "阻断",
      "category": "税码与税率",
      "reason": "tax_code='VAT99' 不在税码目录（有效：VAT06, VAT09, VAT13）",
      "source": "fixtures\\masterdata\\dirty.json:供应商[SUP-012].tax_code",
      "suggestion": "在 ERP 修正税码或补齐税码目录；修正前该主体相关发票与对账行不得自动处理。",
      "period": null
    }
  ]
}
```

稳定性约定：

- 固定顶层字段与字段顺序；`entries` 按
  `(subject_type, subject_code, rule_id, period, reason)` 确定性排序，同一输入可重复导出。
- 同一主体被多条规则命中时逐条输出，不做合并，便于审计「为什么降级」。
- 只输出有效严重度为「阻断」的差异；配置把某规则覆盖成「提示」后，该规则不再进入清单。
- UTF-8 无 BOM、缩进 2、行尾 `\n`。

### 4.2 API 侧怎么接（下一轮实现时照此执行）

- **接哪个函数**：只接 `load_downgrade_list(path)`（或先自行取到 JSON 后调用
  `parse_downgrade_list(raw)`），不要直接读 JSON 拼字段，也不要绕过 schema 校验。
- **在哪个时点调用**：每批对账**开始前调用一次**，用 `generated_from.org_id` 与
  `as_of_period` 校验清单与当前批次一致（不一致按不可用处理）。不要在批中反复读文件，
  也不要把清单缓存到下一批——主数据修复后清单会变化。
- **失败策略：fail-closed**。出现以下任一情况，整批转人工，不得继续自动对账：
  1. `DowngradeListUnavailable`（文件缺失 / 损坏 / schema 不兼容 / 字段非法）；
  2. `org_id` 或 `as_of_period` 与当前批次不一致；
  3. 清单超过一个对账批次未刷新（建议由调用方按批次时间戳判断）。
- **`downgrade_all=true`**：整批转人工，忽略 `entries` 是否为空。
- **命中判定**：行上的 `supplier_code` / `customer_code` / `item_code` 任一出现在
  `entries` 的 `(subject_type, subject_code)` 集合中，该行转人工；原因取该主体对应条目的
  `rule_id`、`reason`、`source`。
- **空清单不等于无降级**：只有成功读取且 `downgrade_all=false` 时，空 `entries` 才表示
  本期没有需要降级的主体。
- 本轮不新增 API 路由、不改 `web/`；API 只在下一轮按上述约定接入。
