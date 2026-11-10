# 发布记录（smart-erp）

> 依据 C12：每次发布登记一条；镜像 tag 必须是完整 40 位 git sha，禁止 latest。
> 回滚以「上一个 sha」为单位，镜像包不覆写、不删。
> **版本 sha 的唯一取值方式**：发布时刻的 `git ls-remote origin refs/heads/main`。
> `git rev-parse HEAD` 只是本地检出的分支头，不能当作 main（第一次登记就错在这里）。

## 现状 — 2026-11-11：P0 跨租户越权已解除，**v0.2.0 已登记**（基线 `e1926cf`）

原缺陷：`_require_ticket` 与 `TicketRepository.list` 缺 `tenant_id` 谓词，17 条 API 路径中 10 条可跨租户读取/驳回/取消/落审批/导出审计。**三条解除条件已全部满足：**

| 条件 | 证据 |
|---|---|
| ① 修复进 main | #27 的修复随 #32 一起进主干；`apps/api/service.py` 在该 sha 上有四处 `tenant_id` 谓词 + 跨租户统一 404（注意 #32 是 squash，`#27` 的 merge commit 本身不在祖先链上，验证以文件内容为准，不用 `merge-base --is-ancestor`） |
| ② 跨租户用例转绿 | `tests/security/test_idor_scan.py` **24 passed / 0 skipped / 0 failed**。24 条是当年 13 条缺陷清单的**超集**（同一缺陷按「列出/读取/驳回/取消/落审批/导出」等路径拆成多条用例），不是 13 被改成 24，以后不要拿 13 对 24 说对不上 |
| ③ 全量复跑 | Python **3.12**，11 个目录逐项 0 failed（api 41 / recon 63 / platform 94 / security 61 / audit 18 / agents 59+24 skip / connectors 82 / docparse 90 / deploy 110 / observability 18 / e2e 11）；`ruff` 整树 0；契约 `--check` 一致；合并后 CI run `42871516840` success |

原「仍不登记 v0.2.0」的两条理由已消除：`web/` 与 `core/`、`tools/`、迁移 `0003`–`0005` 均已进主干；B-S8-01 的运行态四项已于 2026-11-11 在 `e1926cf` 上通过。登记条见下方「v0.2.0 — 2026-11-11」。下方「准入条款」第 1–3 条为 P0 期间的临时约束，自本登记起由「v0.2.0 登记条」取代（四镜像、demo/full 两档同时验证）。


## v0.2.0 准入条款（2026-11-11 定，硬性）

| # | 条款 | 判定方式 |
|---|---|---|
| 1 | **只发 demo 档**：api + connector-gw + web 三个镜像；full 档不发 | 登记条「档位」字段必须是 demo；出现 full 服务或 full 档镜像的登记视为违规 |
| 2 | **full 档在 B-S8-01 解除前不具备发版条件**（`core/runtime/*` 与 `tools/health.py` 缺失、打包 `include` 未含 `core*`，4 个服务起不来） | 依据只有一个：S8 的 `FULL-READINESS.md`（S8 交付物，随 S8 合入主干）；集成会话不得自行判断「差不多了」 |
| 3 | S6 合并、S8 真 build 出 web 镜像并拿到 `/healthz` 200 后，才可走清单第 3–7 步 | 三个镜像包齐全 + 容器实测 200，缺一不登记 |

> 上段「v0.2.0 登记口径」为 P0 期间的预案（当时 demo 档只发三镜像、worker/scheduler 未交付）。实际登记以下方「v0.2.0 — 2026-11-11」条目为准：四镜像、demo/full 两档、worker/scheduler 已交付并实测。

## v0.2.0 — 2026-11-11（冻结基线；四镜像；demo + full）

