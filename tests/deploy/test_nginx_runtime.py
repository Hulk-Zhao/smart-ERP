#!/usr/bin/env python3
"""真容器验证：经 web 容器的 /erp/api/* 必须 200，且解析族与监听族一致。

为什么单独一条：静态断言只能看文本。`proxy_pass $upstream/;` 这种「变量 + URI」
在文本上和后端契约完全一致，但 nginx 运行时不做前缀替换，所有 /erp/api/* 都会
被压成 "/" → 404（2026-11-11 实测）。反过来，为了剥前缀改写字面量上游，又会把
`resolver ... ipv6=off` 一起拿走：Docker DNS 同时返回 A 与 AAAA 时 nginx 可能选中
AAAA，而 api 只监听 IPv4，于是 502（同日 CI 事故）。所以这里不仅断言 200，还断言：

  1. 运行中的 nginx 配置里 resolver 仍带 `ipv6=off`（`nginx -T` 读的是进程真正
     加载的配置，不是仓库文件文本）；
  2. web 容器经 Docker DNS（127.0.0.11）解析到的 api 地址族，与 api 实际监听的
     地址族一致；即 nginx 在 ipv6=off 下会用的 A 记录，必须就是 api 在用的 IPv4。

第 2 条是那次 502 的直接产物：200 只说明「这一次恰好解析到 IPv4」，族一致性才
说明「换一种 DNS 返回顺序也不会 502」。

默认跳过（构建 api/web 镜像较慢）；CI 的 deploy-static 会先构建镜像再打开：
  ERP_NGINX_E2E=1 \
  ERP_NGINX_E2E_API_IMAGE=smart-erp/app:ci-nginx-e2e \
  ERP_NGINX_E2E_WEB_IMAGE=smart-erp/web:ci-nginx-e2e \
  python -m pytest tests/deploy/test_nginx_runtime.py -q -o addopts=""

镜像缺失时失败而不是跳过：跳过会让这条门禁在主干上等于不存在。
"""

from __future__ import annotations

