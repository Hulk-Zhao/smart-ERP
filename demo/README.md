# smart-ERP 演示数据集

面向演示的三套行业数据集，配套幂等加载、单文件导出与云服务器导入。数据落在独立的
`demo` schema，同时把 API 真正读取的 `erp.*` 投影一起灌好，因此**不改前端渲染代码、
不改 API 契约**，页面直接消费。

## 目录

```
demo/
├── README.md                  # 本文件：场景、口径、快速开始
├── schema.sql                 # demo schema（33 张表，全部带 scenario 列）
├── load.py                    # 幂等加载：--scenario / --all / --dry-run / --no-erp
├── verify_audit_chain.py      # 从库里读回审计影像，用生产 AuditLedger 复验哈希链
├── export.py                  # 一键导出：pg_dump -Fc + sha256 + --verify 回灌校验
├── tools/build_datasets.py    # 确定性生成器（同 seed 重跑结果一致）
├── scenarios/
│   ├── index.json             # 三套场景的行数、自检结论总览
│   └── <场景>/
│       ├── *.csv              # 主数据 + 交易 + 特殊场景数据
│       ├── erp_seed.json      # erp.* 投影（对账批次/差异/工单/审批/审计影像）
│       └── manifest.json      # 行数、seed、时间窗、自检问题、逐文件 sha256
└── cloud/
    ├── import.sh              # 云服务器一条命令导入（先清后灌，可重复执行）
    ├── provision-demo.sql     # 最小角色开通（PG15，无 pgvector）
    └── README.md              # 云端导入、环境变量、校验与回退
```

## 三套场景

时间窗统一为过去 12 个月：`2025-11-12 ~ 2026-11-11`，业务日期最晚到 2026-09-27，
不存在未来时间戳。每套约 30 客户 / 80 供应商 / 600 物料 / 2000+ 订单行。

### 1. 制造业多工厂 `manufacturing-multi-plant`

- **故事线**：苏州总装、宁波精工、武汉电子三地工厂共用主数据。两层以上 BOM（FG→SF/RM，
  SF→RM）驱动生产工单领料；采购收货按批次入库形成库龄；月末按供应商对账，差异走工单
  审批、执行、校验与补偿回滚。
- **给谁看**：制造业集团 IT/财务/供应链负责人，关心多组织隔离、BOM 与工单、批次库龄。
- **推荐页面**：
  - 工单列表/详情：11 种状态齐全，`TK-202609-0012`、`TK-202609-0013` 是两条完整补偿回滚链；
  - 对账批次：`2026-07/08/09` 三个批次，每月 104 行里 80 条差异；
  - 差异详情：前像/后像落在真实 PO/收货单上；
  - 计量：按有效对账行出账。
- **数据锚点**：5 仓、2 层 BOM 1270 行、工单 220 张、批次 787 个、GL 2040 行。

### 2. 快消分销 `fmcg-distribution`

- **故事线**：上海总部 + 杭州/南京分仓管理 30 家一级/二级经销商与 KA 直营。月度促销、
  阶梯返利先计提后核销；账期与信用额度约束大量小额订单；月末以经销商结算单对账，重点
  处理促销费用、数量短装与重复开单。
- **给谁看**：快消品牌商/经销商集团的销售运营与财务共享中心，关心返利、信用、海量小单。
- **推荐页面**：
  - 总览：700 张订单、2081 行、客单价低，能体现"小单高频"；
  - 对账批次与差异：促销/返利相关差异与容差内差异同屏；
  - 工单与审批：待审批/已驳回/执行中/已回滚混合，演示审批与补偿；
  - 计量：按有效对账行计费。
- **数据锚点**：3 主体、3 仓、促销 24 条、返利计提 36 条、销售行 2081、采购行 775。

### 3. 跨境多币种 `cross-border-multicurrency`

