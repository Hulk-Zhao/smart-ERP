"""中央模型注册表（apps/platform/db/registry.py）的单元测试。

重点保护两件事：
1. 各流共用 S0 的 ``Base``（同一 metadata、同一 Table 对象）时，registry 不能误判重复；
2. 真正的重复定义（同名但不同 Table 对象）仍必须报警，不能被上面的去重规则掩盖。
"""

from __future__ import annotations

import sys
from types import ModuleType

import pytest
from sqlalchemy import Column, Integer, MetaData, Table
from sqlalchemy.orm import DeclarativeBase

from apps.platform.db.base import Base
from apps.platform.db.registry import load_model_modules, table_map


def _fake_module(monkeypatch, name: str, **attrs: object) -> str:
    module = ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    monkeypatch.setitem(sys.modules, name, module)
    return name


def test_same_table_object_from_two_modules_is_counted_once(monkeypatch) -> None:
    first = _fake_module(monkeypatch, "fake_models_first", Base=Base)
    second = _fake_module(monkeypatch, "fake_models_second", Base=Base)

    tables = table_map((first, second))

    expected = Base.metadata.tables["erp.ticket"]
    assert tables["erp.ticket"] is expected


def test_same_name_different_table_objects_still_raise(monkeypatch) -> None:
    class FirstBase(DeclarativeBase):
        metadata = MetaData(schema="erp")

    class SecondBase(DeclarativeBase):
        metadata = MetaData(schema="erp")

    Table("ticket", FirstBase.metadata, Column("id", Integer, primary_key=True))
    Table("ticket", SecondBase.metadata, Column("id", Integer, primary_key=True))

    first = _fake_module(monkeypatch, "fake_conflict_first", FirstBase=FirstBase)
    second = _fake_module(monkeypatch, "fake_conflict_second", SecondBase=SecondBase)

    with pytest.raises(RuntimeError, match="erp.ticket"):
        table_map((first, second))


def test_missing_module_is_reported_not_raised() -> None:
    modules = load_model_modules(("fake.not_landed.models",))

    assert len(modules) == 1
    assert not modules[0].loaded
    assert modules[0].missing and "fake.not_landed.models" in modules[0].missing
