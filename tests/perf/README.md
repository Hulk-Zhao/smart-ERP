# T704 对账性能基线

> **结论的使用边界**：本目录给的是「当前这台机器的服务端耗时」，**不是目标规格吞吐**。
> 目标规格（4C4G）环境尚未拿到，**未在目标规格验证**，不得把下表数字直接报给 POC 客户。

## 1. 测的是哪一段

```
POST /v1/recon/runs 的服务端处理链：
  请求体校验（JSON→Pydantic）
  → 引擎（build_request + reconcile：重复计费识别、三层匹配、归因）
  → 落库（批次 1 行 + 每条对账行 1 行 INSERT）
  → list_recon_diffs 首页 50 条（含 result_view 反序列化）
```

**不含**：网络传输、文件上传/解压、连接器拉数、前端渲染、鉴权与四道闸。
所以对外只能说「这一段服务端耗时」，不能省略成「系统处理 10 万行需要 X 秒」。

**为什么进程内调用而非打 HTTP**：main 上 api 进程未装配对账所需的连接器元数据时，
`POST /v1/recon/runs` 会在 `pick_meta` 处 503 `E-CONN-UNAVAILABLE`，HTTP 端到端产不出成功批次。
脚本绕过 `pick_meta` 与 `_authorize`（两者都不在计时口径内），
校验、引擎、store、真实 PG 全部是生产代码。

## 2. 数据集（合成，可复现）

- 数量/金额分布取自 `demo/scenarios/manufacturing-multi-plant/recon_case.csv`（只取 qty/amount 两列）。
- 每行 `(supplier_code, item_code)` **唯一**：200 供应商 × 最多 500 物料，我方与供应商 1:1。
- 每 100 行注入 1 行数量差异（供应商数量 ×1.02），用于产生差异结果。
- `scope.period=2026-09`，`org_id=ORG-SZ`，`system_ids=[u8,wms,sap]`，主数据（供应商/物料）随数据集一并生成。
- **不含**「同供应商同物料多单」形态：那会产生候选笛卡尔积，属另一个问题，不混入本基线。

## 3. 环境（2026-11-11 实测）

| 项 | 值 |
|---|---|
| 宿主 | Windows 11（10.0.26200） |
| CPU | AMD Ryzen 9 9955HX 16C/32T |
| 内存 | 31.2 GiB（Docker Desktop 分配 32 CPU / 15.2 GiB） |
| Python | 3.12（app 镜像内） |
| 进程 | api 单进程单 worker，对账引擎单线程 |
| DB | PostgreSQL 15，**独立压测容器**，与宿主同机（非 live 演示栈） |
| 网络 | 无（进程内直调）；不含 HTTP/上传/拉数 |

`recon_baseline.py` 每档输出会附带 `cpu_model`、`cpu_count`、`mem_total_mib`、`python`、
`db_version`、`db`（host/db，不含口令），换机复跑时以输出为准，不要照抄本表。

## 4. 三档实测（main `12ba3e3`，未含 S2 分桶优化）

每侧行数；合计 = `run_recon` + 差异首页。

| 档位（每侧） | 校验 | 引擎 | 落库 | 结果可查 | 服务端合计 |
|---|---|---|---|---|---|
| 1 万（2 万行对象） | 0.25s | 28.7s | 14.3s | 0.48s | **44.4s** |
| 5 万（10 万行对象） | 0.78s | 598.2s | 57.2s | 2.0s | **674.3s（11 分 14 秒）** |
| 10 万（20 万行对象） | 1.39s | 2268.9s | 63.5s | 3.0s | **2380.1s（39 分 40 秒）** |

10 万档原始输出（`/tmp/perf100k.out`）：

```json
{"rows_each_side":100000,"total_line_objects":200000,"status":"待人工复核","diff_count":50,
 "validate_s":1.389,"engine_parse_s":2.722,"engine_reconcile_s":2266.208,"engine_total_s":2268.929,
 "view_build_s":41.847,"snapshot_build_s":0.455,"price_s":0.039,"persist_s":63.543,
 "run_recon_total_s":2377.039,"read_first_page_s":3.039}
```

**重复性（同一脚本、同一库、1 万档连续 3 次暖机后）**：`engine 16.7 / 15.3 / 13.7s`、
`persist 10.1 / 8.4 / 7.6s`、`合计 28.1 / 24.7 / 22.3s`。
首轮（冷库、含建表/缓存未热）是 44.4s，与暖机后差约 2 倍——**单次运行不足以对外报数**，
目标规格测量必须「每档 ≥3 次、报中位数、注明冷/暖」。

**瓶颈**：引擎 `reconcile` 占 65%（1 万）→ 89%（5 万）→ 95.5%（10 万）；
行数 5 倍时引擎 20.8 倍、再翻倍 3.79 倍（n² 理论 4 倍）。
代码在 `apps/recon/matching.py:448-452`（每个供应商行 `enumerate(our_lines)` 全量打分）
与 `matching.py:278-285`（合单候选同样全量扫描）。落库是 N 次单行 INSERT。
10 万档引擎阶段 RSS 850 MiB、未 OOM、未交换 → **CPU 计算瓶颈，不是内存**。

