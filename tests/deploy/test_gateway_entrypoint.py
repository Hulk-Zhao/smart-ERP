#!/usr/bin/env python3
"""connector-gw 入口回归（P0）：compose command 与 Dockerfile CMD 必须指向真实存在的模块。

背景：main 曾经把 gw 镜像入口写成 `python -m connectors.gateway`，而代码实际在
`apps/connectors/gateway.py`，镜像启动即 `ModuleNotFoundError`。本用例锁死三件事：
  ① 入口模块路径指向 `apps.connectors.gateway`（compose 与 Dockerfile 一致）；
  ② 该模块文件在仓库里真实存在；
  ③ 模块可被解析（ast）且带 `main` 入口，避免「路径对但函数没了」。

离线可跑，不需要 docker：
  python -m pytest tests/deploy/test_gateway_entrypoint.py -q
"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
COMPOSE_FILE = REPO_ROOT / "deploy" / "smart-erp" / "docker-compose.yml"
DOCKERFILE = REPO_ROOT / "projects" / "smart-ERP" / "docker" / "connector-gw.Dockerfile"
ENTRY_MODULE = "apps.connectors.gateway"
ENTRY_FILE = REPO_ROOT / "apps" / "connectors" / "gateway.py"


def _compose_gw_command() -> list[str]:
    compose = yaml.safe_load(COMPOSE_FILE.read_text(encoding="utf-8"))
    command = compose["services"]["connector-gw"]["command"]
    return [str(part) for part in command]


def _dockerfile_cmd() -> list[str]:
    for line in DOCKERFILE.read_text(encoding="utf-8").splitlines():
        if line.strip().startswith("CMD "):
            payload = line.strip()[4:].strip()
            return [str(item) for item in ast.literal_eval(payload)]
    raise AssertionError("connector-gw.Dockerfile 缺少 CMD")


class GatewayEntrypointTest(unittest.TestCase):
    """用 TestCase 包装：unittest discover 只收 TestCase，pytest 两种都收，
    包成类后两个入口收到的是同一批用例，避免 CI 换入口时静默少收。"""

    def test_entry_module_file_exists(self) -> None:
        assert ENTRY_FILE.is_file(), (
            f"入口模块不存在：{ENTRY_FILE.relative_to(REPO_ROOT)}"
        )

    def test_compose_command_points_to_cli_module(self) -> None:
        command = _compose_gw_command()
        assert command[:3] == ["python", "-m", ENTRY_MODULE], (
            f"compose command 与入口不一致：{command}"
        )

    def test_dockerfile_cmd_points_to_cli_module(self) -> None:
        cmd = _dockerfile_cmd()
        assert cmd[:3] == ["python", "-m", ENTRY_MODULE], (
            f"Dockerfile CMD 与入口不一致：{cmd}"
        )

    def test_entry_module_exposes_main(self) -> None:
        tree = ast.parse(ENTRY_FILE.read_text(encoding="utf-8"))
        functions = {
            node.name for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)
        }
        assert "main" in functions, (
            "apps/connectors/gateway.py 必须提供 main() 供 python -m 调用"
        )
