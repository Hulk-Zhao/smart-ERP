# =============================================================================
# smart-erp/app —— api / worker / scheduler 三个进程共用的业务镜像
# -----------------------------------------------------------------------------
# 依据 docs/product/tech/01-技术架构.md §1A.2：多阶段 + slim 基础镜像 + 非 root(10001)
# 目标体积 < 220MB。
#
# 构建（context 必须是仓库根，依赖清单 pyproject.toml 由 S0 维护）：
#   docker build -f projects/smart-ERP/docker/api.Dockerfile \
#                -t smart-erp/app:$(git rev-parse HEAD) .
#
# 构建输入：pyproject.toml、apps/、core/、tools/、alembic.ini、db/migrations
#   （仓库根为构建上下文）；缺任一项 build-images.sh 会先报错，不会构建出一半。
# =============================================================================
ARG PYTHON_IMAGE=python:3.12-slim-bookworm

# ---- 阶段 1：builder（编译期依赖只存在于这里，不进运行镜像）----
FROM ${PYTHON_IMAGE} AS builder

ARG PIP_INDEX_URL=https://mirrors.cloud.tencent.com/pypi/simple
ENV PIP_INDEX_URL=${PIP_INDEX_URL} \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

# 不装 build-essential：fastapi/uvicorn/pydantic/sqlalchemy/asyncpg/alembic 都有
# cp312 manylinux wheel，装了也只是多 200MB 与一次 apt 失败风险（现场网络受限）。
# 若将来某个依赖没有 wheel，这里会以 "error: command 'gcc' failed" 明确失败，
# 届时再加 `apt-get install build-essential`（并配国内 apt 源），不要现在预装。

WORKDIR /src
# 依赖层缓存取舍：这里整包 COPY 后一次性 wheel，保证「构建一定成功」优先于
# 「只改一行业务代码不重装依赖」。S0 落地 requirements lock 后可改为两段式。
COPY . .

RUN python -m pip wheel --wheel-dir /wheels .

# ---- 阶段 2：runtime（无编译器、无 apt 缓存、非 root）----
FROM ${PYTHON_IMAGE} AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONFAULTHANDLER=1 \
    TZ=Asia/Shanghai \
    LANG=C.UTF-8

# uid/gid 固定 10001，与 compose / 宿主机备份脚本的权限假设一致
RUN groupadd --gid 10001 app \
 && useradd --uid 10001 --gid app --create-home --shell /usr/sbin/nologin app

COPY --from=builder /wheels /wheels
RUN python -m pip install --no-index --find-links=/wheels --no-cache-dir /wheels/*.whl \
 && rm -rf /wheels

WORKDIR /app
# 迁移资产随镜像分发：compose 的 migrate 服务在容器内执行
# `alembic -c /app/alembic.ini upgrade head`，服务器上没有源码目录可挂，必须打进镜像。
# alembic.ini 的 script_location=%(here)s/db/migrations，两者保持 /app 下的相对位置。
COPY alembic.ini /app/alembic.ini
COPY db/migrations /app/db/migrations

# 运行期可写目录只有卷挂载点；其余保持镜像只读语义（compose 里 gw 显式 read_only）
RUN mkdir -p /app/logs /data/objects /app/config \
 && chown -R app:app /app /data

USER app

EXPOSE 8010

# 健康检查由 compose 声明（§1A.6 统一 place，避免镜像与 compose 两处真源）
# 入口模块以 apps/main.py 为准（S0 已落地）：apps.main:app
CMD ["uvicorn", "apps.main:app", "--host=0.0.0.0", "--port=8010", "--workers=1", "--limit-concurrency=64"]
