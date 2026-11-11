"""启动期配置（C11 / C14）。

所有环境变量统一 ``ERP_`` 前缀，只读一次、进程内不可变。
本模块只登记启动期必需的少量键；C11 的全量清单与 JSON Schema 归 S8，
新增键必须先改 ``deploy/smart-erp/.env.example`` 再改这里。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from functools import lru_cache
from uuid import UUID

ENV_PREFIX = "ERP_"

#: demo 静态身份条目的字段闭集；缺字段或多字段都启动失败。
_DEMO_IDENTITY_FIELDS = frozenset(
    {"token", "user_id", "tenant_id", "roles", "org_ids", "periods", "system_ids"}
)
_DEMO_IDENTITY_LIST_FIELDS = ("roles", "org_ids", "periods", "system_ids")
_DEMO_TOKEN_MIN_LENGTH = 16

_DEFAULT_DATABASE_URL = "postgresql+asyncpg://erp_app@127.0.0.1:5432/smart_erp"

_TRUTHY = frozenset({"1", "true", "yes", "on"})


def _bool_env(name: str, default: bool = False) -> bool:
    raw = _env(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in _TRUTHY


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
class DemoIdentity:
    """demo 档的一枚静态身份；一枚令牌对应一个人，不共享、不代登录。"""

    token: str
    user_id: UUID
    tenant_id: UUID
    roles: tuple[str, ...]
    org_ids: tuple[str, ...]
    periods: tuple[str, ...]
    system_ids: tuple[str, ...]


def parse_demo_identities(raw: str) -> tuple[DemoIdentity, ...]:
    """解析 ``ERP_DEMO_SESSION_IDENTITIES``；任何一处不合法都启动失败。

    fail-closed：不跳过坏条目、不做默认填充——半套身份跑起来比不跑更危险。
    """

    cleaned = (raw or "").strip()
    if not cleaned:
        raise ConfigError(
            "ERP_DEMO_SESSION_ENABLED=true 时 ERP_DEMO_SESSION_IDENTITIES 不能为空"
        )
    try:
        payload = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise ConfigError(
            f"ERP_DEMO_SESSION_IDENTITIES 必须是合法 JSON（{exc.msg}，位置 {exc.pos}）"
        ) from exc
    if not isinstance(payload, list):
        raise ConfigError("ERP_DEMO_SESSION_IDENTITIES 必须是一个 JSON 数组（每个元素一个身份）")
    if not payload:
        raise ConfigError("ERP_DEMO_SESSION_IDENTITIES 不能是空数组（至少一个身份）")

    identities: list[DemoIdentity] = []
    seen_tokens: set[str] = set()
    seen_users: set[UUID] = set()
    for index, entry in enumerate(payload):
        label = f"ERP_DEMO_SESSION_IDENTITIES[{index}]"
        if not isinstance(entry, dict):
            raise ConfigError(f"{label} 必须是对象，实际为 {type(entry).__name__}")
        missing = sorted(_DEMO_IDENTITY_FIELDS - set(entry))
        if missing:
            raise ConfigError(f"{label} 缺少字段：" + "、".join(missing))
        unknown = sorted(set(entry) - _DEMO_IDENTITY_FIELDS)
        if unknown:
            raise ConfigError(f"{label} 含未知字段：" + "、".join(unknown))

        token = entry["token"]
        if not isinstance(token, str) or not token.strip():
            raise ConfigError(f"{label}.token 必须是非空字符串")
        token = token.strip()
        if len(token) < _DEMO_TOKEN_MIN_LENGTH:
            raise ConfigError(
                f"{label}.token 至少 {_DEMO_TOKEN_MIN_LENGTH} 个字符（避免误用短串当令牌）"
            )
        if token in seen_tokens:
            raise ConfigError(f"{label}.token 与其他身份重复（令牌必须唯一）")

        parsed_uuids: dict[str, UUID] = {}
        for key in ("user_id", "tenant_id"):
            raw_uuid = entry[key]
            if not isinstance(raw_uuid, str):
                raise ConfigError(f"{label}.{key} 必须是 UUID 字符串")
            try:
                parsed_uuids[key] = UUID(raw_uuid)
            except ValueError as exc:
                raise ConfigError(f"{label}.{key} 必须是 UUID，实际为 {raw_uuid!r}") from exc
        if parsed_uuids["user_id"] in seen_users:
            raise ConfigError(f"{label}.user_id 与其他身份重复（一人一条）")

        parsed_lists: dict[str, tuple[str, ...]] = {}
        for key in _DEMO_IDENTITY_LIST_FIELDS:
            value = entry[key]
            if not isinstance(value, list) or not value:
                raise ConfigError(f"{label}.{key} 必须是非空字符串数组")
            items: list[str] = []
            for item in value:
                if not isinstance(item, str) or not item.strip():
                    raise ConfigError(f"{label}.{key} 只能包含非空字符串")
                items.append(item.strip())
            if len(set(items)) != len(items):
                raise ConfigError(f"{label}.{key} 存在重复值")
            parsed_lists[key] = tuple(items)

        seen_tokens.add(token)
        seen_users.add(parsed_uuids["user_id"])
        identities.append(
            DemoIdentity(
                token=token,
                user_id=parsed_uuids["user_id"],
                tenant_id=parsed_uuids["tenant_id"],
                roles=parsed_lists["roles"],
                org_ids=parsed_lists["org_ids"],
                periods=parsed_lists["periods"],
                system_ids=parsed_lists["system_ids"],
            )
        )
    return tuple(identities)


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
    #: 值为 JSON 数组，每条是一个 ``{token, user_id, tenant_id, roles, org_ids,
    #: periods, system_ids}`` 身份；多身份用于演示「发起人 ≠ 审批人」的双人复核。
    demo_session_enabled: bool = False
    demo_session_identities: str = ""
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
        """demo 静态会话只能活在 demo 档；身份不全一律启动失败，不留半套身份。"""

        if not self.demo_session_enabled:
            return
        if self.app_env != "demo":
            raise ConfigError(
                "ERP_DEMO_SESSION_ENABLED 只允许在 ERP_APP_ENV=demo 时开启，"
                f"当前 ERP_APP_ENV={self.app_env!r}（full/生产档位不得继承该开关）"
            )
        # 解析即校验：空映射、坏 JSON、坏 UUID、重复令牌一律在这里炸掉。
        parse_demo_identities(self.demo_session_identities)

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
            demo_session_identities=(_env("DEMO_SESSION_IDENTITIES") or "").strip(),
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
