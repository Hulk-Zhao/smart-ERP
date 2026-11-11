#!/usr/bin/env python3
"""镜像启动冒烟契约：compose 里真跑的每个入口，CI 构建后必须真跑一次。

2026-11-11 同族事故两次：
- main 新增 observability/ 未进 packages.find.include：四个镜像 build 全绿，api 容器
  一启动就 ModuleNotFoundError（157929d 修）。
- 修完只给 app 镜像加了 `import apps.main`，但同一个镜像还被 worker / scheduler 使用
  （`python -m core.runtime.worker` / `core.runtime.scheduler`），这两个入口的导入图
  没有任何一步碰过——同一种病换个入口照样能复发。

本用例把口径钉死（是红/绿，不是注释约定）：
1. 冒烟必须覆盖 image-build 矩阵里的**每个镜像**（缺 case 分支即红）；
2. 每个镜像的**每个真实入口**都必须出现在该镜像的冒烟分支里：
   - compose `command:` 声明的入口（`uvicorn <mod>:app` / `python -m <mod>` /
     `python <script>.py` / 可执行文件如 alembic）；
   - compose 没写 command 时，用 Dockerfile 的 `CMD` 兜底（如 web 的 nginx）。
3. 新增镜像 = workflow matrix 多一行；新增入口 = compose/Dockerfile 多一行。
   两者都会立刻让本用例要求同步补冒烟，否则该 job 会退化成「证明某个包能 import」。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "smart-erp.yml"
COMPOSE = REPO_ROOT / "deploy" / "smart-erp" / "docker-compose.yml"


def _matrix_images(text: str) -> list[tuple[str, str]]:
    section = text.split("image-build:", 1)[1].split("steps:", 1)[0]
    return re.findall(r"-\s+image:\s*([\w-]+)\s*\n\s+dockerfile:\s*(\S+)", section)


def _smoke_arms(text: str) -> dict[str, str]:
    """取出「启动冒烟」步骤里 case 的每个分支文本（键 = 镜像名）。"""
    lines = text.splitlines()
    start = next(i for i, line in enumerate(lines) if "启动冒烟" in line)
    block: list[str] = []
    for line in lines[start + 1:]:
        if re.match(r"^ {6}- ", line) or re.match(r"^ {2}\S", line):
            break
        block.append(line)
    smoke = "\n".join(block)
    arms = dict(
        re.findall(
            r"(?ms)^ {12}([\w-]+)\)(.*?)(?=^ {12}[\w-]+\)|^ {10}esac)", smoke
        )
    )
    assert arms, f"未能从「启动冒烟」步骤解析出 case 分支：\n{smoke}"
    return arms


def _dockerfile_cmd(dockerfile: str) -> list[str]:
    text = (REPO_ROOT / dockerfile).read_text(encoding="utf-8")
    match = re.search(r"^CMD\s+(\[.*\])\s*$", text, re.M)
    return json.loads(match.group(1)) if match else []


def _entry_tokens(command) -> set[str]:
    """把一个 command 翻译成「冒烟里必须出现的字符串」。

    只认这几种真实入口形态；其它形态按可执行文件名兜底（如 nginx / alembic），
    这样新写法不会被静默忽略。
    """
    if not command:
        return set()
    cmd = list(command)
    if cmd[0] == "uvicorn" and len(cmd) > 1:
        return {cmd[1].split(":")[0]}
    if cmd[:2] == ["python", "-m"] and len(cmd) > 2:
        return {cmd[2]}
    if cmd[0] == "python" and len(cmd) > 1 and cmd[1].endswith(".py"):
        return {cmd[1].rsplit("/", 1)[-1]}
    return {cmd[0]}


def _image_key(image: str) -> str:
    match = re.search(r"smart-erp/([\w-]+):", image or "")
    return match.group(1) if match else ""


def test_every_image_entry_is_smoked() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    images = _matrix_images(text)
    assert images, "未能从 image-build 矩阵解析出镜像列表"
    arms = _smoke_arms(text)
    compose = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))

    problems: list[str] = []
    for image, dockerfile in images:
        if image not in arms:
            problems.append(f"{image}: 冒烟缺少 case 分支")
            continue
        tokens = _entry_tokens(_dockerfile_cmd(dockerfile))
        for service, spec in (compose.get("services") or {}).items():
            spec = spec or {}
            if _image_key(spec.get("image", "")) != image:
                continue
            command = spec.get("command")
            if command is None:
                # compose 未覆盖 command：入口由镜像 CMD 决定（上面已取过）
                continue
            service_tokens = _entry_tokens(command)
            if not service_tokens:
                problems.append(f"{image}: 服务 {service} 的入口无法识别")
            tokens |= service_tokens
        missing = sorted(token for token in tokens if token not in arms[image])
        if missing:
            problems.append(f"{image}: 冒烟未覆盖入口 {missing}")

    assert not problems, (
        "镜像启动冒烟与真实入口不一致：\n- "
        + "\n- ".join(problems)
        + "\n规则：新增镜像或新增 CMD/command 入口，必须同步补 "
        "`.github/workflows/smart-erp.yml` 的「启动冒烟」分支；"
        "否则漏包仍会 build 绿、容器启动才红。"
    )