- **故事线**：深圳、香港、新加坡三个主体做转口与本地交付。外币订单按成交日汇率折算；
  进口报关计提关税与进口增值税；月末跨主体按内部转移价结算；供应商对账单同时出现汇率、
  税率与单位换算差异。
- **给谁看**：跨境供应链、国际结算与税务团队，关心多币种、汇率、关税、跨主体。
- **推荐页面**：
  - 对账差异：`tax` / `uom` / `rounding_tolerance` 差异集中出现；
  - 对账批次：跨主体、跨币种批次；
  - 工单详情：外币金额与补偿链路；
  - 计量：多主体账单。
- **数据锚点**：5 币种、48 条日汇率、120 张报关单、60 笔跨主体结算、销售行 2202。

## 数据口径

- **编码规则**
  - 客户：`C-AUTO-0001`（制造）/ `C-DIST-0001`（分销）/ `C-OVR-0001`（跨境）；
  - 供应商：`S-STL-0001`、`S-SUG-0001`、`S-BAT-0001`（按供应品类分段）；
  - 物料：`FG-FRA-0001`、`RM-CRS-0001`、`PK-PAL-0001`、`SF-GBL-0001`——前缀是物料类型，
    中段是**语义分类段**（如 FRA=前副车架、CRS=冷轧钢板、PAL=木托盘），末段是流水；
  - BOM：`BOM-<父项编码>`；批次：`LOT-<流水>-<日期>-<订单序号>-<行号>`。
- **勾稽**
  - 销售：订单行金额合计 = 订单头；发货数量 ≤ 订单数量；开票 ≤ 发货；
  - 采购：收货 ≤ 订单；发票 ≤ 收货；库存批次数量 = 收货行数量；
  - 总账：每张凭证借贷相等，`gl_entry` 全表借贷合计相等。
- **对账差异**：每个批次同时覆盖契约里的 8 类（`unit_price` / `qty` / `scrap` / `uom` /
  `tax` / `missing_doc` / `duplicate` / `rounding_tolerance`），另含 `candidate` 与
  `unparseable` 两个候选/解析失败态（API 分别归入 qty/uom）。差异行经 `recon_line_link`
  指向真实工单，可查看前像/后像。
- **工单**：覆盖 `待确认/待审批/已驳回/执行中/校验中/已完成/补偿中/已回滚/需人工处置/
  已锁定/已终止` 11 种状态，其中 2 条是"执行中→校验失败→补偿中→已回滚"的完整链路。
- **时间**：下单 < 发货/收货 < 开票 < 对账批次 < 工单事件，全部落在过去 12 个月内。

## 快速开始

```bash
# 1) 重新生成（可选；仓库已带生成结果，确定性重跑字节级一致）
python demo/tools/build_datasets.py

# 2) 只看统计与自检，不连库
python demo/load.py --scenario manufacturing-multi-plant --dry-run

# 3) 单套加载（幂等，可重复执行）
python demo/load.py --scenario manufacturing-multi-plant --dsn "$DEMO_DSN"

# 4) 一次全灌（三套场景各用独立 tenant_id，互不覆盖）
python demo/load.py --all --dsn "$DEMO_DSN"

# 5) 回归守卫（内存）：用生产函数重算种子里的全部审计影像 hash
python -m pytest tests/demo/test_seed_audit_chain.py -q

# 6) 回归守卫（读库）：灌库后从 erp.audit_image 读回，用生产 AuditLedger 验链
python demo/verify_audit_chain.py --all --dsn "$DEMO_DSN"
```

### 演示前检查（照做，别跳）

```bash
# 1) 重灌（三套或单套），用表属主/迁移账号
python demo/load.py --all --dsn "$DEMO_DSN"

# 2) 重灌后必须重启 API（原因见下，跳了会在台上 409）
docker restart erp_api

# 3) 清桩，回到干净起点：reset 同时归零单号，下一单从 PO-STUB-000001 起
curl -X POST http://127.0.0.1:8021/stub/reset
```

