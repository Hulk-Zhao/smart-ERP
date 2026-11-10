# 智能执行工作台 · 前端（S6）

React 18 + TypeScript + Vite 的 7 页工作台。数据来自真实后端 `/v1` 契约（C16），类型由契约生成；离线开发可显式打开 mock 开关。

> **校验生成物只认这一条命令：`npm run gen:api -- --check`。`--` 不能省。**
> 省掉 `--` 时 npm 会把 `--check` 吞掉，脚本按「无参数」执行，等于把生成文件重写一遍——看起来成功，实际是静默覆盖，之后的漂移再也报不出来。
> `npm run check:api` 只是这条命令的别名（`"check:api": "npm run gen:api -- --check"`），不要新增第二条直连生成器的校验入口。

## 启动

```bash
cd web
npm install
npm run dev        # http://localhost:5173，默认连真实后端
npm run gen:api    # 从 C16 契约重新生成 TS 类型与路径表（会写盘）
npm run gen:api -- --check   # 校验生成物是否与契约漂移（只读，build 前置）
npm run build      # gen:api -- --check && tsc --noEmit && vite build
npm run test       # vitest（用例在仓库根 tests/web/）
```

Node 18+。首次 `npm install` 如提示 esbuild 安装脚本未执行，运行 `npm rebuild esbuild`。

## 契约是唯一真源

