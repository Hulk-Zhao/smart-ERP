"""C1 统一数据模型的最小共用件（完整 5 主对象由 S1 维护）。

本模块只放所有流都会复用的结构：``Money`` / ``Evidence`` / ``Canonical`` 溯源基类。
约定（PRD §5.0、§3.5.3）：

- 金额一律 ``Money{amount, currency, tax_included}``，禁止裸数字；
- 缺失 ≠ 零：可选字段用 ``Optional`` + ``missing_reason``，序列化后前端显示 ``—``；
- 每条记录必须能溯源到 ``source_refs`` + ``authority_system`` + ``evidence``。
"""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints


class Coord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    page: int = Field(ge=1)
    bbox: tuple[float, float, float, float]


class Evidence(BaseModel):
    """证据最小单元：系统 + 单号 + 字段 +（可选）影像坐标。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    system: str = Field(min_length=1)
    doc_no: str = Field(min_length=1)
    field: str = Field(min_length=1)
    coord: Coord | None = None


Amount = Annotated[
    str,
    StringConstraints(pattern=r"^-?\d{1,14}(\.\d{1,4})?$"),
]


class Money(BaseModel):
    """金额对象（numeric(18,4)）。``amount`` 是十进制字符串，裸数字直接拒绝。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    amount: Amount
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    tax_included: bool

    @property
    def value(self) -> Decimal:
        return Decimal(self.amount)

    @property
    def quantized(self) -> Decimal:
        return self.value.quantize(Decimal("0.0001"))


class Canonical(BaseModel):
    """统一模型溯源基类：只增不改，``model_version`` 版本化。"""

    model_config = ConfigDict(extra="allow", frozen=True)

    model_version: Literal["1.0"] = "1.0"
    source_refs: dict[str, str] = Field(min_length=1)
    authority_system: str = Field(min_length=1)
    evidence: list[Evidence] = Field(default_factory=list)


class Missing:
    """「缺失 ≠ 零」的显式标记（用于非 Pydantic 的领域层装配）。"""

    __slots__ = ("missing_reason",)

    def __init__(self, missing_reason: str) -> None:
        if not missing_reason.strip():
            raise ValueError("缺失必须给出 missing_reason")
        self.missing_reason = missing_reason
