"""映射层的通用件：缺失清单、阻断判定、金额/数量的严格转换。

三条硬规则（全部来自 PRD §7.1/§7.3 与 DQ 规则）：

1. 缺就报缺：必需字段缺失 → ``blocked=True``，记录为 ``None``，绝不构造半条记录；
2. 缺失 ≠ 零：空值保持 ``None``，同时写进 ``missing_fields``；
3. 不猜：单位不做 1:1、币种不由单据推断（币种来自连接器配置的本位币）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Generic, TypeVar

from apps.connectors.canonical.models import (
    CanonicalRecord,
    MissingField,
    MissingReason,
)
from apps.platform.contracts.canonical import Money

T = TypeVar("T", bound=CanonicalRecord)

AMOUNT_TOLERANCE = Decimal("0.01")  # G-12：比较时按容差 0.01 判定相等


class MappingError(ValueError):
    """映射调用方式错误（不是数据缺失）。数据缺失走 ``MappingOutcome``。"""


@dataclass(frozen=True, slots=True)
class MappingContext:
    """映射上下文：连接器标识、权威源、币种（客户本位币，来自连接器配置）。"""

    connector: str
    authority_system: str
    currency: str = "CNY"
    tax_inclusive_price: bool = True

    def __post_init__(self) -> None:
        if len(self.currency) != 3 or not self.currency.isalpha():
            raise MappingError(f"币种必须是 3 位字母代码，实际 {self.currency!r}")


@dataclass
class MappingOutcome(Generic[T]):
    record: T | None
    issues: list[MissingField] = field(default_factory=list)
    blocked: bool = False

    @property
    def completeness(self) -> str:
        if self.record is None or self.blocked:
            return "unavailable"
        return "partial" if self.issues else "complete"

    def reason_codes(self) -> list[str]:
        return sorted({issue.reason for issue in self.issues})


def dec_str(value: object, *, places: int = 4) -> str | None:
    """任意数值 → 定点十进制字符串；空值/非法值返回 ``None``（不填 0）。"""

    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, str):
        value = value.strip()
        if value == "":
            return None
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    if not number.is_finite():
        return None
    return str(number.quantize(Decimal(1).scaleb(-places)))


def money(value: object, ctx: MappingContext, *, tax_included: bool | None = None) -> Money | None:
    """数值 → ``Money``；空值返回 ``None``，绝不产出 0 元。"""

    amount = dec_str(value, places=4)
    if amount is None:
        return None
    return Money(
        amount=amount,
        currency=ctx.currency,
        tax_included=ctx.tax_inclusive_price if tax_included is None else tax_included,
    )


def text(value: object) -> str | None:
    if value is None:
        return None
    raw = str(value).strip()
    return raw or None


def qty(value: object) -> str | None:
    return dec_str(value, places=6)


def date_only(value: object) -> str | None:
    """U8 常见 ``2026-10-31T00:00:00`` / ``2026-10-31 00:00:00`` / ``2026-10-31``。"""

    raw = text(value)
    if raw is None:
        return None
    head = raw.replace("/", "-").split("T")[0].split(" ")[0]
    parts = head.split("-")
    if len(parts) != 3 or not all(part.isdigit() for part in parts):
        return None
    year, month, day = parts
    return f"{int(year):04d}-{int(month):02d}-{int(day):02d}"


def missing(
    fld: str, reason: MissingReason, *, source: str, doc_no: str | None = None, detail: str | None = None
) -> MissingField:
    return MissingField(field=fld, reason=reason, source=source, doc_no=doc_no, detail=detail)


def amount_mismatch(
    quantity: str | None, unit_price: Money | None, amount: Money | None
) -> bool:
    """DQ-05：金额 = Σ(数量 × 单价) ± 0.01。缺任一输入时不判定（交给缺失清单）。"""

    if quantity is None or unit_price is None or amount is None:
        return False
    try:
        expected = Decimal(quantity) * unit_price.value
    except InvalidOperation:  # pragma: no cover - 上游已做正则约束
        return True
    return abs(expected - amount.value) > AMOUNT_TOLERANCE
