"""启动期配置：ERP_ 前缀、发布 tag 约束。"""

from __future__ import annotations

import pytest

from apps.platform.config import ConfigError, Settings


def test_env_prefix_is_erp(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ERP_IMAGE_TAG", "a" * 40)
    monkeypatch.setenv("ERP_API_PORT", "9099")
    monkeypatch.setenv("ERP_DATABASE_URL", "postgresql+asyncpg://erp_app@db:5432/smart_erp")

    settings = Settings.from_env()

    assert settings.image_tag == "a" * 40
    assert settings.api_port == 9099
    assert settings.database_url.endswith("/smart_erp")


def test_unprefixed_vars_are_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("IMAGE_TAG", "nope")
    settings = Settings.from_env()
    assert settings.image_tag == "dev"


def test_latest_image_tag_rejected() -> None:
    with pytest.raises(ConfigError):
        Settings(image_tag="latest")


def test_database_url_must_be_asyncpg() -> None:
    with pytest.raises(ConfigError):
        Settings(database_url="postgresql://erp_app@db:5432/smart_erp")


def test_pool_budget_must_be_positive() -> None:
    with pytest.raises(ConfigError):
        Settings(db_pool_size=0)
