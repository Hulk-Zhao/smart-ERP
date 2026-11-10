#!/usr/bin/env python3
"""connector-gw 镜像启动冒烟：build → run → 容器内 GET /healthz 必须 200 → 清理容器。

为什么单独一个脚本而不是 unittest 用例：
  tests/deploy 的其它用例都是「离线静态断言」，一条命令秒级跑完，可放 CI 门禁；
  冒烟要真构建镜像（本机实测数分钟）并起容器，CI 明确不构建镜像
  （见 .github/workflows/smart-erp.yml 头部说明），所以做成发布前手动执行的脚本。
  文件不以 test_ 开头，不会被 unittest / pytest 意外收集。

验证的正是静态断言锁不住的那一步：镜像内 `python -m apps.connectors.gateway`
能不能真的起监听 8012（历史上这里写错过入口，容器一启动就 ModuleNotFoundError）。

用法（在仓库根目录）：
  python tests/deploy/smoke_connector_gw.py                 # 构建 + 启动 + 探测 + 清理
  python tests/deploy/smoke_connector_gw.py --no-build      # 复用已有镜像
  python tests/deploy/smoke_connector_gw.py --keep          # 失败时保留容器便于排障
  python tests/deploy/smoke_connector_gw.py --image smart-erp/connector-gw:<sha>

退出码：0 通过 / 1 冒烟失败 / 2 环境不可用（没有 docker 或构建失败）。
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DOCKERFILE = REPO_ROOT / "projects" / "smart-ERP" / "docker" / "connector-gw.Dockerfile"
CONTAINER_NAME = "erp_gw_smoke"
DEFAULT_IMAGE = "smart-erp/connector-gw:smoke"
HEALTH_PORT = 8012
HEALTH_URL = f"http://127.0.0.1:{HEALTH_PORT}/healthz"

# 容器内探针：不依赖 curl/wget，镜像本身就是 python
PROBE_CODE = (
    "import urllib.request as u;"
    f"r=u.urlopen('{HEALTH_URL}', timeout=3);"
    "print(r.status);"
    "print(r.read().decode('utf-8')[:600])"
)


def _run(cmd: list[str], *, timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)


def _fail(message: str, *, code: int = 1) -> int:
    print(f"[smoke] FAIL: {message}", file=sys.stderr)
    return code


def _remove_container() -> None:
    # -f：容器可能还在跑；不存在时 docker 会返回非 0，这里刻意忽略
    _run(["docker", "rm", "-f", CONTAINER_NAME], timeout=60)


def _container_running() -> bool:
    result = _run(
        ["docker", "inspect", "-f", "{{.State.Running}}", CONTAINER_NAME], timeout=30
    )
    return result.returncode == 0 and result.stdout.strip() == "true"


def _dump_logs() -> None:
    logs = _run(["docker", "logs", "--tail", "40", CONTAINER_NAME], timeout=60)
    output = (logs.stdout or "") + (logs.stderr or "")
    if output.strip():
        print("[smoke] 容器日志（末尾 40 行）：")
        print(output.rstrip())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="connector-gw 镜像启动冒烟")
    parser.add_argument("--image", default=DEFAULT_IMAGE, help=f"镜像 tag（默认 {DEFAULT_IMAGE}）")
    parser.add_argument("--no-build", action="store_true", help="跳过构建，直接用现有镜像")
    parser.add_argument("--keep", action="store_true", help="不删容器（失败排障用）")
    parser.add_argument("--timeout", type=int, default=90, help="等待 /healthz 就绪的秒数")
    args = parser.parse_args(argv)

    version = _run(["docker", "version", "--format", "{{.Server.Version}}"], timeout=60)
    if version.returncode != 0:
        return _fail(f"docker 不可用：{(version.stderr or version.stdout).strip()}", code=2)
    print(f"[smoke] docker server {version.stdout.strip()}")

    if not args.no_build:
        if not DOCKERFILE.is_file():
            return _fail(f"缺少 Dockerfile: {DOCKERFILE}", code=2)
        print(f"[smoke] 构建 {args.image}（{DOCKERFILE.name}）…")
        started = time.monotonic()
        build = _run(
            [
                "docker", "build",
                "--file", str(DOCKERFILE),
                "--tag", args.image,
                str(REPO_ROOT),
            ],
            timeout=1800,
        )
        if build.returncode != 0:
            print(build.stdout[-2000:], file=sys.stderr)
            print(build.stderr[-2000:], file=sys.stderr)
            return _fail("镜像构建失败", code=2)
        print(f"[smoke] 构建完成，用时 {time.monotonic() - started:.1f}s")

    size = _run(["docker", "image", "inspect", "-f", "{{.Size}}", args.image], timeout=60)
    if size.returncode != 0:
        return _fail(f"镜像 {args.image} 不存在（去掉 --no-build 或先构建）", code=2)
    print(f"[smoke] 镜像 {args.image} 大小 {int(size.stdout.strip()) / 1024 / 1024:.1f} MiB")

    _remove_container()
    # 与 compose 对齐：只读根文件系统 + tmpfs /tmp，不挂数据库网络
    run = _run(
        [
            "docker", "run", "-d",
            "--name", CONTAINER_NAME,
            "--read-only",
            "--tmpfs", "/tmp:size=32m",
            args.image,
        ],
        timeout=120,
    )
    if run.returncode != 0:
        return _fail(f"容器启动失败：{(run.stderr or run.stdout).strip()}")

    started = time.monotonic()
    status_line = ""
    try:
        while time.monotonic() - started < args.timeout:
            probe = _run(
                ["docker", "exec", CONTAINER_NAME, "python", "-c", PROBE_CODE], timeout=30
            )
            if probe.returncode == 0:
                first_line, _, rest = probe.stdout.partition("\n")
                status_line = first_line.strip()
                if status_line == "200":
                    elapsed = time.monotonic() - started
                    print(f"[smoke] /healthz 200，用时 {elapsed:.1f}s")
                    print("[smoke] 响应体：")
                    print(rest.strip() or "(空)")
                    return 0
                # 返回了非 200：不需要继续等
                return _fail(f"/healthz 返回 {status_line}，响应体：{rest.strip()}")
            if not _container_running():
                _dump_logs()
                return _fail("容器已退出（启动即崩），见上方日志")
            time.sleep(2)
        _dump_logs()
        return _fail(f"{args.timeout}s 内 /healthz 未就绪")
    finally:
        if args.keep:
            print(f"[smoke] --keep：容器 {CONTAINER_NAME} 保留（docker rm -f {CONTAINER_NAME} 清理）")
        else:
            _remove_container()
            print(f"[smoke] 已清理容器 {CONTAINER_NAME}")


if __name__ == "__main__":
    raise SystemExit(main())
