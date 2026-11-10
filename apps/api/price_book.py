"""合同价目取价端口（0008）：批次创建与账单读取共用同一取价口径。

为什么单独成模块：取价是「业务事实的来源」，不是路由细节。把它做成端口后：

- 批次创建时取一次价并随批次落快照（``recon_batch`` 四字段），历史账不随后续
  改价而变；
- 账单读取优先用批次快照里的价，没有快照的旧批次才走 ``ERP_METERING_*`` 配置；
- 用例可以注入假实现，不需要起 PostgreSQL。

fail closed：币种不符即「无有效价」，**绝不**改用价格表里另一种币种的单价，
也**不**把其他币种的价带进错误响应（那是别人的合同价）。
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Protocol
from uuid import UUID

import sqlalchemy as sa

from apps.api.metering import ENV_CURRENCY, PRICE_LOOKUP_SQL

#: 是否禁止回落到 ``ERP_METERING_UNIT_PRICE``（默认 false，保持既有部署行为）。
ENV_REQUIRE_CONTRACT_PRICE = "ERP_METERING_REQUIRE_CONTRACT_PRICE"

_TRUTHY = frozenset({"1", "true", "yes", "on"})
_ISO_CURRENCY = re.compile(r"^[A-Z]{3}$")


def require_contract_price(env: Mapping[str, str] | None = None) -> bool:
    """读 ``ERP_METERING_REQUIRE_CONTRACT_PRICE``；空串视为未配置（false）。"""

    source = os.environ if env is None else env
    return (source.get(ENV_REQUIRE_CONTRACT_PRICE) or "").strip().lower() in _TRUTHY


def metering_currency(env: Mapping[str, str] | None = None) -> str:
    """批次计价币种：``ERP_METERING_CURRENCY``（默认 CNY）。

    0008 起快照列是 ``char(3)`` 且带 ``^[A-Z]{3}$`` CHECK，所以这里就要求
    ISO-4217 形状的三位大写字母——配置成 ``USDT`` 这类会在建批次时明确报错，
    而不是等到落库被数据库截断/拒绝。
    """

    source = os.environ if env is None else env
    currency = (source.get(ENV_CURRENCY) or "CNY").strip().upper()
    if not _ISO_CURRENCY.match(currency):
        raise ValueError(
            f"{ENV_CURRENCY} 必须是三位大写 ISO 币种代码，实际为 {currency!r}"
        )
    return currency


@dataclass(frozen=True, slots=True)
class PriceQuote:
    """一行生效价（只含取价必需的事实，不含审批人等无关字段）。"""

    price_book_id: UUID
    unit_price: Decimal
    currency: str


@dataclass(frozen=True, slots=True)
class PriceLookup:
    """取价结果。

    ``has_other_currency_price`` 只回答「这个租户在该时点有没有别的币种的价」，
    用来区分 ``E-PRICE-MISSING``（压根没价）与 ``E-PRICE-CURRENCY``（有价但币种不符），
    **不返回**那些价的具体数值，避免跨合同信息泄露。
    """

    quote: PriceQuote | None
    has_other_currency_price: bool


class PriceBookLookup(Protocol):
    async def effective_price(
        self,
        *,
        tenant_id: UUID,
        currency: str,
        at: datetime,
    ) -> PriceLookup: ...


#: 只查「存在性」，不查单价：用于区分原因码，且不把其他币种的价带出库。
_OTHER_CURRENCY_EXISTS_SQL = """
SELECT EXISTS (
  SELECT 1
    FROM erp.metering_price_book
   WHERE tenant_id = CAST(:tenant_id AS uuid)
     AND currency <> :currency
     AND effective_from <= CAST(:at AS timestamptz)
)
"""


class SqlPriceBook:
    """PostgreSQL 取价适配器；首次使用时才建引擎（导入/装配阶段不连库）。"""

    def __init__(self, session_factory: Any | None = None) -> None:
        self._session_factory = session_factory

    def _factory(self) -> Any:
        if self._session_factory is None:
            from apps.platform.db.session import build_engine, build_session_factory

            self._session_factory = build_session_factory(build_engine())
        return self._session_factory

    async def effective_price(
        self,
        *,
        tenant_id: UUID,
        currency: str,
        at: datetime,
    ) -> PriceLookup:
        params = {"tenant_id": tenant_id, "currency": currency, "at": at}
        factory = self._factory()
        async with factory() as session:
            row = (
                (await session.execute(sa.text(PRICE_LOOKUP_SQL), params))
                .mappings()
                .first()
            )
            if row is not None:
                return PriceLookup(
                    quote=PriceQuote(
                        price_book_id=row["id"],
                        unit_price=Decimal(row["unit_price"]),
                        currency=str(row["currency"]).strip(),
                    ),
                    has_other_currency_price=False,
                )
            has_other = bool(
                (await session.execute(sa.text(_OTHER_CURRENCY_EXISTS_SQL), params)).scalar()
            )
        return PriceLookup(quote=None, has_other_currency_price=has_other)


__all__ = [
    "ENV_REQUIRE_CONTRACT_PRICE",
    "PriceBookLookup",
    "PriceLookup",
    "PriceQuote",
    "SqlPriceBook",
    "metering_currency",
    "require_contract_price",
]