| 项 | 内容 |
|---|---|
| git 提交 | `e1926cf6748fe0fa31b432a9df221419dc086c8c`（取值方式：发布时刻 `git ls-remote origin refs/heads/main`，2026-11-11 实测；main 已冻结于此） |
| tag | `v0.2.0` → `e1926cf6748fe0fa31b432a9df221419dc086c8c`（annotated tag 直接打在该提交上；其后的登记 commit 仅含本文件改动，**不含代码**） |
| 镜像 | `smart-erp/app` / `connector-gw` / `doc` / `web`，tag 均为 `e1926cf6748fe0fa31b432a9df221419dc086c8c`；`docker images` 实测 355MB / 323MB / 344MB / 74.6MB（inspect：app 89.0MiB、gw 81.5MiB） |
| 产物 | `dist/erp-images-e1926cf6748fe0fa31b432a9df221419dc086c8c.tar.gz`（173.5MiB）+ `sha256sum.txt`；sha256 `a26df58dffc372c86f35899409bcf173c4f40294b47b32e49cfddfcd04ed57ee` |
| 档位 | demo（api / worker / scheduler / connector-gw / web）与 full（api-prod 端口语义 + doc / worker-agent / worker-io / migrate）两档均验证；full 档 8010 仅绑 `127.0.0.1` |
| 构建方式 | 干净 worktree（`git worktree add --detach <dir> e1926cf…`，`git status --porcelain` 为空）执行 `ERP_IMAGE_TAG=e1926cf… scripts/build-images.sh`；四镜像打进同一 tar.gz，不推镜像仓库 |
| 发布方式 | `scripts/deploy-images.sh --tag e1926cf…`（scp → 远端 `sha256sum -c` → `docker load` → `docker compose --profile <档位> up -d --remove-orphans`） |
| 回滚方式 | 用上一 sha 的 `erp-images-<old-sha>.tar.gz` 重放发布流程；迁移不可自动回退，回滚前确认 `erp.alembic_version_erp`（本版 `0005_worker_runtime`）与旧镜像兼容 |

### 验证要点（可在该 sha 上复跑，均为实测输出）

1. **构建**：上述命令产出四镜像，`docker images` 可见四个 `e1926cf…` tag（大小同登记条）；`sha256sum -c dist/sha256sum.txt` 校验通过。
2. **镜像内容**：`python tests/deploy/check_image_contents.py --no-build --api-image smart-erp/app:e1926cf… --gw-image smart-erp/connector-gw:e1926cf…` → 两镜像 `IMAGE_CLEAN` / `PASS`（不含 `tests/`、`_e2e`）。
3. **四容器探针**（各自 `docker run` 后逐个 `GET`）：
   - api `/healthz` → 200 `{"status":"ok","service":"smart-erp-api","image_tag":"e1926cf…"}`
   - gw `/healthz` → 200 `registered_actions=2`、`connectors[0].alias=u8`、`breaker_state=closed`、`actions_allowlist_hash=sha256:0f752ad6…`
   - doc `/healthz` → 200 `{"status":"ok","service":"smart-erp-doc"}`
   - web `/` → 200，HTML 引用 `/demo/smart-erp/assets/index-CAhPj0Me.js` 与 `/demo/smart-erp/assets/index-DfZgi6P2.css`
4. **compose 编排**：`--profile demo config` 与 `--profile full config` 均 exit 0；full 输出含 `host_ip: 127.0.0.1` / `target: 8010` / `published: "8010"`；两档 `… config | grep -c 8011` 均为 **0**。
5. **worker / scheduler**：四容器按 compose 健康命令 `python -m tools.health <worker|scheduler> --check [--queues …]` 判定，均 `healthy` 且 `RestartCount=0`；插入 5 agent + 3 io 任务后 `agent succeeded 5` / `io succeeded 3`；`erp.worker_heartbeat` 3 行、`age` 为秒级（两次实测 3–9s），`jobs_done` 合计 8（单行分配随抢占而变，不固定为某个值）；`python tests/agents/s8_evidence.py` → `SUMMARY 4/4 passed`（cli_5_jobs 5 succeeded；retry_and_dead 10 succeeded / 4 dead / 6 retries；kill9_reclaim 心跳 3.3s 后回收 1；scheduler_single_point A→B 接管）。
6. **回归**：`python -m pytest tests/deploy -q` → 38 passed。

