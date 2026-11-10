"""异步引擎与会话工厂（C14：单库单 schema erp）。"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from apps.platform.config import Settings, get_settings
from apps.platform.db.base import SCHEMA


def build_engine(settings: Settings | None = None) -> AsyncEngine:
    settings = settings or get_settings()
    return create_async_engine(
        settings.database_url,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_pre_ping=True,
        connect_args={
            "server_settings": {
                "search_path": SCHEMA,
                "statement_timeout": str(settings.statement_timeout_ms),
                "lock_timeout": str(settings.lock_timeout_ms),
                "idle_in_transaction_session_timeout": str(
                    settings.idle_in_transaction_timeout_ms
                ),
            }
        },
    )


def build_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
