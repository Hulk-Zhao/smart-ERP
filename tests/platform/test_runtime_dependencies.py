"""运行期第三方依赖的声明口径：硬依赖进 core，软依赖进 extra 且必须条件导入。

背景：``apps/connectors``（``errors.py``、``u8/*``）曾在模块顶层 import ``httpx``，
但 ``httpx`` 当时只在 ``[project.optional-dependencies].dev`` 里，connector-gw 运行镜像
装不到，容器启动即 ``ModuleNotFoundError``。

因此本测试对「随镜像发布的 Python 目录」（``SCAN_ROOTS``）做全量 AST 扫描，并按两条
铁律判定：

1. **顶层硬导入**（模块 import 时无条件执行）的第三方包必须声明在
   ``[project.dependencies]``；
2. 只声明在**任一 optional extra**（如 ``[project.optional-dependencies].pdf``）里的包
   允许使用，但该 import 必须是**条件导入**——``try/except ImportError`` 包裹，或写在
   函数体里。顶层硬导入 + 仅 extra 声明 = 违规：运行镜像默认不装 extra，启动即崩。

「条件导入」的判定口径（见 ``_catches_import_error`` / ``_module_imports``）：
- import 语句位于函数体（含异步函数、嵌套函数）内 → 条件导入；
- import 语句位于 ``try`` 体，且该 ``try`` 至少有一个 handler 捕获
  ``ImportError`` / ``ModuleNotFoundError`` / ``Exception`` / ``BaseException``
  或裸 ``except:`` → 条件导入；
- ``except`` handler 体本身只在异常时执行 → 条件导入；
- 其余位置（模块顶层、``if`` 分支、``with``/``for``/``while`` 体、``try`` 的
  ``else`` 与 ``finally``、类体之外的一切同步执行路径）→ 顶层硬导入。

扫描范围口径：
- 纳入 ``apps/``、``db/``、``observability/``：都会被镜像带走或由部署期命令执行
  （``db/migrations`` 由 ``alembic upgrade`` 跑，``observability`` 随 api 镜像发布）；
- 不纳入 ``tests/``：只在开发机/CI 跑，第三方包由 ``.[dev]`` 提供；
- 不纳入 ``scripts/``、``web/``、``fixtures/``：当前分别只有 shell 脚本、前端工程与
  静态夹具，没有运行期 Python 第三方 import；将来出现 .py 时再加进 ``SCAN_ROOTS``。
"""

from __future__ import annotations

import ast
import sys
import tomllib
from importlib.metadata import packages_distributions

from apps.platform.contracts.export_contracts import ROOT

# 扫描范围内允许出现的「仅测试用」第三方包，键为规范化发行名（PEP 503）。
# 当前为空：运行期模块不应 import 测试专用包。确需例外时在此登记并写明理由，
# 例如 "pytest": "仅 apps/xxx/testing.py 的夹具使用，不进运行镜像"。
DEV_ONLY_ALLOWED: dict[str, str] = {}

# 随镜像发布 / 部署期执行的 Python 目录（口径见模块 docstring）。
SCAN_ROOTS: tuple[str, ...] = ("apps", "db", "observability")

# 捕获这些异常（或裸 except）时，try 体内的 import 才算「缺包也不致命」。
_IMPORT_ERRORS = frozenset({"ImportError", "ModuleNotFoundError", "Exception", "BaseException"})


def _normalize(name: str) -> str:
    """PEP 503 规范化：大小写、下划线、点号统一成短横线。"""

    return name.lower().replace("_", "-").replace(".", "-")


def _requirement_name(spec: str) -> str:
    """从 ``httpx>=0.27`` / ``uvicorn[standard]>=0.30`` 里取出发行名。"""

    for separator in "[<>=!~; ":
        spec = spec.split(separator, 1)[0]
    return _normalize(spec)


def _project_table() -> dict:
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return data["project"]


def _declared_dependencies() -> set[str]:
    return {_requirement_name(spec) for spec in _project_table()["dependencies"]}


def _optional_dependency_extras() -> dict[str, str]:
    """``{"pypdfium2": "pdf"}``：可选 extra 里的发行名 → extra 名。"""

    extras = _project_table().get("optional-dependencies", {}) or {}
    result: dict[str, str] = {}
    for extra, specs in extras.items():
        for spec in specs:
            result.setdefault(_requirement_name(spec), extra)
    return result


def _first_party_roots() -> set[str]:
    """仓库根下的目录都视为本项目包（apps/contracts/db/web…），不算第三方。"""

    return {
        entry.name
        for entry in ROOT.iterdir()
        if entry.is_dir() and not entry.name.startswith(".")
    }


def _catches_import_error(handler: ast.ExceptHandler) -> bool:
    """该 except handler 是否能把「包没装」吞下来。"""

    if handler.type is None:  # 裸 except:
        return True
    candidates = handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type]
    return any(isinstance(node, ast.Name) and node.id in _IMPORT_ERRORS for node in candidates)


def _module_imports(tree: ast.Module) -> tuple[set[str], set[str]]:
    """返回 ``(hard, soft)``：顶层硬导入的顶层模块名、条件导入的顶层模块名。"""

    hard: set[str] = set()
    soft: set[str] = set()

    def record(node: ast.Import | ast.ImportFrom, conditional: bool) -> None:
        target = soft if conditional else hard
        if isinstance(node, ast.Import):
            target.update(alias.name.split(".")[0] for alias in node.names)
        elif node.level == 0 and node.module:
            target.add(node.module.split(".")[0])

    def walk(statements: list[ast.stmt], conditional: bool) -> None:
        for node in statements:
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                record(node, conditional)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                # 函数体只在调用时执行 → 条件导入
                walk(node.body, True)
            elif isinstance(node, ast.If):
                walk(node.body, conditional)
                walk(node.orelse, conditional)
            elif isinstance(node, ast.Try):
                guarded = conditional or any(
                    _catches_import_error(handler) for handler in node.handlers
                )
                walk(node.body, guarded)
                for handler in node.handlers:
                    walk(handler.body, True)
                walk(node.orelse, conditional)
                walk(node.finalbody, conditional)
            elif isinstance(node, (ast.With, ast.AsyncWith)):
                walk(node.body, conditional)
            elif isinstance(node, (ast.For, ast.AsyncFor, ast.While)):
                walk(node.body, conditional)
                walk(node.orelse, conditional)

    walk(tree.body, False)
    return hard, soft