> 端口一律以 `docker compose config` 的输出为准（当前 `u8-stub` 的宿主映射是
> `127.0.0.1:8021:8021`）。**`8013` 是 compensation 的容器内端口，只 `expose`
> 不发布宿主**：在本机 curl 8013 会直接连接失败；它绝不是桩。

**为什么重灌后必须重启 `erp_api`**：种子把工单的 `pending_write.idem_key` 固定成
`demo-demo-w2-write-9001`，而写结果的幂等缓存在 **API 进程内**
（`erp.connector_idempotency` 表为空可证）。不重启时，重灌后再执行会命中
`REPLAY`：直接返回上次的回执、**不向桩发 POST**；此时桩已被 reset，
随后的 verify 按 `source_doc_no` 查不到 → `409 E-VERIFY-MISMATCH（F6）`。
重灌 + 重启 API + reset 桩，三步缺一不可。

### 库内审计链复验（一条命令）

```bash
python demo/verify_audit_chain.py --all --dsn "$DEMO_DSN"
```

三套场景的 `tenant_id` 从各自的 `erp_seed.json` 读取，脚本按 `SqlAuditStore.chain`
的顺序把行读回内存，再走生产 `AuditLedger.verify`（与演示栈打开工单详情同一条判定
路径）。期望输出：

```
[ok] cross-border-multicurrency tenant=... checked=18/18
[ok] fmcg-distribution tenant=... checked=18/18
[ok] manufacturing-multi-plant tenant=... checked=18/18
TOTAL images=54 tenants_failed=0
```

任一租户验链失败、或库里条数与种子不一致（漏灌/多灌），退出码为 1。
**注意**：本脚本证明的是「种子落库、读回后与生产验证口径一致」；生产写入侧对带时区
`datetime` 的归一由其自身的往返用例证明，不在本数据集范围内。

`load.py` 的事务语义：任一场景失败整批回滚；`demo.*` 先按 scenario 删除再插入；
`erp.*` 先按 tenant_id 删除再插入（append-only 表需临时关闭用户触发器，请用表属主/迁移
账号执行）。

## 打包与云端导入

```bash
# 导出单个可传输文件（含 demo + erp 两个 schema 的结构与数据）
python demo/export.py --dsn "$DEMO_DSN" --docker-container <pg 容器名> --out demo/dist --verify

# 云服务器（一条命令，重复执行即重灌）
PGDATABASE=smart_erp PGHOST=<pg-host> PGPORT=5432 PGUSER=erp_migrator PGPASSWORD=<pwd> \
  bash demo/cloud/import.sh smart-erp-demo-YYYYMMDD-HHMM.dump
```

细节见 [cloud/README.md](cloud/README.md)。

## 演示写路径：模拟外部系统（u8-stub）

> ⚠️ `apps/connectors/u8/stub_server.py` 是**模拟外部系统（stub）**，不是真实 U8，
> 也不是任何真实 ERP。启动横幅、响应体 `simulated: true`、响应头
> `X-Simulated-System: u8-stub` 都明确标注；**禁止**把它当生产对接，也禁止在生产环境指向它。

演示里的「跨系统写入 + 可审计回滚」走真实 U8 连接器（`apps/connectors/u8`，真实 HTTP
预演 → 写入 → 校验 → 冲销），只把外部系统换成本地桩：

```bash
# 终端 A：启动模拟外部系统（端口与 compose 的宿主映射保持一致：8021，仅本机监听）
# 注意：脚本默认端口是 8013，但 8013 已被 compensation 占用，本地直跑也要显式 --port 8021
python -m apps.connectors.u8.stub_server --host 127.0.0.1 --port 8021

# 终端 B：让 API 连到桩（不设置则写入显式失败，不会假成功）
export ERP_U8_BASE_URL=http://127.0.0.1:8021          # PowerShell: $env:ERP_U8_BASE_URL="http://127.0.0.1:8021"
```

