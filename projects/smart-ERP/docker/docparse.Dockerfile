# =============================================================================
# smart-erp/doc —— 文档解析镜像（full 档；demo 档不启动，避免占 3G 预算）
# -----------------------------------------------------------------------------
# 依据 docs/product/tech/01-技术架构.md §1A.2（镜像共 4 个，doc 单列便于私有化
# 分发与低频更新）、§1A.3（full 档 doc 3G / start_period 300s）。
# 约束与另外三个镜像一致：多阶段、slim 基础镜像、非 root(10001)、不装编译器。
#
# 入口：`python -m apps.docparse`（apps/docparse/service.py），提供 /healthz 与
# /readyz（真跑一次解析管线）。队列消费与文档接入 API 待后续任务补齐，补齐后只换
# service.py 内实现，compose 的 doc.command 与 tests/deploy 断言保持同一入口。
#
# 构建（context 必须是仓库根）：
#   docker build -f projects/smart-ERP/docker/docparse.Dockerfile \
#                -t smart-erp/doc:$(git rev-parse HEAD) .
# =============================================================================
ARG PYTHON_IMAGE=python:3.12-slim-bookworm

FROM ${PYTHON_IMAGE} AS builder

ARG PIP_INDEX_URL=https://mirrors.cloud.tencent.com/pypi/simple
ENV PIP_INDEX_URL=${PIP_INDEX_URL} \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

# 同 api.Dockerfile：不预装 build-essential，依赖必须有 cp312 manylinux wheel。
# 将来接 PaddleOCR/PyMuPDF 时若出现无 wheel 的包，这里会以 gcc 报错形式暴露。
WORKDIR /src
COPY . .

# 必须装 [pdf] extra：apps/docparse/extractors/pdf.py 依赖 pypdfium2（BSD-3-Clause/Apache-2.0，
# 刻意不用 AGPL 的 PyMuPDF）。只装基础依赖会让镜像里的 PDF 解析静默降级成「缺 PDF 支持」。
RUN python -m pip wheel --wheel-dir /wheels ".[pdf]"

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
 && rm -rf /wheels /tmp/pycache

WORKDIR /app
RUN mkdir -p /app/config /data/objects && chown -R app:app /app /data

USER app

EXPOSE 8020

CMD ["python", "-m", "apps.docparse", "--port=8020"]
