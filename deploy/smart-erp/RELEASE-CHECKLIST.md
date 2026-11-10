# 发布前检查清单（smart-erp demo / full 档）

每次发版按顺序执行，任一步非 0 就不发。全部在仓库根目录运行。
发版完成后在 [RELEASES.md](./RELEASES.md) 追加一条登记（tag、验证项、回滚 sha）。

| # | 命令 | 通过标准 |
|---|---|---|
| 0 | `git ls-remote origin refs/heads/main` | **硬门禁**：输出的 sha 必须与 RELEASES.md 本次登记的版本 sha 完全一致；不一致立即停止发布（本地 `git rev-parse HEAD` 不作数，它只是本地检出） |
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