种子新增两张锁定 `pending_write` 的待审批工单：

| 工单号 | 写入等级 | 预期结果 |
| --- | --- | --- |
| `DEMO-W2-WRITE-9001` | W2 | U8 是 B 级连接器，强制双人复核（`reviewer` + `approver`）；两票齐后由发起人触发执行，真实 `POST` 到桩，回执含 U8 单号/时间戳/关键字段，审计影像有 `field_diff` |
| `DEMO-W3-FUND-9002` | W3 | 在 AccessGate 被 `E-POLICY-W3FORBIDDEN` 拒绝，`/stub/requests` 中无任何业务写入（判据见下方验收口径，**零外部写入**） |

> 演示口径（重要）：`DEMO-W2-WRITE-9001` 的 `reviewer` 票由种子预置在
> `erp.approval_record`（操作人 `bbbbbbbb…`、时间 `12:15`、理由见 comment），
> 现场只需以 `dddddddd…` 补 `approver` 一票；两票齐后 `satisfied=true`，但工单
> **不会因此自动执行**。真正触发写入的是发起人（执行体 `aaaaaaaa…`）调用
> `POST /v1/tickets/{id}/actions/approve`；审批人身份调该接口会 403
> `E-AUTHZ-001`（职责分离约束，不是缺陷）。单令牌环境请按「审批人点票 +
> 执行体触发」两步演示，不要用审批人令牌冒充发起人。

### 演示身份（`ERP_DEMO_SESSION_IDENTITIES`，仅 demo 档）

| user_id | roles | 用途 |
| --- | --- | --- |
| `aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa` | `operator` | 发起工单 |
| `bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb` | `reviewer,finance` | 投 reviewer 票（种子已预置） |
| `dddddddd-dddd-4ddd-8ddd-dddddddddddd` | `reviewer,approver,cfo`（无 `finance`） | 反例：类别闸应拒绝 |
| `eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee` | `reviewer,approver,finance` | 投 approver 票 |

> `bbbbbbbb` 只持有 `reviewer,finance` **不是漏配**：reviewer 票只校验 `reviewer` 角色
> （`ApprovalService._assert_role_capability`）+ 场景类别 `finance`
> （`BusinessCategoryPolicy.assert_reviewer`）；`approver` 票才接受 `approver` 或 `cfo`。
> 两票由不同人各持一个角色即可，不要给它补 `approver`。

核对「单据到底有没有到 / 有没有冲销」：

```bash
curl http://127.0.0.1:8021/stub/receipts    # 已落草稿回执（cCode、received_at、关键字段回显）
curl http://127.0.0.1:8021/stub/requests    # 桩收到的全部请求；W3 被拒时按下方机械判据检查，不是「整体为空」
curl http://127.0.0.1:8021/stub/reversals   # 已冲销记录；补偿后出现该 U8 单号
```

> 零外部写入判据（机械可执行）：`/stub/requests` 中，**方法为 `POST` 或 `DELETE`
> 且路径不以 `/stub/` 开头的条目必须为空**；健康检查与查询探测只是 `GET`，不计入。
> 理由：业务写入只可能走 `POST`/`DELETE`，而 `/stub/*` 是桩自己的查询面；用「方法 +
> 路径前缀」双重限定，比列白名单更难被放宽。

失败与补偿演练：

```bash
# 让接下来 3 次写入返回 503（工单会停在原状态并显式报错，不会静默成功）
curl -X POST http://127.0.0.1:8021/stub/fault -H 'Content-Type: application/json' \
  -d '{"fail_posts": 3}'

# 恢复后重试同一工单即可；补偿接口会发真实 DELETE 草稿，
# 之后 /stub/reversals 会出现该单号的冲销记录（这是「可回滚」的唯一实证方式）。
```

## 演示身份验收口径