import ipaddress
import os
import re
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
# api 已确认就绪后才起 web；5xx 只留一小段窗口等 nginx worker 起来，
# 不再用长重试掩盖「解析到错误地址族」这种持续 502。
RETRY_5XX_SECONDS = 15


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

        # 先把 api 等到就绪，再起 web：避免「api 尚未监听」被误判成 nginx 配置问题。
        cls._wait_api_ready(WAIT_SECONDS)

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

    @classmethod
    def _wait_api_ready(cls, timeout: float) -> None:
        """在 api 容器内部打自己的 /healthz：不依赖宿主机能否路由容器 IP。"""
        script = (
            "import urllib.request;"
            "urllib.request.urlopen('http://127.0.0.1:8010/healthz', timeout=2).read()"
        )
        deadline = time.time() + timeout
        last_probe = ""
        while time.time() < deadline:
            probe = _docker("exec", cls.api, "python", "-c", script)
            if probe.returncode == 0:
                return
            last_probe = f"rc={probe.returncode} stdout={probe.stdout[-500:]!r} " \
                         f"stderr={probe.stderr[-500:]!r}"
            time.sleep(1)
        state = _docker(
            "inspect", "--format",
            "{{.State.Status}} exit={{.State.ExitCode}} "
            "started={{.State.StartedAt}} err={{.State.Error}}",
            cls.api,
        )
        logs = _docker("logs", cls.api)
        cls._cleanup()
        raise AssertionError(
            f"api 在 {int(timeout)}s 内未就绪；最后一次探针：{last_probe}；"
            f"state={state.stdout.strip()}{state.stderr.strip()}；"
            f"stdout={logs.stdout[-1500:]!r} stderr={logs.stderr[-1500:]!r}"
        )

    def _api_ipv4(self) -> str:
        inspected = _docker(
            "inspect", "--format",
            "{{range .NetworkSettings.Networks}}{{.IPAddress}} {{end}}", self.api,
        )
        self.assertEqual(inspected.returncode, 0, inspected.stderr)
        addresses = inspected.stdout.split()
        self.assertEqual(
            len(addresses), 1, f"api 容器应恰好有一个 IPv4 地址，实际 {addresses!r}"
        )
        return addresses[0]

    def _resolve(self, rrtype: str) -> list[str]:
        """在 web 容器内用 Docker DNS（127.0.0.11）解析 api，只收集指定地址族。"""
        result = _docker(
            "exec", self.web, "nslookup", f"-type={rrtype}", "api", "127.0.0.11"
        )
        text = result.stdout + result.stderr
        version = 4 if rrtype == "A" else 6
        found: list[str] = []
        for token in re.findall(r"[0-9A-Fa-f:.]+", text):
            try:
                address = ipaddress.ip_address(token)
            except ValueError:
                continue
            if address.version != version or address.is_loopback:
                continue
            if str(address) not in found:
                found.append(str(address))
        return found

    def _web_http_get(self, address: str) -> bool:
        """从 web 容器向 api 的某个具体地址发真实 HTTP 请求（等价 nginx 的连接方向）。"""
        host = f"[{address}]" if ":" in address else address
        result = _docker(
            "exec", self.web, "wget", "-q", "-O", "-", "-T", "5",
            f"http://{host}:8010/healthz",
        )
        return result.returncode == 0 and '"status":"ok"' in result.stdout

    def _runtime_nginx_conf(self) -> str:
        """`nginx -T` 输出进程真正加载的完整配置（含 include 进来的 server 段）。"""
        result = _docker("exec", self.web, "nginx", "-T")
        self.assertEqual(result.returncode, 0, f"nginx -T 失败：{result.stderr}")
        return result.stdout

    def _probe(self, path: str) -> tuple[int | None, str]:
        last_error = ""
        deadline = time.time() + RETRY_5XX_SECONDS
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{self.port}{path}", timeout=5
                ) as response:
                    return response.status, response.read().decode("utf-8", "replace")
            except urllib.error.HTTPError as exc:
                status = exc.code
                body = exc.read().decode("utf-8", "replace")
                # 5xx 只等 nginx worker 起来的短窗口；持续 5xx 就是配置/解析问题。
                if status >= 500:
                    last_error = f"{status}: {body[:200]}"
                    time.sleep(1)
                    continue
                return status, body
            except (OSError, urllib.error.URLError) as exc:
                last_error = str(exc)
                time.sleep(1)
        return None, last_error

    def test_resolved_family_matches_listening_family(self):
        """web 解析到的 api 地址族必须正是 api 在监听的那一族。

        这是 2026-11-11 CI 502 的直接产物：当时变量+URI 改成字面量上游，
        resolver/ipv6=off 一起丢失；Docker DNS 返回 AAAA 时 nginx 连过去，
        而 api 只监听 IPv4 → 502。只断言 200 挡不住这种回归（这次恰好解析对
        就绿了），所以这里断言「族一致」这个不变量本身。
        """
        api_ipv4 = self._api_ipv4()
        resolved_a = self._resolve("A")
        self.assertEqual(
            resolved_a, [api_ipv4],
            f"web 容器经 Docker DNS 解析 api 的 A 记录为 {resolved_a!r}，"
            f"与 api 容器实际 IPv4 {api_ipv4!r} 不一致：测试环境本身不可信，先修环境",
        )
        # ipv6=off 意味着 nginx 只会用 A 记录；它必须真的能连上 api。
        self.assertTrue(
            self._web_http_get(api_ipv4),
            f"api 的 A 记录 {api_ipv4} 从 web 容器连不通：ipv6=off 下 nginx 只用这个族，"
            "必然 502",
        )

        # 必须是**运行中**的配置带 ipv6=off，而不是仓库文件里有、挂载进去丢了。
        runtime_conf = self._runtime_nginx_conf()
        self.assertRegex(
            runtime_conf,
            r"resolver\s+127\.0\.0\.11\b[^;]*\bipv6=off\b[^;]*;",
            "运行中的 nginx 配置缺少 `resolver 127.0.0.11 ... ipv6=off`："
            "Docker DNS 返回 AAAA 时 nginx 会选中它，而 api 只监听 IPv4 → 502",
        )

        resolved_aaaa = self._resolve("AAAA")
        print(
            f"[e2e] api IPv4={api_ipv4} web 解析 A={resolved_a} AAAA={resolved_aaaa}",
            flush=True,
        )
        if resolved_aaaa:
            reachable = [addr for addr in resolved_aaaa if self._web_http_get(addr)]
            print(
                f"[e2e] AAAA 记录 {resolved_aaaa} 中从 web 可连通的：{reachable}；"
                "ipv6=off 保证 nginx 不会使用它们",
                flush=True,
            )

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

    def test_empty_prefix_maps_to_root_not_empty_uri(self):
        """`/erp/api/` 本身必须变成 `/`，不能变成空 URI。

        api 没有 `/` 路由，所以期望 404；一旦出现 5xx/400，说明 rewrite 的
        `(.*)` 空匹配把 URI 弄空了，而不是剥成根路径。
        """
        status, body = self._probe("/erp/api/")
        self.assertIn(
            status, (200, 404),
            f"经 web 的 /erp/api/ 期望 404（api 无根路由），实际 {status}；"
            f"响应：{body[:300]!r}。5xx/400 通常意味着 rewrite 把空匹配变成了空 URI。",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
