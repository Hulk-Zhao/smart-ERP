"""SQLAlchemy 2.0 声明式基类。

所有对象强制落在 ``erp`` schema；禁止无 schema 限定的表（C14 / §1A.7.4）。
"""

from __future__ import annotations

from sqlalchemy import MetaData
from sqlalchemy.orm import DeclarativeBase

SCHEMA = "erp"

NAMING_CONVENTION = {
    "ix": "%(table_name)s_%(column_0_name)s_idx",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(schema=SCHEMA, naming_convention=NAMING_CONVENTION)
