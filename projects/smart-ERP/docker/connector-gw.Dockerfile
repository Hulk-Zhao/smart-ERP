# =============================================================================
# smart-erp/connector-gw —— 连接器网关（MCP Server + 限流 + 拦截记录 + 沙箱）
# -----------------------------------------------------------------------------
# 依据 §1A.2：只装 SDK + 连接器依赖，**不装** PG 客户端、OCR、numpy/pandas；目标 < 180MB。
# 最小权限：compose 里 read_only: true + tmpfs /tmp；不挂数据库网络（§1A.4）。
#
# 构建：
#   docker build -f projects/smart-ERP/docker/connector-gw.Dockerfile \
#                -t smart-erp/connector-gw:$(git rev-parse HEAD) .
#
# 实测（2026-11-10）：构建通过，镜像 91.4MB，USER=app(10001)。
# 入口已按 S1 交付物落地为 apps.connectors.gateway（`python -m apps.connectors.gateway`）：
#   /healthz 不查依赖、/readyz 查下游（不可用返 503），端口读 ERP_CONNECTOR_GW_PORT（默认 8012）。
# 启动冒烟（2026-11-11 实测）：tests/deploy/smoke_connector_gw.py → 镜像 88.3MiB，
#   /healthz 200（3.3s），u8 连接器卡片与 allowlist hash 正常返回。改动入口后请复跑该脚本。
#   同步位置：deploy/smart-erp/docker-compose.yml 的 connector-gw.command，
#   以及 tests/deploy 的回归断言——三处必须一起改。
# =============================================================================
ARG PYTHON_IMAGE=python:3.12-slim-bookworm

FROM ${PYTHON_IMAGE} AS builder

ARG PIP_INDEX_URL=https://mirrors.cloud.tencent.com/pypi/simple
ENV PIP_INDEX_URL=${PIP_INDEX_URL} \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

# 同 api.Dockerfile：不预装编译器，依赖必须有 manylinux wheel（见该文件说明）

WORKDIR /src
COPY . .

RUN python -m pip wheel --wheel-dir /wheels .

FROM ${PYTHON_IMAGE} AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPYCACHEPREFIX=/tmp/pycache \
    TZ=Asia/Shanghai \
    LANG=C.UTF-8

RUN groupadd --gid 10001 app \
 && useradd --uid 10001 --gid app --create-home --shell /usr/sbin/nologin app

COPY --from=builder /wheels /wheels
# /tmp/pycache 是 PYTHONPYCACHEPREFIX 的落点：装依赖时会把 .pyc（含第三方包自带的
# test_*.pyc）写进去，运行期 /tmp 被 tmpfs 覆盖用不到，装完即删，避免白占镜像层。
RUN python -m pip install --no-index --find-links=/wheels --no-cache-dir /wheels/*.whl \
 && rm -rf /wheels /tmp/pycache

WORKDIR /app
# 网关根文件系统只读（compose read_only），只保留 /tmp 供 tmpfs 挂载
RUN mkdir -p /app/config && chown -R app:app /app/config

USER app

EXPOSE 8012

CMD ["python", "-m", "apps.connectors.gateway"]
