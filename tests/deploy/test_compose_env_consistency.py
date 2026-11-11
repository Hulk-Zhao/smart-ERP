"""compose 环境变量 ↔ 代码读取点的一致性（B-COMPOSE-METRICS）。

判定规则是可执行的，不靠注释记住：
1. 枚举 ``deploy/smart-erp/docker-compose.yml`` 所有服务 ``environment`` 段的 ERP_* 键；
2. 每个键必须至少有一个读取点：
   - 运行时代码/脚本里出现键名字面量（shell 的 ``${ERP_X}``、Python 的 ``"ERP_X"`` 都算），或
   - ``apps/platform/config.py`` 风格的前缀式读取 ``_env("X")`` / ``_int_env("X")``；
3. 否则必须登记在 ``contracts/compose_env_exceptions.json``，且 owner / reason / card 齐全。

三条防退化断言：
- 例外里的键必须仍在 compose 的 environment 段（compose 删了却留例外 = 僵尸条目 → 红）；
- 每条例外必须有 owner、reason、card（缺一即红）；
- 例外总数必须恰好等于 ``MAX_COMPOSE_ENV_EXCEPTIONS``：只许随修复下调。新登记例外必须
  显式上调这个常量——制造一次必须解释的改动，而不是悄悄多一行。

扫描范围只含运行时代码（apps/core/tools/observability/scripts/db 与 docker 脚本），不含
tests/docs：测试里提到键名不算「代码读了」。
"""

from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
COMPOSE_FILE = REPO_ROOT / "deploy" / "smart-erp" / "docker-compose.yml"
EXCEPTIONS_FILE = REPO_ROOT / "contracts" / "compose_env_exceptions.json"

CODE_ROOTS = (
    "apps",
    "core",
    "tools",
    "observability",
    "scripts",
    "db",
    "projects/smart-ERP/docker",
)
CODE_SUFFIXES = {".py", ".sh", ".conf", ".ini"}

# 只许下调：每修复一条就把这个数减一；新增例外必须显式上调并写清理由。
MAX_COMPOSE_ENV_EXCEPTIONS = 16

REQUIRED_EXCEPTION_FIELDS = ("owner", "reason", "card")


def _compose_env_keys() -> set[str]:
    services = (yaml.safe_load(COMPOSE_FILE.read_text(encoding="utf-8")) or {}).get(
        "services"
    ) or {}
    keys: set[str] = set()
    for spec in services.values():
        env = (spec or {}).get("environment") or {}
        if isinstance(env, dict):
            keys.update(str(k) for k in env if str(k).startswith("ERP_"))
    return keys


def _exceptions() -> dict[str, dict]:
    data = json.loads(EXCEPTIONS_FILE.read_text(encoding="utf-8"))
    return data.get("exceptions") or {}


def _code_texts() -> list[str]:
    texts: list[str] = []
    for root in CODE_ROOTS:
        base = REPO_ROOT / root
        if not base.exists():
            continue
        for path in base.rglob("*"):
            if path.suffix in CODE_SUFFIXES and path.is_file():
                texts.append(path.read_text(encoding="utf-8", errors="ignore"))
    return texts


def _has_reader(key: str, texts: list[str]) -> bool:
    suffix = key[len("ERP_"):]
    prefix_pattern = re.compile(rf'_(?:int_)?env\(\s*"{re.escape(suffix)}"')
    return any(key in text or prefix_pattern.search(text) for text in texts)


class ComposeEnvConsistencyTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.env_keys = _compose_env_keys()
        cls.exceptions = _exceptions()
        cls.texts = _code_texts()

    def test_every_compose_env_is_read_or_registered(self) -> None:
        """compose 下发的每个 ERP_* 键，要么有读取点，要么在例外表里记账。"""
        unread = sorted(
            key
            for key in self.env_keys
            if not _has_reader(key, self.texts) and key not in self.exceptions
        )
        self.assertEqual(
            unread,
            [],
            "compose 下发了但代码从不读取这些键："
            f"{unread}。要么接线，要么从 compose 删除；确实将来会接的，"
            f"登记到 {EXCEPTIONS_FILE.relative_to(REPO_ROOT)} 并写明 owner/reason/card。",
        )

    def test_exception_entry_fields_are_complete(self) -> None:
        """例外必须能追责：owner 流、原因、跟踪卡号缺一不可。"""
        incomplete = {
            key: [field for field in REQUIRED_EXCEPTION_FIELDS if not str(entry.get(field, "")).strip()]
            for key, entry in self.exceptions.items()
            if any(not str(entry.get(field, "")).strip() for field in REQUIRED_EXCEPTION_FIELDS)
        }
        self.assertEqual(
            incomplete,
            {},
            f"例外条目缺 owner/reason/card：{incomplete}",
        )

    def test_exception_entries_are_still_in_compose(self) -> None:
        """僵尸条目：compose 已删掉的键不能继续留在例外表里。"""
        zombies = sorted(set(self.exceptions) - self.env_keys)
        self.assertEqual(
            zombies,
            [],
            f"这些键已不在 compose environment 里，例外登记过期，请删除：{zombies}",
        )

    def test_exception_count_matches_declared_cap(self) -> None:
        """例外总数写死在这里，只许随修复下调。"""
        self.assertEqual(
            len(self.exceptions),
            MAX_COMPOSE_ENV_EXCEPTIONS,
            f"例外数 {len(self.exceptions)} 与上限 {MAX_COMPOSE_ENV_EXCEPTIONS} 不一致。"
            "修好一条后请把 MAX_COMPOSE_ENV_EXCEPTIONS 同步下调；"
            "若确实要新增例外，请先说明为什么它不是「接线或删除」能解决的。",
        )


if __name__ == "__main__":
    unittest.main()
