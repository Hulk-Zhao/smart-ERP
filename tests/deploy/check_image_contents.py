#!/usr/bin/env python3
"""生产镜像内容校验：镜像里不许有 tests/ 与 _e2e 测试代码/路由。

为什么需要真跑容器而不是只读 Dockerfile：
  镜像从整个仓库 build，`tests` 是否真的被 .dockerignore 挡住、依赖包有没有顺带带进
  测试目录、`_e2e` 字符串有没有被装进站点包——只有进镜像里看才算数。
  S7 的约束：`/_e2e/*` 只允许存在于 tests/e2e 的测试支持层，生产镜像必须干净。

检查项（api 与 connector-gw 各一遍）：
  1. 任何位置存在名为 tests 的目录；
  2. 任何文件名含 `_e2e` 的文件；
  3. /app 与站点包内 .py 文件残留 `_e2e` 字符串；
  4. 任何 test_*.py 文件。

用法（仓库根目录）：
  python tests/deploy/check_image_contents.py              # 构建 + 校验两个镜像
  python tests/deploy/check_image_contents.py --no-build   # 校验现有镜像
  python tests/deploy/check_image_contents.py --api-image smart-erp/app:<sha> \
                                             --gw-image smart-erp/connector-gw:<sha>

退出码：0 干净 / 1 镜像里发现了测试内容 / 2 环境不可用（无 docker 或构建失败）。
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DOCKER_DIR = REPO_ROOT / "projects" / "smart-ERP" / "docker"
IMAGES = {
    "api": ("api.Dockerfile", "smart-erp/app:content-check"),
    "connector-gw": ("connector-gw.Dockerfile", "smart-erp/connector-gw:content-check"),
}

# 容器内检查脚本：只读扫描「我们自己装进去的东西」，命中即打印明细并以 3 退出。
# 范围刻意限定在我们自己的包与 /app：
#   第三方依赖自带它们自己的测试目录（certifi/tests、greenlet/tests、
#   sqlalchemy/testing/suite/test_*.py 等），那是上游发版内容，不属于本项目的
#   「构建上下文没收敛」，扫出来只会天天误报。
CHECK_SCRIPT = r"""
set -u
fail=0

pkg=$(python -c "import os, apps; print(os.path.dirname(apps.__file__))" 2>/dev/null)
if [ -z "$pkg" ]; then echo "FAIL_NO_APP_PACKAGE"; fail=1; fi
if [ -n "$pkg" ]; then echo "APP_PACKAGE:$pkg"; fi

# 1) 我们的包与 /app 下不得有 tests 目录
if [ -n "$pkg" ]; then
  dirs=$(find "$pkg" /app -type d -name tests -print 2>/dev/null)
  if [ -n "$dirs" ]; then echo "FAIL_TESTS_DIR:$dirs"; fail=1; fi

  # 2) 不得有 test_*.py 或 *_e2e* 文件
  files=$(find "$pkg" /app -type f \( -name 'test_*.py' -o -name '*_e2e*' \) -print 2>/dev/null)
  if [ -n "$files" ]; then echo "FAIL_TEST_FILES:$files"; fail=1; fi

  # 3) 生产代码里不得残留 _e2e 字符串（测试路由一旦被 import 就会命中）
  refs=$(grep -rl --include='*.py' '_e2e' /app "$pkg" 2>/dev/null)
  if [ -n "$refs" ]; then echo "FAIL_E2E_REF:$refs"; fail=1; fi
fi

# 4) 打包清单 RECORD 里不得有 tests/ 条目（防止被 setuptools 打进 wheel）
record=$(ls /usr/local/lib/python3.12/site-packages/smart_erp-*.dist-info/RECORD 2>/dev/null)
if [ -n "$record" ]; then
  if grep -Eq '(^|/)tests/' "$record"; then echo "FAIL_RECORD_TESTS:$record"; fail=1; fi
else
  echo "WARN_NO_DIST_INFO_RECORD"
fi

if [ "$fail" -eq 0 ]; then echo "IMAGE_CLEAN"; exit 0; fi
exit 3
"""


def _run(cmd: list[str], *, timeout: int = 180) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)


def _build(image: str, dockerfile: Path) -> int:
    if not dockerfile.is_file():
        print(f"[image-check] FAIL: 缺少 {dockerfile}", file=sys.stderr)
        return 2
    print(f"[image-check] 构建 {image}（{dockerfile.name}）…")
    result = _run(
        ["docker", "build", "--file", str(dockerfile), "--tag", image, str(REPO_ROOT)],
        timeout=1800,
    )
    if result.returncode != 0:
        print(result.stdout[-1500:], file=sys.stderr)
        print(result.stderr[-1500:], file=sys.stderr)
        print("[image-check] FAIL: 构建失败", file=sys.stderr)
        return 2
    return 0


def _inspect(image: str) -> int:
    size = _run(["docker", "image", "inspect", "-f", "{{.Size}}", image], timeout=60)
    if size.returncode != 0:
        print(f"[image-check] FAIL: 镜像 {image} 不存在", file=sys.stderr)
        return 2
    print(f"[image-check] {image} 大小 {int(size.stdout.strip()) / 1024 / 1024:.1f} MiB")

    # 覆盖 entrypoint：镜像默认 CMD 是服务进程，这里只跑一次只读扫描
    result = _run(
        ["docker", "run", "--rm", "--entrypoint", "sh", image, "-c", CHECK_SCRIPT],
        timeout=180,
    )
    output = ((result.stdout or "") + (result.stderr or "")).strip()
    if output:
        for line in output.splitlines():
            print(f"  {line}")
    if result.returncode == 0 and "IMAGE_CLEAN" in output:
        print(f"[image-check] PASS: {image} 不含 tests/ 与 _e2e")
        return 0
    if result.returncode == 3:
        print(f"[image-check] FAIL: {image} 里发现测试内容（见上）", file=sys.stderr)
        return 1
    print(f"[image-check] FAIL: 扫描未正常完成（exit={result.returncode}）", file=sys.stderr)
    return 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="生产镜像内容校验（tests/ 与 _e2e）")
    parser.add_argument("--api-image", default=IMAGES["api"][1])
    parser.add_argument("--gw-image", default=IMAGES["connector-gw"][1])
    parser.add_argument("--no-build", action="store_true", help="跳过构建，直接校验现有镜像")
    args = parser.parse_args(argv)

    version = _run(["docker", "version", "--format", "{{.Server.Version}}"], timeout=60)
    if version.returncode != 0:
        print(f"[image-check] FAIL: docker 不可用 {version.stderr.strip()}", file=sys.stderr)
        return 2
    print(f"[image-check] docker server {version.stdout.strip()}")

    targets = [("api", args.api_image), ("connector-gw", args.gw_image)]
    if not args.no_build:
        for name, image in targets:
            code = _build(image, DOCKER_DIR / IMAGES[name][0])
            if code != 0:
                return code

    results = []
    for name, image in targets:
        print(f"[image-check] --- {name} ---")
        results.append(_inspect(image))
    if all(code == 0 for code in results):
        print("[image-check] 全部通过：两个镜像均不含 tests/ 与 _e2e")
        return 0
    return max(results)


if __name__ == "__main__":
    raise SystemExit(main())
