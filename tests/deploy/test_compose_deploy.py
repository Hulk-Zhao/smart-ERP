#!/usr/bin/env python3
"""部署面静态校验（离线可跑，不依赖网络、不需要起栈）。

覆盖 docs/product/tech/02-并发开发路线.md §2.9 S8 可独立验收里的四条静态判据：
  ① demo 档 compose 的服务清单固定为 5 个，且不含 pg / redis / doc / minio；
  ② 显式 memory limit 合计 ≤ 1200MiB（§1A.3 预算）；
  ③ compose 引用的每个 ERP_* 变量都在 deploy/smart-erp/.env.example 登记；
  ④ 每个服务的 healthcheck 指向契约路径（/healthz、/readyz）或 tools.health。

另外校验：日志轮转 20m×5、named volumes、8010 只绑回环、8012 只 expose 不发布、
worker/scheduler 健康判定统一走 `tools.health --check`（不再 expose 8011）、
Dockerfile 多阶段 + 非 root + 不用 latest、provision SQL 的隔离与红线。

运行：
  python -m unittest discover -s tests/deploy -v
  # 或
  python tests/deploy/test_compose_deploy.py -v

有 docker 时额外执行 `docker compose config` 的正向与负向（缺 ERP_IMAGE_TAG 必失败）；
没有 docker 时这两条自动跳过，静态断言仍然生效。
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from urllib.parse import urlparse

try:
    import yaml
except ImportError:  # pragma: no cover
    sys.exit("需要 PyYAML：pip install pyyaml（CI 里由 workflow 安装）")

REPO_ROOT = Path(__file__).resolve().parents[2]
DEPLOY_DIR = REPO_ROOT / "deploy" / "smart-erp"
COMPOSE_FILE = DEPLOY_DIR / "docker-compose.yml"
ENV_EXAMPLE = DEPLOY_DIR / ".env.example"
PROVISION_SQL = DEPLOY_DIR / "db" / "00-provision.sql"
DOCKER_DIR = REPO_ROOT / "projects" / "smart-ERP" / "docker"
SCRIPTS_DIR = REPO_ROOT / "scripts"
TEST_ENV_FILE = Path(__file__).resolve().parent / "compose.env"

DEMO_SERVICES = {"api", "worker", "scheduler", "connector-gw", "web"}
FULL_ONLY_SERVICES = {"doc", "worker-agent", "worker-io", "migrate"}
EXPECTED_SERVICES = DEMO_SERVICES | FULL_ONLY_SERVICES
# 任何档位都不许出现的容器（共用 PG15，队列在 PG，对象存储走本地卷/MinIO 外部）
FORBIDDEN_SERVICES = {"pg", "postgres", "redis", "cache", "minio"}
MEMORY_BUDGET_MIB = 1200
FULL_MEMORY_CEILING_MIB = 8 * 1024
HEALTHCONTRACT_PATHS = {"/healthz", "/readyz"}

TAG_PATTERN = r"^\s*ERP_IMAGE_TAG=(.*)$"


def _load_compose() -> dict:
    with COMPOSE_FILE.open(encoding="utf-8") as fp:
        return yaml.safe_load(fp)


def _parse_mib(value) -> float:
    """把 compose 的 384m / 1200M / 1g 统一成 MiB。"""
    text = str(value).strip().lower()
    match = re.fullmatch(r"([0-9.]+)\s*([kmg]?)i?b?", text)
    if not match:
        raise AssertionError(f"无法解析内存限额: {value!r}")
    number = float(match.group(1))
    unit = match.group(2)
    return number * {"": 1 / (1024 * 1024), "k": 1 / 1024, "m": 1, "g": 1024}[unit]


def _env_example_keys() -> dict:
    keys = {}
    for line in ENV_EXAMPLE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        keys[key.strip()] = value.strip()
    return keys


def _compose_env_refs() -> set:
    text = COMPOSE_FILE.read_text(encoding="utf-8")
    return set(re.findall(r"\$\{(ERP_[A-Z0-9_]+)", text))


def _usable_bash():
    """挑一个真能用的 bash。

    Windows 上 System32\\bash.exe 是 WSL 壳：探针 `bash -c echo` 可能返回 0，
    但会把 Windows 路径吃掉反斜杠。所以统一优先 Git Bash，并且全程用 stdin
    传脚本内容（不做路径翻译）。
    """
    candidates = [
        r"C:\Program Files\Git\bin\bash.exe",
        r"C:\Program Files\Git\usr\bin\bash.exe",
        "/bin/bash",
        shutil.which("bash"),
    ]
    for candidate in candidates:
        if not candidate or not Path(candidate).exists():
            continue
        try:
            probe = subprocess.run(
                [candidate, "-n"],
                input=b"set -e\necho ok\n",
                capture_output=True,
                timeout=30,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if probe.returncode == 0:
            return candidate
    return None


class ComposeStaticTest(unittest.TestCase):
    """不依赖 docker：只解析 YAML 与文本。"""

    @classmethod
    def setUpClass(cls):
        cls.compose = _load_compose()
        cls.services = cls.compose["services"]

    def test_project_name(self):
        self.assertEqual(self.compose.get("name"), "smart-erp")

    def test_service_inventory(self):
        self.assertEqual(set(self.services), EXPECTED_SERVICES)
        self.assertFalse(
            FORBIDDEN_SERVICES & set(self.services),
            "不得包含 pg/redis/minio 等容器（共用 PG15、队列在 PG、对象存储走卷）",
        )

    def test_profiles_partition(self):
        """demo 档 5 个服务，full 档额外 4 个；两档都覆盖的常驻服务列 demo+full。"""
        for name in DEMO_SERVICES:
            with self.subTest(service=name):
                profiles = self.services[name]["profiles"]
                self.assertIn("demo", profiles)
                if name != "worker":  # full 档把 worker 拆成 worker-agent/worker-io
                    self.assertIn("full", profiles, f"{name} 在 full 档也要常驻")
        for name in FULL_ONLY_SERVICES:
            with self.subTest(service=name):
                self.assertEqual(self.services[name]["profiles"], ["full"])
        self.assertEqual(self.services["worker"]["profiles"], ["demo"])

    def test_demo_memory_budget(self):
        """§1A.3：demo 档常驻合计恰好 1200MiB；full 档另算（≤8G 整机）。"""
        total = 0.0
        demo_total = 0.0
        for name, svc in self.services.items():
            with self.subTest(service=name):
                self.assertIn("mem_limit", svc, f"{name} 必须显式声明 mem_limit")
                # 不放水：不用 memswap_limit（§1A.3）
                self.assertNotIn("memswap_limit", svc)
                size = _parse_mib(svc["mem_limit"])
                total += size
                if "demo" in svc["profiles"]:
                    demo_total += size
        self.assertEqual(demo_total, MEMORY_BUDGET_MIB, "demo 档内存预算应与 §1A.3 的 1200MiB 对齐")
        self.assertLessEqual(
            total, FULL_MEMORY_CEILING_MIB, f"两档服务合计 {total:.0f}MiB 超过 8G 整机预算"
        )

    def test_full_profile_topology(self):
        """full 档差异项：doc 3G / worker 拆 agent-io / migrate 一次性。"""
        doc = self.services["doc"]
        self.assertEqual(_parse_mib(doc["mem_limit"]), 3072)
        self.assertIn("smart-erp/doc:", doc["image"])
        self.assertEqual(str(doc["healthcheck"]["start_period"]), "300s")
        self.assertIn("8020", [str(p) for p in doc.get("expose", [])])
        self.assertNotIn("ports", doc, "8020 只在内网，不发布到宿主")

        for name, queue in (("worker-agent", "agent"), ("worker-io", "io")):
            with self.subTest(service=name):
                command = " ".join(str(part) for part in self.services[name]["command"])
                self.assertIn(f"--queues={queue}", command)
                self.assertIn(f"ERP_WORKER_QUEUES: {queue}", COMPOSE_FILE.read_text(encoding="utf-8"))

        migrate = self.services["migrate"]
        self.assertEqual(migrate["restart"], "no", "migrate 必须是一次性服务")
        self.assertEqual(
            " ".join(str(part) for part in migrate["command"]),
            "alembic -c /app/alembic.ini upgrade head",
        )
        # 键仍是 ERP_DATABASE_URL（应用只认这个键），值必须来自迁移角色变量
        self.assertIn("ERP_MIGRATION_DATABASE_URL", migrate["environment"]["ERP_DATABASE_URL"])
        self.assertIn("erp_migrator", migrate["environment"]["ERP_DATABASE_URL"])

    def test_image_tag_is_forced(self):
        raw = COMPOSE_FILE.read_text(encoding="utf-8")
        self.assertIn("${ERP_IMAGE_TAG:?", raw)
        for name, svc in self.services.items():
            with self.subTest(service=name):
                self.assertIn(
                    "${ERP_IMAGE_TAG:?", svc["image"],
                    f"{name} 的 image 必须用 ${{ERP_IMAGE_TAG:?...}} 强制传 tag",
                )
                self.assertNotIn("latest", svc["image"])

    def test_logging_rotation(self):
        for name, svc in self.services.items():
            with self.subTest(service=name):
                logging = svc.get("logging", {})
                self.assertEqual(logging.get("driver"), "json-file")
                self.assertEqual(str(logging["options"]["max-size"]), "20m")
                self.assertEqual(str(logging["options"]["max-file"]), "5")

    def test_restart_policy(self):
        for name, svc in self.services.items():
            with self.subTest(service=name):
                if name == "migrate":  # 一次性迁移，绝不能重启（见 test_full_profile_topology）
                    self.assertEqual(svc.get("restart"), "no")
                else:
                    self.assertEqual(svc.get("restart"), "unless-stopped")

    def test_required_env_in_services(self):
        for name in ("api", "worker", "scheduler"):
            with self.subTest(service=name):
                env = self.services[name]["environment"]
                self.assertIn("ERP_DATABASE_URL", env, f"{name} 必须拿到 DATABASE_URL")
                self.assertIn("ERP_IMAGE_TAG", env)

    def test_env_example_registers_every_compose_var(self):
        declared = _env_example_keys()
        used = _compose_env_refs()
        missing = sorted(v for v in used if v not in declared)
        self.assertEqual(missing, [], f"以下变量未在 .env.example 登记：{missing}")

    def test_env_example_has_no_secret(self):
        text = ENV_EXAMPLE.read_text(encoding="utf-8")
        self.assertIn("CHANGE_ME_PASSWORD", text, "口令必须是显式占位符")
        for pattern in (r"sk-[A-Za-z0-9]{16,}", r"AKIA[0-9A-Z]{16}"):
            self.assertIsNone(re.search(pattern, text), "不得提交真实密钥")

    def test_image_tag_example_is_empty(self):
        # 留空是刻意的：copy 成 .env 后必须由人填，否则 compose 直接失败
        match = re.search(TAG_PATTERN, ENV_EXAMPLE.read_text(encoding="utf-8"), re.MULTILINE)
        self.assertIsNotNone(match)
        self.assertEqual(match.group(1).strip(), "")

    def test_ports_are_split_public_and_internal(self):
        api_ports = self.services["api"].get("ports", [])
        web_ports = self.services["web"].get("ports", [])
        self.assertTrue(any("8010" in p for p in api_ports), "api 必须发布 8010")
        # 安全收口：8010 只绑回环，公网只经宿主 nginx；出现 0.0.0.0 视为回退
        self.assertTrue(all("127.0.0.1" in p for p in api_ports), "8010 必须只绑 127.0.0.1")
        self.assertTrue(any("8088" in p for p in web_ports), "web 必须发布 8088")

        worker, gw = self.services["worker"], self.services["connector-gw"]
        self.assertNotIn("ports", worker, "worker 不发布宿主端口")
        self.assertNotIn("ports", gw, "8012 只在内网，不得发布到宿主")
        # 8011 没有常驻进程监听（健康判定走 tools.health --check），不应再声明 expose
        self.assertNotIn("expose", worker, "worker 不 expose 8011：没有进程监听")
        self.assertIn("8012", [str(p) for p in gw.get("expose", [])])

    def test_networks(self):
        networks = self.compose["networks"]
        self.assertEqual(networks["smart-erp-net"]["name"], "smart-erp_net")
        db_net = networks["erp-db-net"]
        self.assertTrue(db_net.get("external"), "共用 PG 必须挂外部网络")
        self.assertIn("hulkai-gz-api_default", db_net["name"])

        # 最小权限：只有需要连库的进程挂 DB 网络
        for name in ("api", "worker", "scheduler", "worker-agent", "worker-io", "migrate"):
            with self.subTest(service=name):
                self.assertIn("erp-db-net", self.services[name].get("networks", []))
        for name in ("connector-gw", "web", "doc"):
            with self.subTest(service=name):
                self.assertNotIn("erp-db-net", self.services[name].get("networks", []))

    def test_named_volumes(self):
        volumes = self.compose["volumes"]
        self.assertEqual(volumes["erp_objects"]["name"], "smart-erp_objects")
        self.assertEqual(volumes["erp_logs"]["name"], "smart-erp_logs")
        mounts = " ".join(
            str(m) for svc in self.services.values() for m in svc.get("volumes", [])
        )
        self.assertIn("erp_objects", mounts)
        self.assertIn("erp_logs", mounts)

    def test_healthchecks_hit_contract_paths(self):
        for name, svc in self.services.items():
            with self.subTest(service=name):
                if name == "migrate":
                    # 一次性任务：没有常驻进程可探，不许挂 healthcheck
                    self.assertNotIn("healthcheck", svc)
                    continue
                hc = svc.get("healthcheck")
                self.assertIsNotNone(hc, f"{name} 缺 healthcheck")
                self.assertIn("start_period", hc, f"{name} 缺 start_period")
                command = " ".join(str(part) for part in hc["test"])

                if name in ("worker", "worker-agent", "worker-io", "scheduler"):
                    # 心跳探针：不依赖 HTTP，也不触发业务写
                    self.assertIn("tools.health", command)
                    continue

                urls = re.findall(r"https?://[^\s'\"]+", command)
                self.assertTrue(urls, f"{name} 的 healthcheck 未指向 HTTP 路径")
                for url in urls:
                    path = urlparse(url).path
                    self.assertIn(
                        path, HEALTHCONTRACT_PATHS,
                        f"{name} 的探针路径 {path} 不在 C13 契约内（/healthz 或 /readyz）",
                    )

        # web 的 /healthz 必须真的由 nginx 配置返回，而不是只写在探针里
        nginx_conf = (DOCKER_DIR / "web-nginx.conf").read_text(encoding="utf-8")
        self.assertIn("location = /healthz", nginx_conf)
        # 反向代理必须用「resolver + 变量」，否则 api 未就绪时 nginx 会
        # "host not found in upstream" 直接起不来（实测踩过）
        self.assertIn("resolver 127.0.0.11", nginx_conf)
        self.assertIn("proxy_pass $erp_api_upstream", nginx_conf)
        self.assertNotIn("proxy_pass http://api:8010", nginx_conf)

        # gw 的 read_only + tmpfs（§1A.2）
        gw = self.services["connector-gw"]
        self.assertTrue(gw.get("read_only"))
        self.assertTrue(gw.get("tmpfs"))

    def test_healthz_route_really_exists(self):
        """healthcheck 打 /healthz，就必须在源码里真有这条路由，而不是只写在 YAML 里。"""
        main_py = REPO_ROOT / "apps" / "main.py"
        self.assertTrue(main_py.is_file(), "apps/main.py 未落地，无法验证 /healthz")
        text = main_py.read_text(encoding="utf-8")
        self.assertIn('@app.get("/healthz"', text)

    def test_api_entrypoint_matches_source(self):
        command = " ".join(str(part) for part in self.services["api"]["command"])
        self.assertIn("apps.main:app", command, "uvicorn 入口必须与 apps/main.py 对齐")
        main_py = REPO_ROOT / "apps" / "main.py"
        self.assertIn("app = create_app()", main_py.read_text(encoding="utf-8"))

    def test_env_key_names_align_with_config_py(self):
        """compose 的键名必须与 apps/platform/config.py 的 ERP_ 前缀一致。"""
        raw = COMPOSE_FILE.read_text(encoding="utf-8")
        self.assertIn("ERP_APP_ENV", raw)
        self.assertNotIn("ERP_ENV:", raw, "config.py 读的是 ERP_APP_ENV，不是 ERP_ENV")

        config_py = REPO_ROOT / "apps" / "platform" / "config.py"
        if config_py.is_file():
            config_text = config_py.read_text(encoding="utf-8")
            for key in ("APP_ENV", "API_PORT", "DATABASE_URL", "IMAGE_TAG"):
                self.assertRegex(
                    config_text, rf'_(?:int_)?env\("{key}"',
                    f"config.py 里找不到 {key} 的读取点",
                )

    def test_connector_gw_entrypoint_is_locked(self):
        """gw 入口必须是 `python -m apps.connectors.gateway`，且端口走 ERP_CONNECTOR_GW_PORT。

        历史坑：S1 交付前这里写的是 `python -m connectors.gateway`（不存在的模块），
        容器必然 ModuleNotFoundError。加这条断言防止以后又写回旧路径。
        """
        gw = self.services["connector-gw"]

        command = " ".join(str(part) for part in gw["command"])
        self.assertEqual(
            command,
            "python -m apps.connectors.gateway",
            f"gw 的 command 必须是 python -m apps.connectors.gateway，实际：{command}",
        )
        self.assertNotIn(" -m connectors.gateway", command, "不得回退到旧入口 connectors.gateway")

        # 端口：环境变量与 expose 必须同为 8012，不允许两处各写一份
        env = gw["environment"]
        self.assertIn("ERP_CONNECTOR_GW_PORT", env)
        self.assertIn(":-8012}", str(env["ERP_CONNECTOR_GW_PORT"]))
        self.assertNotIn("ERP_GW_PORT", env, "旧键 ERP_GW_PORT 已被 ERP_CONNECTOR_GW_PORT 取代")
        self.assertIn("8012", [str(p) for p in gw.get("expose", [])])

        # 入口模块真的存在，且确实读这个环境变量
        gateway_py = REPO_ROOT / "apps" / "connectors" / "gateway.py"
        self.assertTrue(gateway_py.is_file(), "apps/connectors/gateway.py 未落地")
        source = gateway_py.read_text(encoding="utf-8")
        self.assertIn("ERP_CONNECTOR_GW_PORT", source)
        self.assertIn("DEFAULT_PORT = 8012", source)

        # 镜像自带的 CMD 不能与 compose 打架
        dockerfile = (DOCKER_DIR / "connector-gw.Dockerfile").read_text(encoding="utf-8")
        self.assertRegex(
            dockerfile,
            r'CMD\s+\[\s*"python"\s*,\s*"-m"\s*,\s*"apps\.connectors\.gateway"\s*\]',
            "connector-gw.Dockerfile 的 CMD 必须与 compose command 一致",
        )

    def test_startup_order(self):
        self.assertIn("depends_on", self.services["web"])
        self.assertEqual(
            self.services["web"]["depends_on"]["api"]["condition"], "service_healthy"
        )
        self.assertEqual(
            self.services["api"]["depends_on"]["connector-gw"]["condition"],
            "service_healthy",
        )


class ArtifactsTest(unittest.TestCase):
    """Dockerfile / 脚本 / 开通 SQL 的静态断言。"""

    def test_dockerfiles_exist_and_are_multi_stage_nonroot(self):
        for filename in (
            "api.Dockerfile",
            "connector-gw.Dockerfile",
            "docparse.Dockerfile",
            "web.Dockerfile",
        ):
            path = DOCKER_DIR / filename
            with self.subTest(dockerfile=filename):
                self.assertTrue(path.is_file(), f"缺少 {path}")
                text = path.read_text(encoding="utf-8")
                lowered = text.lower()
                stages = re.findall(r"^FROM\s+\S+\s+AS\s+(\w+)", text, re.MULTILINE)
                self.assertGreaterEqual(len(stages), 2, "必须是多阶段构建")
                # Python 镜像用 slim，前端用 alpine；两者都是最小化基础镜像
                self.assertTrue(
                    "slim" in lowered or "alpine" in lowered,
                    "必须使用 slim / alpine 基础镜像",
                )
                self.assertRegex(text, r"USER\s+10001|USER\s+app", "必须切换到非 root 用户")
                self.assertNotRegex(
                    text, r":latest\b", "不得用 latest 承担发布语义"
                )

    def test_docparse_image_installs_pdf_extra(self):
        """doc 镜像必须装 [pdf] extra，否则 PDF 解析静默降级成「缺 PDF 支持」。"""
        text = (DOCKER_DIR / "docparse.Dockerfile").read_text(encoding="utf-8")
        self.assertRegex(
            text,
            r"pip\s+wheel\s+--wheel-dir\s+/wheels\s+\"\.\[pdf\]\"",
            "docparse.Dockerfile 的 pip wheel 必须带 \".[pdf]\"（pypdfium2 依赖）",
        )

    def test_scripts_are_syntactically_valid(self):
        bash = _usable_bash()
        if not bash:
            self.skipTest("本机没有可用的 bash（WSL 壳不算），跳过语法检查")
        for name in ("build-images.sh", "deploy-images.sh"):
            path = SCRIPTS_DIR / name
            with self.subTest(script=name):
                self.assertTrue(path.is_file(), f"缺少 {path}")
                # 用 stdin 传脚本，避开 Windows 路径在 WSL/Git Bash 之间的转译差异
                result = subprocess.run(
                    [bash, "-n"],
                    input=path.read_bytes(),
                    capture_output=True,
                    timeout=60,
                    check=False,
                )
                self.assertEqual(
                    result.returncode, 0,
                    result.stderr.decode("utf-8", errors="replace"),
                )

    def test_scripts_enforce_full_sha(self):
        for name in ("build-images.sh", "deploy-images.sh"):
            text = (SCRIPTS_DIR / name).read_text(encoding="utf-8")
            with self.subTest(script=name):
                self.assertIn("^[0-9a-f]{40}$", text, "必须校验完整 40 位 sha")
                self.assertIn("ERP_IMAGE_TAG", text)

    def test_build_script_does_not_push(self):
        text = (SCRIPTS_DIR / "build-images.sh").read_text(encoding="utf-8")
        self.assertNotIn("docker push", text, "决策：不建镜像仓库")
        self.assertIn("docker save", text)

    def test_provision_sql_isolation(self):
        sql = PROVISION_SQL.read_text(encoding="utf-8")
        self.assertIn("CREATE DATABASE smart_erp OWNER erp_migrator", sql)
        self.assertIn("CREATE SCHEMA IF NOT EXISTS erp", sql)
        self.assertIn("CREATE ROLE erp_app", sql)
        self.assertIn("CREATE ROLE erp_migrator", sql)
        self.assertIn("CONNECTION LIMIT 20", sql)
        self.assertIn("alembic_version_erp", sql)
        self.assertIn("CREATE EXTENSION IF NOT EXISTS vector   SCHEMA erp", sql)
        # 只收回本项目角色对 public 的权限；不许动全局 PUBLIC
        self.assertIn("REVOKE ALL ON SCHEMA public FROM erp_app", sql)
        self.assertNotRegex(
            sql.replace("--", "\n"),
            r"REVOKE\s+ALL\s+ON\s+SCHEMA\s+public\s+FROM\s+PUBLIC",
            "禁止修改全局 PUBLIC 默认权限",
        )
        self.assertNotIn("DROP DATABASE", sql)


@unittest.skipUnless(shutil.which("docker"), "本机没有 docker，跳过 compose config 实测")
class ComposeRenderTest(unittest.TestCase):
    """用真实 docker 解析 compose：正向必须过，缺必填变量必须失败并指出变量名。"""

    def _config(self, env_file: Path, profile: str = "demo"):
        cmd = [
            "docker", "compose",
            "--project-directory", str(DEPLOY_DIR),
            "--env-file", str(env_file),
            "--file", str(COMPOSE_FILE),
            "--profile", profile,
            "config",
        ]
        env = {k: v for k, v in os.environ.items() if not k.startswith("ERP_")}
        return subprocess.run(cmd, capture_output=True, text=True, env=env, check=False)

    def test_positive_config_full_profile(self):
        result = self._config(TEST_ENV_FILE, profile="full")
        self.assertEqual(result.returncode, 0, result.stderr)
        # full 档必须把 4 个差异服务渲染进来，且 demo 常驻服务仍在
        for name in ("api", "web", "doc", "worker-agent", "worker-io", "migrate"):
            self.assertIn(name, result.stdout, f"full 档缺少 {name}")

    def test_positive_config(self):
        result = self._config(TEST_ENV_FILE)
        self.assertEqual(result.returncode, 0, result.stderr)
        rendered = yaml.safe_load(result.stdout)
        self.assertEqual(set(rendered["services"]), DEMO_SERVICES)
        self.assertEqual(
            rendered["services"]["api"]["image"],
            "smart-erp/app:0123456789abcdef0123456789abcdef01234567",
        )

    def test_missing_image_tag_fails_with_variable_name(self):
        content = TEST_ENV_FILE.read_text(encoding="utf-8")
        stripped = "\n".join(
            line for line in content.splitlines() if not line.startswith("ERP_IMAGE_TAG=")
        )
        with tempfile.NamedTemporaryFile(
            "w", suffix=".env", delete=False, encoding="utf-8"
        ) as tmp:
            tmp.write(stripped + "\n")
            tmp_path = Path(tmp.name)
        try:
            result = self._config(tmp_path)
            self.assertNotEqual(result.returncode, 0, "缺 ERP_IMAGE_TAG 时 config 必须失败")
            self.assertIn("ERP_IMAGE_TAG", result.stderr + result.stdout)
        finally:
            tmp_path.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
