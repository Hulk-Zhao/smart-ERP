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
#   scripts/build-images.sh --tag <40位sha> --allow-dirty   # 仅限冒烟/演练
#
# 工作树门禁：构建前跑 `git status --porcelain`，非空直接拒绝（tag 无法唯一标识内容）。
#   发布构建请在目标 commit 的独立 worktree 里执行——脚本按自身路径定位仓库根：
#     git worktree add /tmp/smart-erp-build <40位sha>
#     /tmp/smart-erp-build/scripts/build-images.sh --tag <40位sha>
#     git worktree remove --force /tmp/smart-erp-build
#   确认只用于本地验证时才加 --allow-dirty：tag 自动追加 "-dirty"，deploy-images.sh
#   会因 tag 不是 40 位 sha 拒绝下发。
#
# 退出码：0 成功；2 参数/前置条件不满足（含工作树不干净）；其它为 docker 自身错误。
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
ALLOW_DIRTY=0

usage() {
  cat <<'EOF'
用法: build-images.sh [选项]
  -t, --tag <sha>       完整 40 位 git sha（也可用环境变量 ERP_IMAGE_TAG）
      --out <dir>       产物目录，默认 <repo>/dist
      --platform <p>    目标平台，默认 linux/amd64（必须与服务器架构一致）
      --no-cache        关闭 docker 构建缓存
      --allow-dirty     允许脏工作树构建（tag 追加 -dirty，不可用于发布登记）
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
    --allow-dirty) ALLOW_DIRTY=1; shift ;;
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

# ---- 1.5 工作树洁净门禁（tag 必须唯一标识镜像内容）-------------------------
DIRTY_LIST=""
if git -C "${REPO_ROOT}" rev-parse --git-dir >/dev/null 2>&1; then
  DIRTY_LIST="$(git -C "${REPO_ROOT}" status --porcelain)"
else
  (( ALLOW_DIRTY )) || die "不是 git 仓库，无法校验工作树；发布构建必须在 git worktree 中进行，或显式 --allow-dirty"
  DIRTY_LIST="(不是 git 仓库，无法列出脏文件)"
fi

if [[ -n "${DIRTY_LIST}" ]]; then
  if (( ALLOW_DIRTY )); then
    TAG="${TAG}-dirty"
    warn "工作树不干净，按 --allow-dirty 继续；tag 改为 ${TAG}（不可用于发布登记）"
    printf '%s\n' "${DIRTY_LIST}" >&2
  else
    {
      echo "工作树不干净，拒绝构建：tag=${TAG} 无法唯一标识镜像内容。"
      echo "git status --porcelain："
      printf '%s\n' "${DIRTY_LIST}"
      echo
      echo "处理方式（择一）："
      echo "  1) 发布构建：在目标 commit 的独立 worktree 里跑本脚本（脚本按自身路径定位仓库根）："
      echo "       git worktree add /tmp/smart-erp-build <40位sha>"
      echo "       /tmp/smart-erp-build/scripts/build-images.sh --tag <40位sha>"
      echo "       git worktree remove --force /tmp/smart-erp-build"
      echo "  2) 冒烟/演练：确认产物只用于本地验证后加 --allow-dirty（tag 自动加 -dirty）。"
    } >&2
    exit 2
  fi
else
  echo "== 工作树洁净：git status --porcelain 为空 =="
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
  # Git Bash（MSYS）会把 `--build-arg ERP_WEB_BASE=/` 的 `/` 改写成 Git 安装根目录，
  # 产物 index.html 里就会出现 /Program Files/Git/assets/...；只对 build 进程关闭转换。
  # 关掉转换后 docker 不再认 /e/... 这类 POSIX 路径，Dockerfile 与 context 需转成 Windows 路径。
  local dockerfile_path="${DOCKER_DIR}/${dockerfile}" context_path="${REPO_ROOT}"
  if command -v cygpath >/dev/null 2>&1; then
    dockerfile_path="$(cygpath -w "${dockerfile_path}")"
    context_path="$(cygpath -w "${context_path}")"
  fi
  local -a cmd=(env MSYS_NO_PATHCONV=1 docker build --file "${dockerfile_path}"
                --tag "smart-erp/${name}:${TAG}")
  [[ -n "$PLATFORM" ]] && cmd+=(--platform "${PLATFORM}")
  (( NO_CACHE )) && cmd+=(--no-cache)
  cmd+=("$@")
  cmd+=("${context_path}")

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