**优化建议（未实现）**：按 `(supplier_code, item_code, period)` 分桶后桶内打分，
把 `matching.py:448-452` 的 O(n²) 降为 O(n·桶内行数)；随后把 `save()` 的逐行 INSERT
换成 `executemany`/多值 VALUES。前者决定复杂度，后者决定常数，都不需要改协议。

## 5. 复现步骤

```bash
# 0) 准备独立压测库（不要指向 live 演示栈的 PG）
#    迁移到最新后，确认库名/端口与演示库不同

# 1) 把脚本与分布样本送进 app 镜像容器
docker cp demo/scenarios/manufacturing-multi-plant/recon_case.csv <容器>:/tmp/scen/recon_case.csv
docker cp tests/perf/recon_baseline.py <容器>:/tmp/perf.py

# 2) 三档一起跑（DSN 必须是独立压测库；--yes 表示确认）
docker exec <容器> python -u /tmp/perf.py \
  --dsn 'postgresql+asyncpg://user:***@<bench-pg>:5432/smart_erp' \
  --scenario-csv /tmp/scen/recon_case.csv \
  --rows 10000 50000 100000 --tag baseline --yes
```

每档输出一行 JSON，最后一行 `summary` 汇总环境与各档数字。
`--tag` 会追加到单号，重复跑同一规模时避免幂等指纹冲突。

**红线**：禁止在 live 演示栈的 PG 上跑；禁止为此重启/重灌演示栈；
压测库必须独立（本基线使用独立 PG15 容器，非演示栈）。

## 6. 与 S2 优化的关系（S7 已复核，结论有保留）

S2 在分支 `fix/recon-conn-wiring-and-bucketing@33c8e99`
（`perf(recon): 匹配改分桶降 O(n²)，对账行落库改批量写`）上做了两件事：
`matching.py` 按 `(supplier_code,item_code,unit)` 预建索引、只在桶内取候选；
`recon_store.save()` 改 `executemany`。该分支**尚未进 main**。

**S7 复核方法**：在同一个 app 容器（main 镜像）里只替换该分支的 `matching.py` 与
`recon_store.py` 两个文件，其余代码与环境不变，用本脚本重跑三档；跑完已还原并复测确认
（engine 回到 13.7s）。这是**同机复核**，不是目标规格验证。

| 档位 | main 合计 | 优化后合计 | 端到端倍数 | main 引擎 | 优化后引擎 | 引擎倍数 |
|---|---|---|---|---|---|---|
| 1 万 | 22.3–44.4s（暖/冷） | 2.9s | **约 8–15×** | 13.7–28.7s | 0.69s | **约 20–42×** |
| 5 万 | 674.3s | 25.7s | **约 26×** | 598.2s | 3.44s | **约 174×** |
| 10 万 | 2380.1s | 99.0s | **约 24×** | 2268.9s | 7.17s | **约 316×** |

**哪一条站不住**：「同数据下提升两个数量级」只对**引擎阶段**成立（≥5 万档 174–316 倍）；
**端到端不是两个数量级**，10 万档是 24 倍（约 1.4 个数量级），
因为落库、视图投影、首页读取不随引擎一起降。
把这句话写成「整体提升两个数量级」会高估约 4 倍，属于不能对外说的表述。

优化后瓶颈转移：10 万档 `_batch_view` 60.3s（约 61%）、`persist` 21.6s（约 22%）、
引擎 7.2s（约 7%）。视图投影在 10 万档出现超线性（2 倍行数约 5 倍耗时），
且与 main 同档 41.8s 存在运行间波动，**需要单独定位后才能对外引用**。

因此：

- 第 4 节表是 **main 未优化**的数字，可作为优化前基线。
- 优化后的数字可引用上表，但必须限定「引擎阶段」与「S7 同机复核、未进 main」。
- 无论优化前后，都**不得**写成目标规格吞吐，也不得换算成任何「10 万行 X 秒」的承诺。

## 7. 目标规格（4C4G）还缺什么

当前数字**不覆盖** 4C4G：引擎单线程，核数不是变量，但落库与磁盘 IO 会显著变化，
不能按比例外推。补齐至少需要：

1. 一台 4C4G 的目标规格机器（或等价限额的容器：4 CPU / 4 GiB），同机部署独立 PG15；
2. 记录 CPU 型号、磁盘类型（SSD/NVMe）、PG 关键参数（`shared_buffers`、`work_mem`、`fsync`）；
3. 用本脚本同样跑 1 万 / 5 万 / 10 万三档，每档至少 3 次取中位数（当前每档 1 次）；
4. 若同时验证 S2 优化，用该分支代码重跑同样三档，并在报告中标注分支 sha；
5. 报告里固定写清「服务端这一段 + 不含网络/上传/拉数」。

在上述五条完成前，对外统一表述：
**「性能以 POC 在您环境中的实测为准；我们已在 16C/32T 工作站完成合成数据基线，目标规格尚未验证。」**