多身份配置上线后，验收不看「某个令牌能不能用」，而看**有效身份集合与文档声明逐条对齐**：
身份数量、每个 `user_id`、每个角色都必须与本文和部署清单一致，**多一个少一个都算不通过**。
（单令牌阶段的「发起人令牌必须 401」只适用于尚未启用多身份的栈，届时不再作为验收项。）

## 运维口径：live 库是长期累积运行态

演示库不是「跑一遍种子就回到已知状态」的产物：它承载排练、手工点击和多次运行，
表里会留下种子之外的行。实测样例：本次重灌前，`DEMO-W2-WRITE-9001` 上有两条
种子中不存在的审批记录（`bbbbbbbb` 的 `demo review`、`cccccccc` 的
`demo approve`，时间 10:11Z）；仓库种子里没有这段文案，`cccccccc` 也不在
当前身份配置里——**来源不明**，不要归因为种子或某个人。它只能说明：
**live 库无法从种子复现**。

因此重灌前必须留底，作为「重灌前后差额」的唯一依据。模板：

| 指标 | 重灌前 | 重灌后 |
|---|---|---|
| `erp.audit_image` 条数 / `max(chain_seq)` | | |
| 目标工单 `approval_record` 条数 | | |
| `erp.ticket` 总数 | | |
| 桩 `receipts` / `requests` / `reversals` | | |

实测样例（2026-11-11）：`56 / 146`、`3`、`51`、`1 / 137 / 0` →
重灌后 `54`、`1`、`54`、`0 / 0 / 0`。

## 与前端/契约的关系

- 前端当前走 `web/src/api/mock.ts`；本数据集**不替换 mock 的字段形状**，而是把同一份契约
  背后的 `erp.*` 表灌满，API 接通数据库后页面自然显示真实数据，渲染代码零改动。
- `demo.*` 是"源业务系统"明细，用于讲解订单→发货→开票→对账的来龙去脉，以及和 BI 对拍；
  API 只读 `erp.*` 投影。
- 三套场景各自独立 `tenant_id`，可同时存在；前端会话必须指向对应 tenant（见下方缺口①）。

## 已知契约/部署缺口（已在台账登记，未自行扩契约）

1. **无 session bootstrap**：`/v1/*` 全部要求 `X-Session-Token`，仓库没有签发入口。
   演示环境需要集成侧提供登录换 token，或使用验收脚本注入会话。
2. **审计影像未接数据库**：`/v1/audit/images`、`/v1/audit/chain` 仍是内存态 `AuditLedger`，
   `erp.audit_image` 已灌数据但页面不会展示；如要演示影像链路，需集成把该接口切到 SQL。
3. **部署脚本依赖 pgvector**：`deploy/smart-erp/db/00-provision.sql` 会 `CREATE EXTENSION vector`，
   与"演示库不用 pgvector"冲突。本目录提供 `cloud/provision-demo.sql`（无 vector），
   是否给 demo 档拆一份开通脚本请集成决定。
4. **场景类别只注册了 `ag2_processing_fee`**：审批流只认这一种 scene_code，本数据集已统一使用；
   若演示需要更多场景类别，需先扩注册表。

## 依赖与许可

- Python 3.12（加载/导出脚本仅标准库 + asyncpg；复验脚本复用生产 SQLAlchemy + asyncpg
  栈；生成器复用生产审计契约 `apps.platform.contracts.audit`，因此与 API 使用同一份
  哈希实现，不另写算法）。
- PostgreSQL 15（导出端 15.19），UTF-8，`Asia/Shanghai`，不使用 pgvector。
- 生成器为确定性：`manifest.json` 里的 `seed` 固定，除 `generated_at` 外重跑结果一致；
  CSV/JSON 显式以 `\n` 写出，不同平台生成的字节一致。
- 审计影像 `created_at` 以 UTC 存储并参与哈希：PG 往返后 asyncpg 按 UTC 返回 datetime，
  种子若用 `+08:00` 参与哈希，同一时刻会算出另一个 digest（演示页会报「疑似篡改」）。
