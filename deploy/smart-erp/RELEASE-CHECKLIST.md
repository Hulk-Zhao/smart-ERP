# 发布前检查清单（smart-erp demo / full 档）

每次发版按顺序执行，任一步非 0 就不发。全部在仓库根目录运行。
**档位口径**：只有 `demo` / `full` 两个档位，`--profile` 只接受这两个取值，**不存在 ops 档**（T802 定稿，2026-11-11）。依据是 `docker-compose.yml` 头注释与 `tests/deploy/test_compose_deploy.py::test_profiles_are_exactly_demo_and_full` 的断言，不依赖口头约定。
发版完成后在 [RELEASES.md](./RELEASES.md) 追加一条登记（tag、验证项、回滚 sha）。

| # | 命令 | 通过标准 |
|---|---|---|
| 0 | `git ls-remote origin refs/heads/main`，并用该 sha 只读检出（`git worktree add --detach <dir> <sha>` 或 `git archive <sha>`） | **硬门禁**：① 输出的 sha 必须与 RELEASES.md 本次登记的版本 sha 完全一致；② **构建来源必须是该 sha 的检出**，在该检出里执行第 3–5 步并真跑容器拿到 `/healthz` 200；本地 `git rev-parse HEAD` 与任何未提交工作树都不作数。任一不满足立即停止发布 |
| 1 | `python -m unittest discover -s tests/deploy` | 全绿（离线静态断言：服务清单/内存预算/环境变量/探针路径/gw 入口） |
| 2 | `uv lock --check`（有 uv.lock 时） | exit 0，锁文件与 pyproject 一致 |
| 3 | `ERP_IMAGE_TAG=<40 位 sha> scripts/build-images.sh` | 产出 `dist/erp-images-<sha>.tar.gz` 与 `sha256sum.txt` |
| 4 | `python tests/deploy/smoke_connector_gw.py` | 容器起来后 `/healthz` 200，脚本自动清理容器 |
| 5 | `python tests/deploy/check_image_contents.py` | api 与 connector-gw 均输出 `IMAGE_CLEAN`（无 `tests/`、无 `_e2e`） |
| 6 | `ERP_IMAGE_TAG=<sha> docker compose --profile demo config --quiet` 与 `--profile full config --quiet`（在 `deploy/smart-erp/`） | 两档都 exit 0 |
| 7 | `scripts/deploy-images.sh` | 远端 `sha256sum -c` 通过、`/healthz` 200 |
| 8 | full 档额外：`docker run -d --name erp_doc_smoke --read-only --tmpfs /tmp:size=64m smart-erp/doc:<sha>` 后容器内请求 `/healthz` 与 `/readyz` | 均 200，`docker rm -f erp_doc_smoke` 清理 |

第 4、5 步必须真跑容器，不要用「Dockerfile 看起来没 COPY tests」代替：
历史上有过入口写错（`connectors.gateway`）与运行期依赖漏装（`httpx`）两次
「Dockerfile 看着对、容器起不来」的事故。

---

## 合并前自证：批量机器修改（硬性，与发版无关）

任何一次**批量机器修改**（`ruff --fix` 自动修复、格式化器、批量重命名、批量字符串替换）在合并前，必须在 PR 正文给出「仅等价变换」的逐类证明；缺一不得合并。这不是发版时才做的说明，是**合并前的清单项**。

| 类别 | 必答内容 |
|---|---|
| `except` 收窄 / 异常处理 | 有无把 `except Exception`（或裸 `except`）收窄成具体异常？有无改变捕获范围导致新增冒泡？逐处列出 `文件:行` 与结论 |
| 布尔短路与求值顺序 | 有无合并嵌套 `if`、化简 `and`/`or`、改变两侧求值先后（带副作用或可能抛异常的表达式）？逐处按真值表说明等价 |
| 缓存装饰器 | 有无新增/删除 `lru_cache`、`cache`、`cached_property`？有无改变函数被调用的次数（进而改变副作用次数）？ |
| 真值判断 | 有无把 `x == True/False`、`len(x) > 0`、与 `None`/`0`/`""` 的比较改写成 `if x`（空容器 / 0 / 空串会走另一分支）？ |

同时必须写明：

1. 自动修了多少处、手修多少处；**手修逐处列出文件与理由**，不能只写「已手修」。
2. `git diff --stat` 的文件数与增删行数，与 PR 声称的修改范围一致（禁止夹带无关文件）。
3. **该提交的完整 sha 必须写入仓库根 `.git-blame-ignore-revs`**，并在 PR 正文给出该行内容；否则一次性格式改动会把功能历史全部压到这一条上，`git blame` 无法追溯。
4. 验证：修改前后阻断档（`ruff check --select E9,F63,F7,F82`）均为 0；受影响目录的测试跑过且 exit 0。

反例（2026-11-11）：ruff 205 项清零一次性改 75 个文件（+319/−332），其中 17 处为手修；合并前没有先给自证，是合并后补做的。自证结论为「无 `except` 收窄、无缓存装饰器改动、无布尔求值顺序变化，真值判断按真值表等价」，据此判定可合并；该 sha `42d72fb` 已写入 `.git-blame-ignore-revs`。本条目即由这次事故固化。

