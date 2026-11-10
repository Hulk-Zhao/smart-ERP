# 发布记录（smart-erp）

> 依据 C12：每次发布登记一条；镜像 tag 必须是完整 40 位 git sha，禁止 latest。
> 回滚以「上一个 sha」为单位，镜像包不覆写、不删。
> **版本 sha 的唯一取值方式**：发布时刻的 `git ls-remote origin refs/heads/main`。
> `git rev-parse HEAD` 只是本地检出的分支头，不能当作 main（第一次登记就错在这里）。

## 现状 — 2026-11-11：v0.2.0 因 P0 跨租户越权**冻结，未登记**

S4 跨租户扫描实测到 13 处越权：租户 A 能列出/读取租户 B 的工单，能驳回、取消别家工单，能在别家工单落审批记录，能导出别家审计 CSV。根因：`_require_ticket` 与 `TicketRepository.list` 缺少 `tenant_id` 谓词。**17 条 API 路径中 10 条受影响。**

| 项 | 内容 |
|---|---|
| 冻结范围 | v0.2.0 **全部档位**（demo / full）；`RELEASE-CHECKLIST.md` 第 3–7 步的登记动作一律暂停 |
| 解除条件（缺一不可） | ① S4 修复合并进 main；② 13 条越权用例全部转绿；③ 全量 `tests/*` 在 Python 3.12 下复跑 exit 0 |
| 留底 | `docs/product/tech/dev-ledger.json` 已追加 `阻塞` 记录 |
| 冻结期间可做 | 与发版无关的代码/文档工作（不涉及清单第 3–7 步） |

## v0.2.0 准入条款（2026-11-11 定，硬性）

| # | 条款 | 判定方式 |
|---|---|---|
| 1 | **只发 demo 档**：api + connector-gw + web 三个镜像；full 档不发 | 登记条「档位」字段必须是 demo；出现 full 服务或 full 档镜像的登记视为违规 |
| 2 | **full 档在 B-S8-01 解除前不具备发版条件**（`core/runtime/*` 与 `tools/health.py` 缺失、打包 `include` 未含 `core*`，4 个服务起不来） | 依据只有一个：S8 的 `FULL-READINESS.md`（S8 交付物，随 S8 合入主干）；集成会话不得自行判断「差不多了」 |
| 3 | S6 合并、S8 真 build 出 web 镜像并拿到 `/healthz` 200 后，才可走清单第 3–7 步 | 三个镜像包齐全 + 容器实测 200，缺一不登记 |

v0.2.0 登记口径（届时按此填写登记条）：demo 档；含 `web/` 与 `contracts/api/` 契约产物；**不含** `apps/api/` 计量端点完整批次；**不含** S2 对账改进；full 档未就绪；`worker` / `scheduler` 未交付；未执行远端部署。

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