def _third_party_imports() -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    """``{"httpx": {"apps/connectors/u8/factory.py"}}``，按硬/软两组返回。"""

    excluded = set(sys.stdlib_module_names) | set(sys.builtin_module_names)
    excluded |= _first_party_roots()

    hard: dict[str, set[str]] = {}
    soft: dict[str, set[str]] = {}
    for root in SCAN_ROOTS:
        for path in sorted((ROOT / root).rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            relative = path.relative_to(ROOT).as_posix()
            module_hard, module_soft = _module_imports(tree)
            for module in module_hard - excluded:
                hard.setdefault(module, set()).add(relative)
            # 同一模块只要有一处硬导入，整体按硬导入判定
            for module in module_soft - excluded - module_hard:
                soft.setdefault(module, set()).add(relative)
    return hard, soft


def _violations(
    hard: dict[str, set[str]],
    soft: dict[str, set[str]],
    *,
    declared: set[str],
    extras: dict[str, str],
    distributions: dict[str, list[str]],
) -> list[str]:
    """按两条铁律给出违规清单（空列表 = 合规）。"""

    violations: list[str] = []

    for module, files in sorted(hard.items()):
        # import 名与发行名可能不同（如 sqlalchemy -> SQLAlchemy）
        candidates = distributions.get(module) or [module]
        if any(
            _normalize(name) in declared or _normalize(name) in DEV_ONLY_ALLOWED
            for name in candidates
        ):
            continue
        detail = (
            f"  - {module}（发行名 {', '.join(candidates)}）被 "
            f"{', '.join(sorted(files))} 顶层硬导入"
        )
        extra_names = sorted({extras[_normalize(name)] for name in candidates if _normalize(name) in extras})
        if extra_names:
            detail += (
                f"，但它只声明在 optional extra {extra_names}——extra 包必须条件导入"
                "（try/except ImportError 或函数体内导入），否则运行镜像启动即崩"
            )
        else:
            detail += "，且未声明在 [project.dependencies]"
        violations.append(detail)

    for module, files in sorted(soft.items()):
        if module in hard:
            continue
        candidates = distributions.get(module) or [module]
        if any(
            _normalize(name) in declared
            or _normalize(name) in extras
            or _normalize(name) in DEV_ONLY_ALLOWED
            for name in candidates
        ):
            continue
        violations.append(
            f"  - {module}（发行名 {', '.join(candidates)}）被 "
            f"{', '.join(sorted(files))} 条件导入，但既未声明在 [project.dependencies] "
            "也未声明在任何 optional extra"
        )

    return violations


def test_runtime_third_party_imports_are_declared() -> None:
    hard, soft = _third_party_imports()
    violations = _violations(
        hard,
        soft,
        declared=_declared_dependencies(),
        extras=_optional_dependency_extras(),
        distributions=packages_distributions(),
    )

    assert not violations, (
        "运行期模块的第三方 import 不符合声明口径（硬导入→core；软导入→extra 且仅条件导入）；"
        "这些包不会装进运行镜像（connector-gw 曾因此启动即 ModuleNotFoundError）：\n"
        + "\n".join(violations)
    )


def _scan(source: str) -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    """把源码片段扫成 ``_violations`` 需要的形态（文件名用占位符）。"""

    hard, soft = _module_imports(ast.parse(source))
    return {module: {"<test>"} for module in hard}, {module: {"<test>"} for module in soft}


def test_extra_only_hard_import_is_rejected() -> None:
    """只在 extra 里声明却在模块顶层硬导入 → 必须报错（正是要防的混淆）。"""

    hard, soft = _scan("import softonly\n")

    violations = _violations(
        hard,
        soft,
        declared=set(),
        extras={"softonly": "pdf"},
        distributions={},
    )

    assert soft == {}
    assert len(violations) == 1
    assert "顶层硬导入" in violations[0]
    assert "pdf" in violations[0]


def test_extra_only_conditional_import_is_allowed() -> None:
    """条件导入 + extra 声明 → 合规软依赖，不报错。"""

    sources = {
        "try/except ImportError": """
try:
    import softonly
except ImportError:
    softonly = None
""",
        "函数体内导入": """
def load():
    import softonly
    return softonly
""",
    }

    for label, source in sources.items():
        hard, soft = _scan(source)
        violations = _violations(
            hard,
            soft,
            declared=set(),
            extras={"softonly": "pdf"},
            distributions={},
        )

        assert "softonly" in soft, label
        assert "softonly" not in hard, label
        assert violations == [], f"{label} 应判为合规软依赖：{violations}"


def test_conditional_import_without_any_declaration_is_rejected() -> None:
    """条件导入也不能凭空使用未声明的第三方包（门禁不放松）。"""

    hard, soft = _scan("def load():\n    import softonly\n")

    violations = _violations(
        hard,
        soft,
        declared=set(),
        extras={},
        distributions={},
    )

    assert len(violations) == 1
    assert "既未声明在 [project.dependencies] 也未声明在任何 optional extra" in violations[0]
