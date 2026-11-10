#!/usr/bin/env python3
"""依赖许可证门禁：白名单 + 硬拒绝（AGPL / GPL / SSPL）。

背景：docs/product/tech/01-技术架构.md §2.2 把「不引入 AGPL」写成硬规则
（历史事故：PyMuPDF 是 AGPL-3.0，选 PDF 解析库时差点误引入）。

数据来源分两层，各司其职：
  - 许可证枚举：`pip-licenses --format=json`（pip 生态的标准工具，MIT 许可）；
  - 引入路径：importlib.metadata 的 Requires-Dist 反查「谁依赖它」，
    一直追到没有上游依赖的根（通常是本项目 smart-erp），这样失败时能直接定位。

用法（在仓库根目录，CI 里由 workflow 安装 pip-licenses 后调用）：
  python scripts/check_licenses.py
  python scripts/check_licenses.py --allowlist contracts/deps/license-allowlist.txt

退出码：0 全绿；1 有拒绝项或许可证不在白名单；2 环境不可用（pip-licenses 缺失等）。
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from collections import deque
from importlib import metadata
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ALLOWLIST = REPO_ROOT / "contracts" / "deps" / "license-allowlist.txt"

# 硬拒绝：出现在依赖树里即失败，白名单里有也照样失败（防止误加白）
DENY_PREFIXES = ("AGPL", "SSPL")
DENY_EXACT = {"GPL-2.0", "GPL-2.0-only", "GPL-2.0-or-later", "GPL-3.0", "GPL-3.0-only", "GPL-3.0-or-later"}

# 构建引导工具不属于运行期依赖树，扫描时忽略（避免 pip/setuptools 的许可证噪声）
IGNORED_PACKAGES = {"smart-erp", "pip", "setuptools", "wheel", "pip-licenses"}


def _normalize_name(name: str) -> str:
    """PEP 503 名称归一化：My_Pkg.Name → my-pkg-name。"""
    return re.sub(r"[-_.]+", "-", name).lower()


def _normalize_license(raw: str) -> list[str]:
    """把 pip-licenses 给的人类可读许可证字段归一成 SPDX 风格 ID 列表。

    复合串（"MIT AND PSF-2.0"、"BSD-3-Clause, Apache-2.0, dependency licenses"）
    先整体做硬拒绝扫描——只要出现 AGPL/GPL/SSPL 的任一写法就直接判拒绝，
    不给「复合串里藏一个 GPL」留空子；再按宽松许可家族归一。
    """
    text = (raw or "").strip()
    if not text:
        return ["UNKNOWN"]
    upper = text.upper()

    if "AGPL" in upper or "AFFERO" in upper:
        return ["AGPL-3.0"]
    if "SSPL" in upper or "SERVER SIDE PUBLIC" in upper:
        return ["SSPL-1.0"]
    if "LGPL" in upper or "LESSER GENERAL PUBLIC" in upper:
        return ["LGPL-3.0"]
    if re.search(r"(^|[^A-Z])GPL", upper) or "GENERAL PUBLIC LICENSE" in upper:
        return ["GPL-3.0"]

    return [_normalize_permissive(text)]


def _normalize_permissive(raw: str) -> str:
    upper = raw.upper()
    if "APACHE" in upper:
        return "Apache-2.0"
    if "MOZILLA" in upper or "MPL" in upper:
        return "MPL-2.0"
    if "ISC" in upper:
        return "ISC"
    if "MIT" in upper:
        return "MIT"
    if "BSD" in upper:
        return "BSD-2-Clause" if "2" in upper else "BSD-3-Clause"
    if "PYTHON SOFTWARE FOUNDATION" in upper or "PSF" in upper or "PYTHON-2.0" in upper:
        return "PSF"
    if "UNKNOWN" in upper or not upper:
        return "UNKNOWN"
    return raw.strip()


def _denied(license_id: str) -> bool:
    return any(license_id.startswith(prefix) for prefix in DENY_PREFIXES) or license_id in DENY_EXACT


def _load_allowlist(path: Path) -> set[str]:
    allowed: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            allowed.add(line)
    return allowed


def _run_pip_licenses() -> list[dict]:
    """优先 `python -m piplicenses`，退化到 PATH 上的 pip-licenses 可执行文件。"""
    attempts = [
        [sys.executable, "-m", "piplicenses", "--format=json"],
        ["pip-licenses", "--format=json"],
    ]
    last_error = ""
    for cmd in attempts:
        if cmd[0] != sys.executable and shutil.which(cmd[0]) is None:
            continue
        result = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if result.returncode == 0:
            return json.loads(result.stdout)
        last_error = (result.stderr or result.stdout).strip()
    raise RuntimeError(
        "pip-licenses 不可用，请先 `pip install pip-licenses`"
        + (f"（最后一次错误：{last_error}）" if last_error else "")
    )


def _dependency_graph() -> tuple[dict[str, str], dict[str, str], dict[str, set[str]]]:
    """返回 (归一化名→展示名, 归一化名→版本, 归一化名→上游依赖集合)。"""
    display: dict[str, str] = {}
    versions: dict[str, str] = {}
    parents: dict[str, set[str]] = {}
    requires_map: dict[str, list[str]] = {}

    for dist in metadata.distributions():
        name = (dist.metadata.get("Name") or "").strip()
        if not name:
            continue
        key = _normalize_name(name)
        display.setdefault(key, name)
        versions.setdefault(key, dist.version)
        requires_map[key] = dist.requires or []

    for key, requirements in requires_map.items():
        for requirement in requirements:
            dep = _requirement_name(requirement)
            if dep:
                parents.setdefault(dep, set()).add(key)
    return display, versions, parents


def _requirement_name(requirement: str) -> str | None:
    """从 `Requires-Dist` 文本里取依赖名（不依赖 packaging，避免额外依赖）。"""
    text = requirement.strip()
    if not text or text.startswith("#"):
        return None
    # 形如 "httpx (>=0.27) ; extra == 'dev'" / "uvicorn[standard]>=0.30"
    match = re.match(r"([A-Za-z0-9][A-Za-z0-9._-]*)", text)
    if not match:
        return None
    return _normalize_name(match.group(1))


def _chain(package: str, parents: dict[str, set[str]], display: dict[str, str], versions: dict[str, str]) -> str:
    """反向 BFS 到最上游，输出「谁把它带进来的」路径。"""
    queue: deque[list[str]] = deque([[package]])
    seen = {package}
    path = [package]
    while queue:
        path = queue.popleft()
        upstream = parents.get(path[-1], set())
        if not upstream:
            break
        for parent in sorted(upstream):
            if parent in seen:
                continue
            seen.add(parent)
            queue.append([*path, parent])
    return " ← ".join(
        f"{display.get(item, item)} {versions.get(item, '')}".strip() for item in path
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="依赖许可证门禁")
    parser.add_argument("--allowlist", default=str(DEFAULT_ALLOWLIST), help="白名单文件路径")
    args = parser.parse_args(argv)

    allowlist_path = Path(args.allowlist)
    if not allowlist_path.is_file():
        print(f"[license] 缺少白名单文件：{allowlist_path}", file=sys.stderr)
        return 2
    allowed = _load_allowlist(allowlist_path)

    try:
        packages = _run_pip_licenses()
    except (RuntimeError, json.JSONDecodeError) as error:
        print(f"[license] {error}", file=sys.stderr)
        return 2

    display, versions, parents = _dependency_graph()

    scanned = 0
    violations: list[tuple[str, str, str, str]] = []  # (name, version, license, 原因)
    for package in packages:
        name = (package.get("Name") or "").strip()
        if not name or _normalize_name(name) in IGNORED_PACKAGES:
            continue
        scanned += 1
        version = (package.get("Version") or "").strip()
        raw_license = package.get("License") or "UNKNOWN"
        licenses = _normalize_license(raw_license)

        hard_denied = [item for item in licenses if _denied(item)]
        if hard_denied:
            violations.append(
                (name, version, "/".join(hard_denied), f"硬拒绝（{raw_license}）")
            )
            continue
        unknown = [item for item in licenses if item not in allowed]
        if unknown:
            violations.append(
                (name, version, "/".join(unknown), f"不在白名单（原始值：{raw_license}）")
            )
            continue

    print(f"[license] 白名单：{allowlist_path}（{len(allowed)} 项）")
    print(f"[license] 已扫描 {scanned} 个已安装依赖")
    if not violations:
        print("[license] OK：全部依赖均为白名单内的宽松许可证，无 AGPL/GPL/SSPL")
        return 0

    print(f"[license] FAIL：{len(violations)} 个依赖不通过", file=sys.stderr)
    for name, version, license_id, reason in violations:
        path = _chain(_normalize_name(name), parents, display, versions)
        print(f"  - {name} {version} | {license_id} | {reason}", file=sys.stderr)
        print(f"    引入路径：{path}", file=sys.stderr)
    print(
        "[license] 处理方式：换成宽松许可的替代包；确需保留则先评估并更新 contracts/deps/license-allowlist.txt"
        "（AGPL/GPL/SSPL 不允许加白）",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
