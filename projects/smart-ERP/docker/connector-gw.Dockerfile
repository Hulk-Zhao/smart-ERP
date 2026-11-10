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
# 未实测说明：同 api.Dockerfile —— 镜像构建依赖仓库根 pyproject.toml 与 apps/connectors/ 包。
# 入口固定为 apps.connectors.gateway（包入口由 tests/deploy 的 entrypoint 用例锁定）。
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
RUN python -m pip install --no-index --find-links=/wheels --no-cache-dir /wheels/*.whl \
 && rm -rf /wheels

WORKDIR /app
# 网关根文件系统只读（compose read_only），只保留 /tmp 供 tmpfs 挂载
RUN mkdir -p /app/config && chown -R app:app /app/config

USER app

EXPOSE 8012

CMD ["python", "-m", "apps.connectors.gateway", "--host=0.0.0.0", "--port=8012"]
