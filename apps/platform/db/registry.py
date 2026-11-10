"""中央模型注册表：模型发现与表清单的唯一入口。

迁移测试如果逐个硬编码 ``import`` 各流的 models，每新增一个流都要改测试；
更糟的是「某个流加了表但测试没登记」时不会报警。本模块集中登记候选模型模块，
对外的 ``table_names()`` 就是「代码里声明过的全部表」。

约定：

- 各流可以各自持有独立 ``MetaData``（S4 为了隔离 autogenerate 刻意不与 ``Base`` 合并），
  这里按 ``DeclarativeBase`` 子类收集其 metadata，去重后返回；
- **可选导入**：模块尚未落地（如 S1 的 ``apps/connectors/db/models.py`` 还没提交）时
  记为 ``missing`` 并继续，不让整棵树因为一个缺失模块全红；
- 模块存在但自身 import 报错（依赖名写错等）时原样抛出，避免掩盖真错误。
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass, field
from types import ModuleType

from sqlalchemy import MetaData, Table
from sqlalchemy.orm import DeclarativeBase

# 新增流只需把模块加到这里（顺序即加载顺序，便于排查）
MODEL_MODULES: tuple[str, ...] = (
    "apps.platform.db.models",
    "apps.agents.models",
    "apps.audit.models",
    "apps.security.models",
    "apps.connectors.db.models",
    "apps.recon.db",
    "apps.api.db",
)


@dataclass(frozen=True)
class ModelModule:
    """单个候选模块的加载结果。"""

    name: str
    metadata: tuple[MetaData, ...] = field(default_factory=tuple)
    missing: str | None = None

    @property
    def loaded(self) -> bool:
        return self.missing is None


def _metadata_from(module: ModuleType) -> tuple[MetaData, ...]:
    seen: dict[int, MetaData] = {}
    for value in vars(module).values():
        if isinstance(value, type) and issubclass(value, DeclarativeBase):
            metadata = getattr(value, "metadata", None)
            if isinstance(metadata, MetaData):
                seen[id(metadata)] = metadata
    return tuple(seen.values())


def load_model_modules(modules: tuple[str, ...] = MODEL_MODULES) -> tuple[ModelModule, ...]:
    """逐个加载候选模块；缺失模块记录原因而不是抛异常。"""

    results: list[ModelModule] = []
    for name in modules:
        try:
            module = importlib.import_module(name)
        except ModuleNotFoundError as exc:
            missing = exc.name or ""
            # 只有「缺的就是这个模块自身或它的父包」才算可选缺失；
            # 缺的是模块内部依赖时说明模块写错了，必须暴露。
            if missing and (name == missing or name.startswith(f"{missing}.")):
                results.append(
                    ModelModule(
                        name=name,
                        missing=f"{missing} 未提供（{name} 所属流尚未落地），已跳过",
                    )
                )
                continue
            raise
        results.append(ModelModule(name=name, metadata=_metadata_from(module)))
    return tuple(results)


def metadata_objects(modules: tuple[str, ...] = MODEL_MODULES) -> tuple[MetaData, ...]:
    """去重后的全部 metadata。"""

    seen: dict[int, MetaData] = {}
    for module in load_model_modules(modules):
        for metadata in module.metadata:
            seen[id(metadata)] = metadata
    return tuple(seen.values())


def table_map(modules: tuple[str, ...] = MODEL_MODULES) -> dict[str, Table]:
    """``{"erp.ticket": Table}``；同名表出现在两个 metadata 里直接报错。"""

    owners: dict[str, str] = {}
    tables: dict[str, Table] = {}
    for module in load_model_modules(modules):
        for metadata in module.metadata:
            for table in metadata.tables.values():
                qualified = f"{table.schema}.{table.name}"
                existing = tables.get(qualified)
                if existing is not None:
                    if existing is table:
                        # 各流共用 S0 的 Base，同一个 Table 对象会被多个模块收集
                        continue
                    raise RuntimeError(
                        f"表 {qualified} 由 {owners[qualified]} 与 {module.name} 声明为"
                        "两个不同的 Table 对象；迁移测试无法判定归属，请先消除重复定义"
                    )
                tables[qualified] = table
                owners[qualified] = module.name
    return tables


def table_names(modules: tuple[str, ...] = MODEL_MODULES) -> frozenset[str]:
    """代码里声明过的全部表（含尚未合并进迁移的表）。"""

    return frozenset(table_map(modules))
