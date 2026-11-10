"""连接器 transport 私有化：import-linter 契约 + 全量 AST 扫描。"""

from __future__ import annotations

import ast
from pathlib import Path

from click.testing import CliRunner
from importlinter.cli import lint_imports_command

ROOT = Path(__file__).resolve().parents[2]
TRANSPORT = "connectors.transport"
ALLOWED_IMPORTERS = {"sdk.py", "gateway.py"}


def test_only_sdk_and_gateway_import_transport() -> None:
    offenders: list[str] = []
    for path in (ROOT / "apps").rglob("*.py"):
        if path.name in ALLOWED_IMPORTERS:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            modules: list[str] = []
            if isinstance(node, ast.ImportFrom) and node.module:
                modules.append(node.module)
            elif isinstance(node, ast.Import):
                modules.extend(alias.name for alias in node.names)
            if any(TRANSPORT in module for module in modules):
                offenders.append(f"{path.relative_to(ROOT)}:{node.lineno}")

    assert offenders == [], f"transport 只允许 SDK 与 WriteGateway 导入，违规：{offenders}"


def test_importlinter_contract_passes(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(ROOT))
    monkeypatch.chdir(ROOT)
    result = CliRunner().invoke(lint_imports_command, [])

    assert result.exit_code == 0, result.output
    assert "0 broken" in result.output
