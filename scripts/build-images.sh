#!/usr/bin/env bash
# =============================================================================
# smart-ERP 镜像构建与离线打包（构建机上执行，服务器不编译）
# -----------------------------------------------------------------------------
# 依据：01 §1A.2（构建策略）/ §1A.10（发布序列）；C12（镜像与发布契约）
#   1. tag 必须是完整 40 位 git sha（ERP_IMAGE_TAG），禁止 latest 承担发布语义；
#   2. 四个镜像（app / connector-gw / web / doc）打进**同一个** tar.gz，
#      配 sha256sum.txt，一次 scp 传完；doc 只在 full 档启动，但一次打包省得二次发版；
#   3. 不建镜像仓库。
#
# 用法：
#   ERP_IMAGE_TAG=$(git rev-parse HEAD) scripts/build-images.sh
#   scripts/build-images.sh --tag <40位sha> --dry-run
#   scripts/build-images.sh --no-cache --out /tmp/erp-dist
#
# 退出码：0 成功；2 参数/前置条件不满足；其它为 docker 自身错误。
# =============================================================================
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DIST_DIR="${ERP_DIST_DIR:-${REPO_ROOT}/dist}"
DOCKER_DIR="${REPO_ROOT}/projects/smart-ERP/docker"
TAG="${ERP_IMAGE_TAG:-}"
PLATFORM="${ERP_BUILD_PLATFORM:-linux/amd64}"
WEB_BASE="${ERP_BUILD_WEB_BASE:-/demo/smart-erp/}"
DRY_RUN=0
NO_CACHE=0

usage() {
  cat <<'EOF'
用法: build-images.sh [选项]
  -t, --tag <sha>       完整 40 位 git sha（也可用环境变量 ERP_IMAGE_TAG）
      --out <dir>       产物目录，默认 <repo>/dist
      --platform <p>    目标平台，默认 linux/amd64（必须与服务器架构一致）
      --no-cache        关闭 docker 构建缓存
      --dry-run         只打印将执行的命令，不真正构建
  -h, --help            显示本帮助
EOF
}

die() { echo "ERROR: $*" >&2; exit 2; }
warn() { echo "WARN: $*" >&2; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    -t|--tag)     TAG="${2:-}"; shift 2 ;;
    --out)        DIST_DIR="${2:-}"; shift 2 ;;
    --platform)   PLATFORM="${2:-}"; shift 2 ;;
    --no-cache)   NO_CACHE=1; shift ;;
    --dry-run)    DRY_RUN=1; shift ;;
    -h|--help)    usage; exit 0 ;;
    *)            usage; die "未知参数: $1" ;;
  esac
done

# ---- 1. tag 规则（C12）-----------------------------------------------------
[[ -n "$TAG" ]] || die "缺少 ERP_IMAGE_TAG：必须是完整 40 位 git sha（git rev-parse HEAD）"
if [[ ! "$TAG" =~ ^[0-9a-f]{40}$ ]]; then
  die "ERP_IMAGE_TAG='${TAG}' 不合法：必须是完整 40 位小写十六进制 sha，禁止 latest / 短 sha"
fi

# ---- 2. 前置条件（源码与依赖清单由其它流负责，这里只做可读的失败）----------
MISSING=()
[[ -f "${REPO_ROOT}/pyproject.toml" ]]            || MISSING+=("pyproject.toml（S0 维护，Python 依赖清单）")
[[ -d "${REPO_ROOT}/apps" ]]                      || MISSING+=("apps/（S0：FastAPI 入口）")
[[ -f "${REPO_ROOT}/web/package.json" ]]          || MISSING+=("web/package.json（S6：前端）")
[[ -f "${REPO_ROOT}/web/package-lock.json" ]]     || MISSING+=("web/package-lock.json（S6：npm ci 需要）")
for f in api.Dockerfile connector-gw.Dockerfile docparse.Dockerfile web.Dockerfile; do
  [[ -f "${DOCKER_DIR}/${f}" ]] || MISSING+=("projects/smart-ERP/docker/${f}")
done

if (( ${#MISSING[@]} > 0 )); then
  echo "以下构建输入尚不存在：" >&2
  printf '  - %s\n' "${MISSING[@]}" >&2
  if (( DRY_RUN )); then
    warn "--dry-run：继续打印计划命令，真实构建一定会失败"
  else
    die "构建输入不全，拒绝构建（不要用「先构建一半再补源码」的方式发版）"
  fi
fi

# ---- 3. 构建 --------------------------------------------------------------
build_one() {
  local name="$1" dockerfile="$2"; shift 2
  local -a cmd=(docker build --file "${DOCKER_DIR}/${dockerfile}"
                --tag "smart-erp/${name}:${TAG}")
  [[ -n "$PLATFORM" ]] && cmd+=(--platform "${PLATFORM}")
  (( NO_CACHE )) && cmd+=(--no-cache)
  cmd+=("$@")
  cmd+=("${REPO_ROOT}")

  echo "+ ${cmd[*]}"
  (( DRY_RUN )) || "${cmd[@]}"
}

echo "== smart-ERP 镜像构建：tag=${TAG} platform=${PLATFORM} =="
build_one app          api.Dockerfile
build_one connector-gw connector-gw.Dockerfile
build_one doc          docparse.Dockerfile
# 基础镜像允许按环境覆盖（内网/镜像站拉不到 nginx:1.27-alpine 时用它钉住可拉到的版本）
build_one web          web.Dockerfile \
  --build-arg "ERP_WEB_BASE=${WEB_BASE}" \
  --build-arg "NGINX_IMAGE=${ERP_NGINX_IMAGE:-nginx:1.27-alpine}"

# ---- 4. docker save + gzip + 校验文件 ------------------------------------
IMAGE_LIST=(
  "smart-erp/app:${TAG}"
  "smart-erp/connector-gw:${TAG}"
  "smart-erp/doc:${TAG}"
  "smart-erp/web:${TAG}"
)
TAR_NAME="erp-images-${TAG}.tar.gz"

SAVE_CMD=(docker save "${IMAGE_LIST[@]}")
SHA_CMD=(sh -c "cd '${DIST_DIR}' && sha256sum '${TAR_NAME}' > sha256sum.txt && cat sha256sum.txt")

echo "+ mkdir -p ${DIST_DIR}"
(( DRY_RUN )) || mkdir -p "${DIST_DIR}"

echo "+ ${SAVE_CMD[*]} | gzip -9 > ${DIST_DIR}/${TAR_NAME}"
if (( ! DRY_RUN )); then
  "${SAVE_CMD[@]}" | gzip -9 > "${DIST_DIR}/${TAR_NAME}"
fi

echo "+ ${SHA_CMD[*]}"
if (( ! DRY_RUN )); then
  sh -c "cd '${DIST_DIR}' && sha256sum '${TAR_NAME}' > sha256sum.txt && cat sha256sum.txt"
  echo "产物：${DIST_DIR}/${TAR_NAME}"
  echo "校验：${DIST_DIR}/sha256sum.txt"
  echo "下一步：scripts/deploy-images.sh --tag ${TAG}"
else
  echo "[dry-run] 未生成任何产物"
fi
