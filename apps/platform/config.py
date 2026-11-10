"""启动期配置（C11 / C14）。

所有环境变量统一 ``ERP_`` 前缀，只读一次、进程内不可变。
本模块只登记启动期必需的少量键；C11 的全量清单与 JSON Schema 归 S8，
新增键必须先改 ``deploy/smart-erp/.env.example`` 再改这里。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache

ENV_PREFIX = "ERP_"

_DEFAULT_DATABASE_URL = "postgresql+asyncpg://erp_app@127.0.0.1:5432/smart_erp"


class ConfigError(ValueError):
    """配置非法，启动即失败（不带着错误配置跑起来）。"""


def _env(name: str) -> str | None:
    return os.environ.get(f"{ENV_PREFIX}{name}")


def _int_env(name: str, default: int) -> int:
    raw = _env(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{ENV_PREFIX}{name} 必须是整数，实际为 {raw!r}") from exc


@dataclass(frozen=True, slots=True)
class Settings:
    """进程级只读配置。字段顺序即 `.env.example` 的建议顺序。"""

    app_env: str = "local"
    api_port: int = 8010
    api_root_path: str = "/erp/api"
    log_level: str = "INFO"
    database_url: str = _DEFAULT_DATABASE_URL
    image_tag: str = "dev"
    db_pool_size: int = 4
    db_max_overflow: int = 4
    statement_timeout_ms: int = 30_000
    lock_timeout_ms: int = 3_000
    idle_in_transaction_timeout_ms: int = 60_000

    def __post_init__(self) -> None:
        if not self.database_url.startswith("postgresql+asyncpg://"):
            raise ConfigError(
                "ERP_DATABASE_URL 必须是 postgresql+asyncpg:// 连接串（C14：单库单 schema erp）"
            )
        # C12：禁止用 latest 承担发布语义。本地默认 dev，发布必须传完整 40 位 sha。
        if not self.image_tag or self.image_tag == "latest":
            raise ConfigError("ERP_IMAGE_TAG 不得为空或 latest；发布必须传完整 git sha")
        if self.db_pool_size < 1 or self.db_max_overflow < 0:
            raise ConfigError("连接池预算必须为正（api 4+4 / worker 4 / scheduler 1）")
        if self.statement_timeout_ms <= 0 or self.lock_timeout_ms <= 0:
            raise ConfigError("statement_timeout / lock_timeout 必须为正整数毫秒")

    @classmethod
    def from_env(cls) -> Settings:
        return cls(
            app_env=_env("APP_ENV") or "local",
            api_port=_int_env("API_PORT", 8010),
            api_root_path=_env("API_ROOT_PATH") or "/erp/api",
            log_level=(_env("LOG_LEVEL") or "INFO").upper(),
            database_url=_env("DATABASE_URL") or _DEFAULT_DATABASE_URL,
            image_tag=_env("IMAGE_TAG") or "dev",
            db_pool_size=_int_env("DB_POOL_SIZE", 4),
            db_max_overflow=_int_env("DB_MAX_OVERFLOW", 4),
            statement_timeout_ms=_int_env("DB_STATEMENT_TIMEOUT_MS", 30_000),
            lock_timeout_ms=_int_env("DB_LOCK_TIMEOUT_MS", 3_000),
            idle_in_transaction_timeout_ms=_int_env("DB_IDLE_TX_TIMEOUT_MS", 60_000),
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings.from_env()
