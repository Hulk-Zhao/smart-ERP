#!/usr/bin/env python3
"""真容器验证：经 web 容器的 /erp/api/* 必须 200。

为什么单独一条：静态断言只能看文本。`proxy_pass $upstream/;` 这种「变量 + URI」
在文本上和后端契约完全一致，但 nginx 运行时不做前缀替换，所有 /erp/api/* 都会
被压成 "/" → 404（2026-11-11 实测）。这里用仓库里的真实 web-nginx.conf（只读挂载，
不替换上游、不改 resolver/ipv6）起 web + api 两个容器，直接打 HTTP。

默认跳过（构建 api/web 镜像较慢）；CI 的 deploy-static 会先构建镜像再打开：
  ERP_NGINX_E2E=1 \
  ERP_NGINX_E2E_API_IMAGE=smart-erp/app:ci-nginx-e2e \
  ERP_NGINX_E2E_WEB_IMAGE=smart-erp/web:ci-nginx-e2e \
  python -m pytest tests/deploy/test_nginx_runtime.py -q -o addopts=""

镜像缺失时失败而不是跳过：跳过会让这条门禁在主干上等于不存在。
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import time
import unittest
import urllib.error
import urllib.request
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
NGINX_CONF = REPO_ROOT / "projects" / "smart-ERP" / "docker" / "web-nginx.conf"

API_IMAGE = os.environ.get("ERP_NGINX_E2E_API_IMAGE", "smart-erp/app:ci-nginx-e2e")
WEB_IMAGE = os.environ.get("ERP_NGINX_E2E_WEB_IMAGE", "smart-erp/web:ci-nginx-e2e")
ENABLED = os.environ.get("ERP_NGINX_E2E") == "1"
WAIT_SECONDS = 90


def _docker(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["docker", *args], capture_output=True, text=True, check=False, timeout=180
    )


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@unittest.skipUnless(
    ENABLED, "真容器用例默认跳过：CI deploy-static 或 ERP_NGINX_E2E=1 时运行"
)
class NginxRuntimeTest(unittest.TestCase):
    api: str
    web: str
    network: str
    port: int

    @classmethod
    def setUpClass(cls):
        if not shutil.which("docker"):
            raise AssertionError("本用例需要 docker CLI；CI deploy-static 上应可用")
        if _docker("info").returncode != 0:
            raise AssertionError("docker daemon 不可用，无法执行真容器验证")
        for image in (API_IMAGE, WEB_IMAGE):
            if _docker("image", "inspect", image).returncode != 0:
                raise AssertionError(
                    f"镜像 {image} 不在本机。CI 由 deploy-static 的构建步骤保证；"
                    "本地请先 docker build（见文件头注释）。镜像缺失不得 skip。"
                )

        suffix = uuid.uuid4().hex[:8]
        cls.network = f"erp-nginx-e2e-{suffix}"
        cls.api = f"erp-nginx-e2e-api-{suffix}"
        cls.web = f"erp-nginx-e2e-web-{suffix}"
        cls.port = _free_port()

        created = _docker("network", "create", cls.network)
        if created.returncode != 0:
            raise AssertionError(f"docker network create 失败：{created.stderr}")

        # api：与 compose 的 api.command 对齐（含 --root-path）。
        # 网络别名必须是 api：web-nginx.conf 里的上游就是 api:8010，不允许替换。
        api = _docker(
            "run", "-d", "--name", cls.api, "--network", cls.network,
            "--network-alias", "api",
            API_IMAGE,
            "uvicorn", "apps.main:app", "--host=0.0.0.0", "--port=8010",
            "--workers=1", "--limit-concurrency=64", "--root-path=/erp/api",
        )
        if api.returncode != 0:
            cls._cleanup()
            raise AssertionError(f"api 容器启动失败：{api.stderr}")

        # web：真实配置只读挂载，不复制、不改上游、不动 resolver/ipv6。
        web = _docker(
            "run", "-d", "--name", cls.web, "--network", cls.network,
            "-p", f"127.0.0.1:{cls.port}:8080",
            "-v", f"{NGINX_CONF.as_posix()}:/etc/nginx/conf.d/default.conf:ro",
            WEB_IMAGE,
        )
        if web.returncode != 0:
            cls._cleanup()
            raise AssertionError(f"web 容器启动失败：{web.stderr}")

        deadline = time.time() + WAIT_SECONDS
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{cls.port}/healthz", timeout=2
                ) as response:
                    if response.status == 200:
                        return
            except (OSError, urllib.error.URLError):
                pass
            time.sleep(1)
        logs = _docker("logs", cls.web).stdout + _docker("logs", cls.api).stdout
        cls._cleanup()
        raise AssertionError(f"web 在 {WAIT_SECONDS}s 内未就绪；容器日志：\n{logs[-2000:]}")

    @classmethod
    def tearDownClass(cls):
        cls._cleanup()

    @classmethod
    def _cleanup(cls):
        _docker("rm", "-f", cls.web)
        _docker("rm", "-f", cls.api)
        _docker("network", "rm", cls.network)

    def _probe(self, path: str) -> tuple[int | None, str]:
        last_error = ""
        deadline = time.time() + WAIT_SECONDS
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{self.port}{path}", timeout=5
                ) as response:
                    return response.status, response.read().decode("utf-8", "replace")
            except urllib.error.HTTPError as exc:
                status = exc.code
                body = exc.read().decode("utf-8", "replace")
                # 502/503 是 api 还没就绪的启动竞态；4xx 是配置错误，立即返回。
                if status >= 500:
                    last_error = f"{status}: {body[:200]}"
                    time.sleep(1)
                    continue
                return status, body
            except (OSError, urllib.error.URLError) as exc:
                last_error = str(exc)
                time.sleep(1)
        return None, last_error

    def test_prefixed_healthz_is_200(self):
        status, body = self._probe("/erp/api/healthz")
        self.assertEqual(
            status, 200,
            f"经 web 的 /erp/api/healthz 应为 200，实际 {status}；"
            f"响应：{body[:300]!r}。关注 web-nginx.conf 的 proxy_pass："
            "变量 + URI 组合会让 nginx 不做前缀替换。",
        )
        self.assertIn('"status":"ok"', body)

    def test_prefixed_business_path_is_200(self):
        status, body = self._probe("/erp/api/openapi.json")
        self.assertEqual(
            status, 200,
            f"经 web 的 /erp/api/openapi.json 应为 200，实际 {status}；"
            f"响应：{body[:300]!r}。说明前缀剥离只对 healthz 生效或根本没生效。",
        )
        self.assertIn('"openapi"', body)


if __name__ == "__main__":
    unittest.main(verbosity=2)
