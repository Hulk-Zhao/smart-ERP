"""镜像打包守卫（B-S8-01）：``core*`` / ``tools*`` 必须被 setuptools 收进产物。

本仓库已经栽过两次「本地跑得过、镜像里 ModuleNotFoundError」，所以这里不从
代码目录是否存在推断，而是直接按 ``pyproject.toml`` 的 include 规则跑
``setuptools.find_packages``（打包时用的同一套发现逻辑），再真实导入三个入口。
"""

from __future__ import annotations

import importlib
import os
import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _include_patterns() -> list[str]:
    with (ROOT / "pyproject.toml").open("rb") as fh:
        data = tomllib.load(fh)
    return list(
        data["tool"]["setuptools"]["packages"]["find"]["include"]
    )


def test_pyproject_includes_runtime_packages() -> None:
    include = _include_patterns()
    for pattern in ("apps*", "core*", "tools*"):
        assert pattern in include, f"packages.find.include 缺 {pattern}：{include}"


def test_find_packages_matches_image_layout() -> None:
    from setuptools import find_packages

    found = set(find_packages(where=str(ROOT), include=_include_patterns()))
    for package in ("apps", "apps.agents", "core", "core.runtime", "tools"):
        assert package in found, f"打包发现缺 {package}；实际 {sorted(found)}"


def test_runtime_modules_importable() -> None:
    for module in (
        "core.runtime.queue",
        "core.runtime.worker",
        "core.runtime.scheduler",
        "tools.health",
    ):
        importlib.import_module(module)


def test_entrypoints_run_as_module() -> None:
    env = {**os.environ, "PYTHONPATH": str(ROOT)}
    for module in ("core.runtime.worker", "core.runtime.scheduler", "tools.health"):
        completed = subprocess.run(
            [sys.executable, "-m", module, "--help"],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert completed.returncode == 0, (
            f"python -m {module} --help 失败：{completed.stderr}"
        )
