#!/usr/bin/env python3
"""校验 `contracts/config.schema.json` 与编排/环境文件的真实一致性（C11）。

不是「读一遍 schema 打印 OK」，而是双向核对：

1. **schema 自身**：`required` 列表与 `properties[*].required` 一致；`type` 取值合法。
2. **compose ↔ schema**：compose 里出现的每个 `${ERP_*}` 都必须在 schema 里登记；
   schema 里 `scope=compose` 的键必须真的出现在 compose；`${K:?}` 的键 `required` 必须为 true；
   `${K:-x}` 的默认值必须与 schema 的 `default` 完全相同类与值；
   同一个键在多个服务上出现**不同的默认值**直接判错（同一部署会出现两个取值）。
3. **env 文件**（`.env.example` 与三套环境矩阵）：未知键、类型不符、枚举/正则不符、
   必填缺失或为空，全部逐条报错。
4. **代码扫描**：`apps/` `core/` `tools/` 里按字面量读取的 `ERP_*` 必须已登记，
   防止「代码里悄悄读一个没人知道的键」。

用法：
    python scripts/validate_config_schema.py --all
    python scripts/validate_config_schema.py --env deploy/smart-erp/.env.example
    python scripts/validate_config_schema.py --matrix deploy/smart-erp/env

退出码：0 全部通过；1 有错误（每条错误都会打印出来）。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SCHEMA = ROOT / "contracts" / "config.schema.json"
DEFAULT_COMPOSE = ROOT / "deploy" / "smart-erp" / "docker-compose.yml"
DEFAULT_ENV = ROOT / "deploy" / "smart-erp" / ".env.example"
DEFAULT_MATRIX = ROOT / "deploy" / "smart-erp" / "env"
MATRIX_FILES = ("local.env", "demo.env", "prod.env")
CODE_DIRS = ("apps", "core", "tools", "observability", "db")

_VAR_RE = re.compile(r"\$\{(ERP_[A-Z0-9_]+)(?::([-?]))?([^}]*)\}")
_CODE_RE = re.compile(r"""["'](ERP_[A-Z0-9_]+)["']""")
_KV_RE = re.compile(r"^([A-Z][A-Z0-9_]*)=(.*)$")


class Report:
    def __init__(self) -> None:
        self.errors: list[str] = []

    def fail(self, msg: str) -> None:
        self.errors.append(msg)

    def check(self, cond: bool, msg: str) -> bool:
        if not cond:
            self.fail(msg)
        return cond

    def flush(self) -> int:
        if self.errors:
            print(f"FAIL: {len(self.errors)} 项不通过", file=sys.stderr)
            for i, msg in enumerate(self.errors, 1):
                print(f"  {i}. {msg}", file=sys.stderr)
            return 1
        return 0


def load_yaml(path: Path):
    import yaml  # 仓库已依赖（tests/deploy 解析 compose 用的同一个库）

    return yaml.safe_load(path.read_text(encoding="utf-8"))


def compose_usages(path: Path) -> tuple[dict[str, set[str]], dict[str, dict[str, str]], set[str], set[str]]:
    """返回 (键→服务集合, 键→{服务: 默认值}, 必填键集合, 全部键集合)。"""
    services = (load_yaml(path) or {}).get("services") or {}
    per_service: dict[str, set[str]] = {}
    required: set[str] = set()
    defaults_by_service: dict[str, dict[str, str]] = {}
    text = path.read_text(encoding="utf-8")
    for name, svc in services.items():
        env = (svc or {}).get("environment") or {}
        values = env.values() if isinstance(env, dict) else env
        for value in values:
            if not isinstance(value, str):
                continue
            for key, op, default in _VAR_RE.findall(value):
                per_service.setdefault(key, set()).add(name)
                if op == "?":
                    required.add(key)
                elif op == "-":
                    defaults_by_service.setdefault(key, {})[name] = default
    # 端口/网络等插值位置没有服务上下文，但要算作 compose 引用；
    # 只认 `${ERP_*…}` 形式，避免把注释里出现的裸键名当成引用。
    all_keys = {m[0] for m in _VAR_RE.findall(text)} | set(re.findall(r"\$\{(ERP_[A-Z0-9_]+)[^}]*\}", text))
    return per_service, defaults_by_service, required, all_keys


def parse_env_file(path: Path, rep: Report) -> dict[str, str]:
    values: dict[str, str] = {}
    for lineno, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = _KV_RE.match(line)
        if not m:
            rep.fail(f"{path.name}:{lineno} 不是 KEY=VALUE 形式：{line!r}")
            continue
        key, value = m.group(1), m.group(2)
        if key in values:
            rep.fail(f"{path.name}:{lineno} 重复定义 {key}")
        values[key] = value
    return values


def validate_schema(schema: dict, rep: Report) -> dict[str, dict]:
    props = schema.get("properties") or {}
    rep.check(bool(props), "schema.properties 为空")
    declared = {k for k, v in props.items() if v.get("required")}
    rep.check(
        declared == set(schema.get("required") or []),
        f"schema.required 与 properties[*].required 不一致：required={sorted(schema.get('required') or [])}，"
        f"properties 标记={sorted(declared)}",
    )
    for key, spec in props.items():
        rep.check(
            spec.get("type") in {"string", "integer", "boolean", "number"},
            f"{key}: type 非法 {spec.get('type')!r}",
        )
        rep.check(
            spec.get("scope") in {"compose", "build", "deploy", "code"},
            f"{key}: scope 非法 {spec.get('scope')!r}",
        )
    return props


def _same_default(raw: str, want) -> bool:
    if want is None:
        return raw == ""
    return str(want) == raw


def validate_compose(compose: Path, props: dict[str, dict], rep: Report) -> None:
    per_service, defaults_by_service, required, all_keys = compose_usages(compose)
    for key in sorted(all_keys):
        if key not in props:
            rep.fail(f"compose 引用了未登记的键 {key}（先加进 contracts/config.schema.json 与 .env.example）")
    for key, spec in sorted(props.items()):
        if spec.get("scope") != "compose":
            continue
        if key not in all_keys:
            rep.fail(f"schema 标记 {key} 为 compose 键，但 compose 里没有引用（登记已过期）")
            continue
        if spec.get("required") and key not in required:
            rep.fail(f"{key}: schema 说必填，但 compose 没有用 ${{{key}:?…}} 强制")
        if not spec.get("required") and key in required:
            rep.fail(f"{key}: compose 用 :? 强制了，但 schema 的 required 不是 true")
        actual = defaults_by_service.get(key) or {}
        declared = spec.get("defaultsByService")
        if declared:
            # 允许按服务差异化调参，但必须逐服务显式登记，不许出现「没人知道」的默认值
            if set(declared) != set(actual):
                rep.fail(f"{key}: defaultsByService 记录 {sorted(declared)} 与实际注入 {sorted(actual)} 不一致")
            for name, raw in sorted(actual.items()):
                if name in declared and not _same_default(raw, declared[name]):
                    rep.fail(f"{key}: 服务 {name} 的 compose 默认值 {raw!r} 与 defaultsByService 记录的 {declared[name]!r} 不一致")
        elif len(set(actual.values())) > 1:
            rep.fail(
                f"{key}: compose 里出现多个默认值 {sorted(set(actual.values()))}，"
                "要么统一，要么在 schema 的 defaultsByService 里逐服务登记"
            )
        elif actual and not _same_default(next(iter(actual.values())), spec.get("default")):
            rep.fail(f"{key}: compose 默认值 {next(iter(actual.values()))!r} 与 schema default={spec.get('default')!r} 不一致")
    # 服务清单：schema 记录的 services 必须与 compose 实际注入的服务一致
    for key, services in sorted(per_service.items()):
        spec = props.get(key) or {}
        recorded = set(spec.get("services") or [])
        if recorded != services:
            rep.fail(f"{key}: services 记录 {sorted(recorded)} 与实际注入 {sorted(services)} 不一致")
        # 反向约束：只要键进了任何一个 service 的 environment，它就是 compose 键。
        # 只改 services 不改 scope 的「半升级」会让登记继续撒谎，必须一起红。
        if key in props and spec.get("scope") != "compose":
            injected = "/".join(sorted(services))
            rep.fail(
                f"{key}: 该键被 {injected} 服务注入，scope 必须为 compose"
                f"（当前 {spec.get('scope')!r}）；若不应注入，请从 compose 移除"
            )


def validate_env(
    path: Path, props: dict[str, dict], rep: Report, *, label: str | None = None, template: bool = False
) -> dict[str, str]:
    """校验一个 env 文件。`template=True` 用于 `.env.example`：占位符允许留空，其余规则照旧。"""
    name = label or path.name
    values = parse_env_file(path, rep)
    for key, raw in values.items():
        spec = props.get(key)
        if spec is None:
            rep.fail(f"{name}: 未登记的键 {key}")
            continue
        if raw == "" and template:
            continue
        typ = spec.get("type")
        if typ == "integer":
            rep.check(re.fullmatch(r"-?\d+", raw) is not None, f"{name}: {key} 需要整数，实际 {raw!r}")
        elif typ == "boolean":
            rep.check(raw in {"true", "false"}, f"{name}: {key} 需要 true/false，实际 {raw!r}")
        if spec.get("enum"):
            rep.check(raw in spec["enum"], f"{name}: {key} 取值必须是 {spec['enum']}，实际 {raw!r}")
        if spec.get("pattern"):
            rep.check(
                re.fullmatch(spec["pattern"], raw) is not None,
                f"{name}: {key} 不符合格式 {spec['pattern']}，实际 {raw!r}",
            )
    for key, spec in sorted(props.items()):
        if not spec.get("required"):
            continue
        if key not in values:
            rep.fail(f"{name}: 缺少必填键 {key}")
        elif not values[key].strip() and not template:
            rep.fail(f"{name}: 必填键 {key} 为空")
    return values


def validate_matrix(matrix: Path, props: dict[str, dict], rep: Report) -> dict[str, dict[str, str]]:
    result: dict[str, dict[str, str]] = {}
    for filename in MATRIX_FILES:
        path = matrix / filename
        if not path.is_file():
            rep.fail(f"环境矩阵缺文件 {path}")
            continue
        result[filename] = validate_env(path, props, rep, label=f"env/{filename}")
    return result


def env_example_keys(path: Path) -> set[str]:
    keys: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        key = s.partition("=")[0].strip()
        if key.startswith("ERP_"):
            keys.add(key)
    return keys


def validate_code(props: dict[str, dict], rep: Report, env_example: Path | None = None) -> set[str]:
    """扫描代码里的 ERP_* 读点：必须已登记进 schema，且必须在 .env.example 有对应项。

    只做静态字面量扫描（`"ERP_XXX"`），不执行代码；这条断言针对的是
    「键在代码里被读、在 .env.example 里查不到」这类配置面说谎。
    """
    registered_in_env = env_example_keys(env_example) if env_example and env_example.exists() else None
    seen: set[str] = set()
    for dirname in CODE_DIRS:
        for path in (ROOT / dirname).rglob("*.py"):
            if "__pycache__" in path.parts:
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:  # pragma: no cover - 非 UTF-8 源文件极少
                continue
            for key in _CODE_RE.findall(text):
                seen.add(key)
                if key not in props:
                    rep.fail(f"{path.relative_to(ROOT)} 读取了未登记的键 {key}")
                elif registered_in_env is not None and key not in registered_in_env:
                    rep.fail(f"{path.relative_to(ROOT)} 读取了未在 .env.example 登记的键 {key}")
    return seen


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="校验 config schema 与编排/环境文件的一致性")
    ap.add_argument("--schema", type=Path, default=DEFAULT_SCHEMA)
    ap.add_argument("--compose", type=Path, default=DEFAULT_COMPOSE)
    ap.add_argument("--env", type=Path, default=None, help="校验单个 env 文件")
    ap.add_argument("--matrix", type=Path, default=None, help="校验三套环境矩阵目录")
    ap.add_argument("--scan-code", action="store_true", help="扫描代码里读取但未登记的 ERP_* 键")
    ap.add_argument("--all", action="store_true", help="等价于 --env .env.example --matrix env --scan-code")
    args = ap.parse_args(argv)

    rep = Report()
    schema = json.loads(args.schema.read_text(encoding="utf-8"))
    props = validate_schema(schema, rep)
    validate_compose(args.compose, props, rep)

    env_target = args.env
    matrix_target = args.matrix
    if args.all:
        env_target = env_target or DEFAULT_ENV
        matrix_target = matrix_target or DEFAULT_MATRIX
    if env_target:
        # `.env.example` 是模板：必填键可以先留空，真实取值由三套环境矩阵负责
        validate_env(env_target, props, rep, template=env_target.name == ".env.example")
    matrix = validate_matrix(matrix_target, props, rep) if matrix_target else {}
    if args.scan_code or args.all:
        validate_code(props, rep, DEFAULT_ENV)

    code = rep.flush()
    if code:
        return code
    print(
        f"PASS: {len(props)} 个键 · compose 覆盖一致 · "
        f"env={'ok' if env_target else 'skipped'} · matrix={','.join(sorted(matrix)) or 'skipped'}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
