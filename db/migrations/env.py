"""Alembic 环境：schema erp + 版本表 alembic_version_erp（C5）。

- 版本表固定 ``erp.alembic_version_erp``，与业务表同 schema，便于授权隔离；
- 连接串唯一真源是 ``ERP_DATABASE_URL``（C14），不在 alembic.ini 里维护第二份；
- ``--sql`` 离线模式可用，产物以 ``CREATE SCHEMA IF NOT EXISTS erp`` 开头，可直接审阅。
"""

from __future__ import annotations

import asyncio
import io
import os
import sys
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool, text

from apps.platform.db.base import SCHEMA, Base
from apps.platform.db import models  # noqa: F401  注册表模型

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata
VERSION_TABLE = "alembic_version_erp"


def _database_url() -> str:
    url = config.get_main_option("sqlalchemy.url") or os.environ.get("ERP_DATABASE_URL")
    if not url:
        url = "postgresql+asyncpg://erp_app@127.0.0.1:5432/smart_erp"
    return url


def run_migrations_offline() -> None:
    buffer = io.StringIO()
    buffer.write("-- smart-ERP Alembic baseline (PostgreSQL 15)\n")
    buffer.write(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA};\n\n")
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        include_schemas=False,
        compare_type=True,
        version_table=VERSION_TABLE,
        version_table_schema=SCHEMA,
        output_buffer=buffer,
    )
    with context.begin_transaction():
        context.run_migrations()
    sys.stdout.write(buffer.getvalue())


def _configure(connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        include_schemas=False,
        compare_type=True,
        version_table=VERSION_TABLE,
        version_table_schema=SCHEMA,
    )


def do_run_migrations(connection) -> None:
    _configure(connection)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    # 延迟导入：--sql 离线渲染不需要 greenlet / 异步驱动
    from sqlalchemy.ext.asyncio import create_async_engine

    # lock_timeout 走连接参数（C14 规定 3s），避免在事务外执行 SET 影响 Alembic 的提交
    engine = create_async_engine(
        _database_url(),
        poolclass=pool.NullPool,
        connect_args={"server_settings": {"lock_timeout": "3000"}},
    )
    try:
        async with engine.connect() as connection:
            await connection.execute(text(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}"))
            await connection.commit()
            await connection.run_sync(do_run_migrations)
    finally:
        await engine.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