> **限定（2026-11-11 补记，勿删）**：第 3 条的四容器探针是**各自 `docker run` 后独立 `GET`**，`web` 只测了 `GET /` 的静态首页，**v0.2.0 从未验证「经 web 容器的 API 路径」**。2026-11-11 在 `c3c662b` 上实测：`/erp/api/*` 全部 404（`web-nginx.conf` 的 `proxy_pass $erp_api_upstream/` 使用了变量，nginx 此时不做 location 前缀替换，请求被压成 `/` 送给 api；`/erp/api/tickets`、`/erp/api/openapi.json` 同样 404）。因此本版「可起」的准确表述是「四容器各自健康 + 静态首页可访问」，**不包含 web→api 转发**；该缺陷自 v0.2.0 起一直存在，修复后须在新 sha 上重跑全链路并补一条经 web 的真实请求证据。

### 已知缺口（登记时如实记录）

1. `8011` 无服务实现：相关 `expose` 与两枚空 env 键已彻底移除（#39 / #40），8011 **不构成验收项**；worker/scheduler 健康判定唯一口径是 `--check` 进程探针。
2. doc 常驻进程（S5 的 `erp_doc`）与迁移矩阵第 4 行，待 S5/S7 交付后补测。
3. **S7 安全组仍为硬阻塞**：宿主 8010 已绑回环，但公网入口的宿主 nginx 与安全组规则未落地，禁止直接对外暴露 8010。
4. `web/` 首次进主干后只在 `e1926cf` 上验证过一次构建与产物前缀，**无跨版本回归**（无历史版本可对比）。
5. 迁移不可自动回退：回滚须按上一 sha 的镜像包重放，并先核对 `erp.alembic_version_erp` 与旧镜像兼容。

## v0.1.0-demo — 2026-11-11（版本 sha 已校正；当前阻塞，不可发布）

| 项 | 内容 |
|---|---|
| git 提交 | `cbf89a5`（完整 `cbf89a55edf0661bcf9ac8a7d9dd6436b995ca3d`，取值 `git ls-remote origin refs/heads/main`，2026-11-11 实测；113 个受版本管理的文件） |
| 校正说明 | 初次登记写成 `76b1dd4`（完整 `76b1dd43c421a78c57c452468a2cfcefc92e386e`），并误称「main 头」。核实：那是本地 HEAD，分支 `chore/gitignore-local-docs`，提交信息 `chore: .gitattributes 强制 LF 检出`，不含集成会话已合入的 5 个 PR；与 `origin/main` 无对应关系。 |
| 镜像 tag | `smart-erp/app:cbf89a55edf0661bcf9ac8a7d9dd6436b995ca3d`，同 sha 的 `connector-gw` / `web`。**doc 镜像在本提交里不存在**（`projects/smart-ERP/docker/` 只有 api / connector-gw / web 三个 Dockerfile），故本版不产出 `smart-erp/doc:<sha>`。 |
| 档位 | demo（该提交的 compose 服务全部 `profiles: ["demo"]`；full 档、doc、worker-agent/io、migrate 均未合入该提交） |
| 构建方式 | `ERP_IMAGE_TAG=cbf89a55edf0661bcf9ac8a7d9dd6436b995ca3d scripts/build-images.sh` → 镜像打进 `dist/erp-images-<sha>.tar.gz` + `sha256sum.txt`，不推镜像仓库。本机复验用只读导出构建（`git archive <sha>` → 临时目录 → `docker build`），未切换分支、未改动工作树。 |
| 发布方式 | `scripts/deploy-images.sh --tag cbf89a55...`（scp 镜像包 → 远端 `sha256sum -c` → `docker load` → `docker compose --profile demo up -d --remove-orphans`） |
| 验证项 | ① `python -m unittest discover -s tests/deploy` 全绿（29 用例）；② `--profile demo config --quiet` exit 0；③ **connector-gw 按该 sha 构建成功（87.2 MiB），但容器启动即崩**：`ModuleNotFoundError: No module named 'connectors'`，见下方阻塞项；④ doc 镜像不在该 sha，冒烟改为对工作树构建的 `smart-erp/doc:dev` 执行：`/healthz` 200、`/readyz` 200（`selfcheck_status=ok`、`selfcheck_doc_type=invoice`），uid 10001(app)、`--read-only` 起得来 |
| 回滚方式 | 用上一个 sha 的 `erp-images-<old-sha>.tar.gz` 重复发布流程：`ERP_IMAGE_TAG=<old-sha> docker compose --profile demo up -d`；数据库迁移不可自动回退，回滚前先确认 `alembic_version_erp` 与旧镜像兼容 |
| 阻塞项（必须修完再发） | `projects/smart-ERP/docker/connector-gw.Dockerfile` 的 `CMD ["python","-m","connectors.gateway",...]` 与包路径不符（代码在 `apps/connectors/gateway.py`，正确入口是 `python -m apps.connectors.gateway`）。按 `cbf89a5` 只读导出构建后 `docker run` 立即退出，日志即上述 ModuleNotFoundError。修复已在本地工作树，但**未合入该 sha**，故本版不发布。 |
| 已知缺口 | ① `core.runtime.worker` / `tools.health` 未交付（worker/scheduler 起不来，demo 档暂只验证 api/web/gw）；② doc 服务入口仍是临时健康壳，S5 交付 erp_doc 常驻进程后替换；③ full 档（doc / worker-agent / worker-io / migrate）仅存在于工作树，未合入本提交。 |