- 契约：`contracts/api/product_api.openapi.v1.json`（由 `apps/api/contract.py` 从路由生成，17 条路径；`GET /v1/metering/batches/{id}/statement` 经批准并入，17 条与 sha 为冻结基线）。
- 生成器：[scripts/gen-api-types.mjs](file:///e:/tools/Factroy_Agents/smart-ERP/web/scripts/gen-api-types.mjs)，零第三方依赖。
- 生成物：[src/api/generated/contract.ts](file:///e:/tools/Factroy_Agents/smart-ERP/web/src/api/generated/contract.ts)，含全部 schema、`CONTRACT_SHA256` / `CONTRACT_PATH_COUNT`、`WRITE_PATHS` 与 `ApiOperations`（键为 `"METHOD /path"`）。
- 页面与 client 的请求/响应类型只从 `ApiOperations` 取；`src/api/types.ts` 只做重导出与 UI 展示元数据（12 状态、8 类差异码），**不手写契约字段**。
- 契约变更流程：后端重新生成契约 → `npm run gen:api` → 评审类型差异。`npm run build` 会在生成物漂移时直接失败。

### 排查：契约没动却报「生成物漂移」（BOM/CRLF）

生成器在 `JSON.parse` 与计算 `CONTRACT_SHA256` 之前，先去掉 BOM 并把 CRLF 归一化为 LF。因此同一份契约在 Windows（CRLF 检出）和 CI（LF 检出）会得到同一个 sha，不会出现「本地绿、CI 红」的假漂移。

如果仍然报漂移，按顺序排查：

1. `git diff --stat contracts/api/product_api.openapi.v1.json`：契约是否真的改过（字段增删，而非换行）。
2. `git status web/src/api/generated/contract.ts`：生成物是否被某次「少了 `--` 的校验」静默重写；用 `npm run gen:api -- --check` 复跑确认。
3. `git check-attr text eol -- contracts/api/product_api.openapi.v1.json`：确认 `.gitattributes` 的换行约定生效。归一化只覆盖 BOM/CRLF，不覆盖编辑器改写的其他字节。

## 连接配置

`src/api/config.ts` 默认值（可在界面「连接设置」里改）：

| 项 | 默认 |
| --- | --- |
| `baseUrl` | `/erp/api` |
| scope | `org_id=ORG-1`、`period=2026-10`、`system_ids=[u8,statement]` |
| 角色 | `reviewer` |
| 会话头 | `X-Session-Token` |
| 幂等 | 写接口自动生成 `Idempotency-Key` |

读接口自动拼 G-05 scope 三参数；写接口自动把 `scope` 合入 body。错误统一解析 problem+json（`code` / `detail` / `trace_id` / `fail_code`），W3 403 与审批 409 有专门呈现。

## 页面与路由

| 路由 | 页面 | 契约来源 |
| --- | --- | --- |
| `/` | 工作台总览 | 工单列表 / 审批待办 / 对账批次 |
| `/tickets` | 工单列表 | `GET /v1/tickets` |
| `/tickets/:ticketId` | 工单详情：12 状态轨道、步骤前像/后像、补偿记录、审计链 | `GET /v1/tickets/{id}`、`GET /v1/audit/tickets/{id}/chain` |
| `/approvals` | 审批待办：双人复核意见脱敏、通过/驳回 | `GET /v1/approvals/pending`、`POST .../approve|reject` |
| `/recon` | 对账发起 + 批次状态 | `POST /v1/recon/runs`、`GET /v1/recon/runs/{id}` |
| `/recon/:batchId` | 批次差异列表（按 8 类过滤） | `GET .../diffs` |
| `/recon/:batchId/metering` | 计量表：四类行、单价与来源、本期/累计/封顶余额、算式、data_gaps、CSV + `X-Content-SHA256` | `GET /v1/metering/batches/{id}/statement` |
| `/recon/:batchId/diffs/:diffId` | 差异明细：字段级 diff + 证据坐标下钻 | `GET .../diffs/{diffId}` |

契约未提供「列出全部对账批次」，前端用 localStorage 记录最近批次，也支持手动输入 `batch_id`。

## 三条核心交互

- **差异明细**：逐字段展示「我方值 / 对方值 / 差异 / 原因码」。数量与单据引用取契约分侧字段；金额契约只给单侧/归因值，界面不猜测，差异列展示引擎归因金额。证据可点开查看页码、bbox 与原始坐标 JSON。
- **工单时间线**：12 状态轨道 + 每步前像/后像 + 补偿记录，来自 `steps` 与审计链；终态不可再操作。
- **双人复核**：仅当 `approver_id` 与 `role` 同时匹配当前查看者才渲染意见正文，其他审批人只显示「已提交」，不在界面层泄露。
- **计量表**：从批次详情「计量表 →」进入；四类行（应处理 / 自动放行 / 争议挂起 / 其他不计费）、单价与来源、本期/累计/封顶余额、`calculation` 算式逐条展示。`data_gaps` 紧贴金额显式列出「这几项暂无数据源」，null 一律显示「—」并说明原因，不用 0 冒充。页面明确写出「有效对账行数是上界，不是精确值」。支持 `format=csv` 下载并展示服务端 `X-Content-SHA256` 供自行核验。

## Mock 开关（默认关）

```bash
# 默认：真实后端
npm run dev

# 离线开发：显式打开 mock
$env:VITE_USE_MOCK="true"; npm run dev     # PowerShell
VITE_USE_MOCK=true npm run dev             # bash
```

mock 数据与真实契约类型同源（`src/api/mock.ts`），仍会走 `client.ts` 的同一套类型与错误解析。

## 窄屏

390px 下不允许横向溢出：页面级 `overflow-x:hidden`，所有卡片/表格容器 `min-w-0`，差异表在窄屏纵向堆叠，列表用卡片而非固定宽度表格。

## 测试

用例在 `tests/web/`，使用真实 `node:http` mock 服务器 + 真实 fetch（不替换 fetch）：

- `contract-types.test.ts`：生成物零漂移、sha、路径数、实体类型。
- `api-client.test.ts`：scope/token/幂等头、problem+json。
- `ticket-w3-403.test.tsx`：W3 审批被拒在界面呈现 403 / `E-POLICY-W3FORBIDDEN` / 零写入。
- `approval-409.test.tsx`：审批未满足 409 / `E-POLICY-APPROVAL` / 缺失角色；双人意见不泄露。
- `recon-diff.test.tsx`：含 `rounding_tolerance` 的差异行渲染 + 证据坐标下钻。
- `metering.test.tsx`：四类行、data_gaps 显式可见、null → 「—」+ 原因、上界说明、CSV 下载与 `X-Content-SHA256`、批次详情入口。

## 目录

```
web/
  scripts/gen-api-types.mjs   契约 → TS 生成器（零依赖）
  src/api/generated/          生成物，勿手改
  src/api/                    config / client / errors / types / mock
  src/components/             AppShell, Badges, ErrorPanel, StateRail,
                              FieldDiffTable, StepTimeline, ApprovalOpinions
  src/lib/format.ts           金额/比例/时间格式化
  src/pages/                  7 个页面
tests/web/                    真实链路用例 + helpers
```

## 已知边界

- 契约无「列出全部对账批次」接口，批次靠本地记录 + 手动输入。
- 差异金额无分侧金额时按契约归因值展示，不做推断。
- 审计 CSV 导出按文本流下载，未接文件服务。
