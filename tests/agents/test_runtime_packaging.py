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
    for pattern in ("apps*", "core*", "tools*", "observability*"):
        assert pattern in include, f"packages.find.include 缺 {pattern}：{include}"


def _first_party_roots() -> set[str]:
    """仓库根下的一等包（有 __init__.py 的目录）。"""
    return {
        path.name
        for path in ROOT.iterdir()
        if path.is_dir() and (path / "__init__.py").is_file()
    }


def test_every_first_party_package_is_packaged() -> None:
    """磁盘上每个一等包都必须被 include 收进镜像。

    2026-11-11：main 新增 observability/（apps.main 在**导入期**就 import
    observability.metrics），而 include 仍是 apps/core/tools。镜像 build 成功，
    api 容器一启动就 ModuleNotFoundError，web 反代最终表现成 502——正是
    「build 绿、运行红」的典型。枚举清单挡不住「新增包忘记登记」，这里按
    磁盘实际包集合反查。
    """
    from fnmatch import fnmatch

    from setuptools import find_packages

    include = _include_patterns()
    packaged_roots = {
        package.split(".")[0]
        for package in find_packages(where=str(ROOT), include=include)
    }
    missing = sorted(
        root
        for root in _first_party_roots()
        if root not in packaged_roots or not any(fnmatch(root, p) for p in include)
    )
    assert not missing, (
        f"这些一等包没被打进镜像：{missing}；include={include}。"
        "镜像 build 成功不代表能启动，漏包会在容器启动时 ModuleNotFoundError。"
    )


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