### 本轮实测记录（2026-11-11）

```
git ls-remote origin refs/heads/main
cbf89a55edf0661bcf9ac8a7d9dd6436b995ca3d    refs/heads/main
git rev-parse HEAD            → 76b1dd43c421a78c57c452468a2cfcefc92e386e（分支 chore/gitignore-local-docs）
git log --oneline -1          → 76b1dd4 chore: .gitattributes 强制 LF 检出

git archive cbf89a5 → %TEMP%\erp-cbf89a5（只读导出，不改工作树）
docker build -f .../connector-gw.Dockerfile -t smart-erp/connector-gw:cbf89a5... → 成功，87.2 MiB
python tests/deploy/smoke_connector_gw.py --no-build --image smart-erp/connector-gw:cbf89a5...
→ FAIL：容器已退出；日志 python: Error while finding module specification for 'connectors.gateway'
       (ModuleNotFoundError: No module named 'connectors')
```

doc 镜像冒烟（工作树，tag `smart-erp/doc:dev`）：`/healthz` `{"status":"ok"}`、`/readyz`
`{"status":"ready","parser":"docparse.rule.v1","selfcheck_status":"ok","selfcheck_doc_type":"invoice"}`，
`uid=10001(app)`，容器已 `docker rm -f` 清理。

### 待办（发布前）

1. 把 connector-gw 入口修复合入 main，并在 `RELEASE-CHECKLIST.md` 第 0 步用 `git ls-remote` 重新核对 sha；
2. 合入后再按新 sha 重跑：gw 冒烟、`build-images.sh`、`check_image_contents.py`；
3. full 档与 doc 镜像合入后，才登记 `v0.2.0-full`（届时 doc 冒烟用该 sha 的镜像重跑）。**2026-11-11 修正**：按上方「准入条款」第 1 条，v0.2.0 只发 demo 档；`v0.2.0-full` 在 B-S8-01 解除前不登记。

### 进展（2026-11-11 后续，未登记新版本）

- 阻塞项已解除：PR #13 热修 gw 入口，main `1c806fc`；用该 sha 构建 gw 镜像并起容器，`GET /healthz` = 200（`registered_actions=2`）。
- doc 镜像与 full 档已合入：PR #19，main `99afa3e`；`docparse.Dockerfile` 构建成功、容器 `GET /healthz` = 200；compose `--profile demo` 与 `--profile full` 正向解析均 exit 0。
- 积压清理完毕：S0/S1/S3S4/S7/S8/S5 六批各自独立 PR + 独立 worktree 验证后合并，main 现为 `60ee617`，全部 `tests/*` 目录在 3.12 下 exit 0。
- **仍未登记 v0.2.0**：清单第 3–7 步（`build-images.sh` 产出镜像包、镜像内容校验、远端 `deploy-images.sh`）尚未按该 sha 完整执行；按第 0 步新门禁，登记前须在该 sha 的只读检出里真跑构建与容器。
