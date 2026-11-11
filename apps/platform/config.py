"""启动期配置（C11 / C14）。

所有环境变量统一 ``ERP_`` 前缀，只读一次、进程内不可变。
本模块只登记启动期必需的少量键；C11 的全量清单与 JSON Schema 归 S8，
新增键必须先改 ``deploy/smart-erp/.env.example`` 再改这里。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache
from uuid import UUID

ENV_PREFIX = "ERP_"

_DEFAULT_DATABASE_URL = "postgresql+asyncpg://erp_app@127.0.0.1:5432/smart_erp"

_TRUTHY = frozenset({"1", "true", "yes", "on"})


def _bool_env(name: str, default: bool = False) -> bool:
    raw = _env(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in _TRUTHY


def _list_env(name: str) -> tuple[str, ...]:
    raw = _env(name) or ""
    return tuple(item.strip() for item in raw.split(",") if item.strip())


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
    #: demo 档静态会话（部署期注入，默认关闭）；只读当前会话，不签发新令牌。
    demo_session_enabled: bool = False
    demo_session_token: str = ""
    demo_session_user_id: str = ""
    demo_session_tenant_id: str = ""
    demo_session_roles: tuple[str, ...] = ()
    demo_session_org_ids: tuple[str, ...] = ()
    demo_session_periods: tuple[str, ...] = ()
    demo_session_system_ids: tuple[str, ...] = ()
    #: 助手建议（OpenAI 兼容）：密钥不参与 repr，日志只打主机名与模型名。
    llm_base_url: str = ""
    llm_api_key: str = field(default="", repr=False)
    llm_model: str = ""
    assistant_rate_limit_per_minute: int = 10

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
        if self.assistant_rate_limit_per_minute < 1:
            raise ConfigError("ERP_ASSISTANT_RATE_LIMIT_PER_MINUTE 必须 >= 1")
        self._validate_demo_session()

    def _validate_demo_session(self) -> None:
        """demo 静态会话只能活在 demo 档；缺字段一律启动失败，不留半套身份。"""

        if not self.demo_session_enabled:
            return
        if self.app_env != "demo":
            raise ConfigError(
                "ERP_DEMO_SESSION_ENABLED 只允许在 ERP_APP_ENV=demo 时开启，"
                f"当前 ERP_APP_ENV={self.app_env!r}（full/生产档位不得继承该开关）"
            )
        missing = [
            name
            for name, value in (
                ("ERP_DEMO_SESSION_TOKEN", self.demo_session_token),
                ("ERP_DEMO_SESSION_USER_ID", self.demo_session_user_id),
                ("ERP_DEMO_SESSION_TENANT_ID", self.demo_session_tenant_id),
                ("ERP_DEMO_SESSION_ROLES", ",".join(self.demo_session_roles)),
                ("ERP_DEMO_SESSION_ORGS", ",".join(self.demo_session_org_ids)),
                ("ERP_DEMO_SESSION_PERIODS", ",".join(self.demo_session_periods)),
                ("ERP_DEMO_SESSION_SYSTEMS", ",".join(self.demo_session_system_ids)),
            )
            if not value.strip()
        ]
        if missing:
            raise ConfigError(
                "ERP_DEMO_SESSION_ENABLED=true 时以下键不能为空：" + "、".join(missing)
            )
        if len(self.demo_session_token) < 16:
            raise ConfigError("ERP_DEMO_SESSION_TOKEN 至少 16 个字符（避免误用短串当令牌）")
        for name, raw in (
            ("ERP_DEMO_SESSION_USER_ID", self.demo_session_user_id),
            ("ERP_DEMO_SESSION_TENANT_ID", self.demo_session_tenant_id),
        ):
            try:
                UUID(raw)
            except ValueError as exc:
                raise ConfigError(f"{name} 必须是 UUID，实际为 {raw!r}") from exc

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
            demo_session_enabled=_bool_env("DEMO_SESSION_ENABLED"),
            demo_session_token=(_env("DEMO_SESSION_TOKEN") or "").strip(),
            demo_session_user_id=(_env("DEMO_SESSION_USER_ID") or "").strip(),
            demo_session_tenant_id=(_env("DEMO_SESSION_TENANT_ID") or "").strip(),
            demo_session_roles=_list_env("DEMO_SESSION_ROLES"),
            demo_session_org_ids=_list_env("DEMO_SESSION_ORGS"),
            demo_session_periods=_list_env("DEMO_SESSION_PERIODS"),
            demo_session_system_ids=_list_env("DEMO_SESSION_SYSTEMS"),
            llm_base_url=(_env("LLM_BASE_URL") or "").strip(),
            llm_api_key=(_env("LLM_API_KEY") or "").strip(),
            llm_model=(_env("LLM_MODEL") or "").strip(),
            assistant_rate_limit_per_minute=_int_env(
                "ASSISTANT_RATE_LIMIT_PER_MINUTE", 10
            ),
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings.from_env()
