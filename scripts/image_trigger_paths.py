#!/usr/bin/env python3
"""镜像构建触发范围的单一来源 + 仓库顶层条目自检。

背景：CI 的 `changes` job 过去用一行 grep 正则决定「要不要重跑镜像构建」。正则和
仓库目录是两处事实——新增一个顶层目录（例如 T204 的 `domain/`、更早的 `core/`）
没人会记得同步正则，结果是「代码变了、镜像没变、没有任何红灯」。

本脚本把两件事放在同一个文件里：

- `match`：从 stdin 读 `git diff --name-only` 的结果，命中镜像相关路径输出 `true`；
- `check`：拿 `git ls-tree` 的顶层条目逐条对照，既不在触发列表、也不在
  `IRRELEVANT` 里的条目会被点名并以 1 退出（CI 直接红）。

新增顶层目录时的正确做法：要么加进 `TRIGGER_PREFIXES`（会影响镜像），要么加进
`IRRELEVANT` 并写清楚为什么不会影响镜像。不要改成「静默跳过」。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

# 命中任一前缀 / 根文件 → 必须重跑镜像构建（matrix 四个镜像）
TRIGGER_PREFIXES = (
    ".github/",  # CI 自身：构建参数、Dockerfile 调用一改，镜像必须重验
    "apps/",  # api / worker / scheduler / connector-gw 源码
    "contracts/",  # web 构建期拷贝的契约
    "core/",  # 运行时公共库
    "db/",  # 迁移 SQL，随 app 镜像发布
    "demo/",  # 演示数据的 schema / 装载脚本
    "deploy/",  # compose / .env.example / 发布清单
    "domain/",  # 历史/规划命名：一旦目录回归也必须触发
    "fixtures/",  # 解析器等镜像内会读取的夹具
    "observability/",  # 指标/告警/导出器代码与规则
    "projects/",  # 四个 Dockerfile 与 nginx 配置
    "scripts/",  # 构建与校验脚本
    "tests/",  # 部署面测试往往伴随编排/镜像变化
    "tools/",  # 运行期辅助工具
    "web/",  # web 镜像源码
)

TRIGGER_FILES = (
    "alembic.ini",  # 迁移入口，随 app 镜像发布
    "pyproject.toml",  # 依赖与打包配置，影响全部 Python 镜像
)

# 明确不影响镜像的顶层条目：新增条目必须在这里写明理由，否则 check 会红
IRRELEVANT = {
    "docs/": "产品/技术文档，不进镜像",
    "README.md": "仓库说明",
    ".gitignore": "git 忽略规则",
    ".gitattributes": "git 属性",
    ".git-blame-ignore-revs": "blame 忽略清单",
}


def is_trigger(path: str) -> bool:
    """判断单个变更文件是否属于镜像相关路径。"""
    normalized = path.replace("\\", "/").removeprefix("./")
    if normalized in TRIGGER_FILES:
        return True
    return any(normalized.startswith(prefix) for prefix in TRIGGER_PREFIXES)


def top_level_entries() -> list[str]:
    """仓库根的全部被跟踪条目（目录不带结尾斜杠，与 git ls-tree 一致）。"""
    try:
        out = subprocess.run(
            ["git", "ls-tree", "--name-only", "HEAD"],
            cwd=REPO_ROOT,
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        return sorted(line.strip() for line in out.splitlines() if line.strip())
    except (OSError, subprocess.CalledProcessError):
        # 非 git 环境（例如直接拷贝目录）：退回文件系统扫描
        return sorted(
            p.name for p in REPO_ROOT.iterdir() if p.name not in {".git", "__pycache__"}
        )


def _key(entry: str) -> str:
    """目录用 `name/`、文件用 `name`，与两张表的写法对齐。"""
    return entry + "/" if (REPO_ROOT / entry).is_dir() else entry


def uncovered_entries() -> list[str]:
    """既不在触发列表、也没有明确豁免的顶层条目。"""
    missing = []
    for entry in top_level_entries():
        key = _key(entry)
        if key in IRRELEVANT:
            continue
        if is_trigger(key) or is_trigger(entry):
            continue
        missing.append(key)
    return missing


def _write_summary(lines: list[str]) -> None:
    """同时写 job summary（CI 里能直接看到哪些条目被覆盖/豁免）。"""
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not summary_path:
        return
    with open(summary_path, "a", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")


def check() -> int:
    covered = [p for p in TRIGGER_PREFIXES + TRIGGER_FILES]
    missing = uncovered_entries()
    _write_summary(
        [
            "### 镜像触发范围自检",
            "",
            f"- 触发前缀：{len(TRIGGER_PREFIXES)} 个 / 触发根文件：{len(TRIGGER_FILES)} 个",
            f"- 显式豁免：{', '.join(IRRELEVANT)}",
            f"- 未覆盖的顶层条目：{len(missing) or '无'}",
        ]
    )
    if not missing:
        print(f"ok：顶层条目均已覆盖（{len(covered)} 条规则）")
        return 0
    for entry in missing:
        print(f"::error::顶层条目 {entry} 既不在镜像触发列表，也不在 IRRELEVANT 里", file=sys.stderr)
    print(
        "修复：会影响镜像 → 加进 scripts/image_trigger_paths.py 的 TRIGGER_PREFIXES/"
        "TRIGGER_FILES；确实无关 → 加进 IRRELEVANT 并写明理由。",
        file=sys.stderr,
    )
    return 1


def match() -> int:
    changed = [line.strip() for line in sys.stdin.read().splitlines() if line.strip()]
    print("true" if any(is_trigger(path) for path in changed) else "false")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args == ["match"]:
        return match()
    if args == ["check"]:
        return check()
    print("用法：python scripts/image_trigger_paths.py <match|check>", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
