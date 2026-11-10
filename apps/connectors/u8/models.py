"""U8 域模型（用友 U8 的原始字段名，不做美化）。

保留 U8 原字段名是刻意的：映射层要能一眼对上接口文档（``cVenCode`` / ``iTaxPrice``…），
改名只会让「这到底取的哪一列」变成口头知识。所有字段可空——U8 侧真的会返回空，
模型不能替它补值。
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class _U8Model(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)


class U8Supplier(_U8Model):
    """供应商档案（U8 ``Vendor``）。"""

    cVenCode: str | None = None
    cVenName: str | None = None
    cVenAbbName: str | None = None
    cTaxCode: str | None = None
    cMemo: str | None = None


class U8Item(_U8Model):
    """存货档案（U8 ``Inventory``）。``cComUnitName`` 是基本单位，缺了不允许猜。"""

    cInvCode: str | None = None
    cInvName: str | None = None
    cInvStd: str | None = None
    cComUnitName: str | None = None
    cInvType: str | None = None


class U8POLine(_U8Model):
    iPOsID: int | None = None
    cInvCode: str | None = None
    cInvName: str | None = None
    cComUnitName: str | None = None
    iQuantity: float | str | None = None
    iTaxPrice: float | str | None = None  # 含税单价
    iUnitPrice: float | str | None = None  # 无税单价
    iTaxRate: float | str | None = None
    iMoney: float | str | None = None


class U8PurchaseOrder(_U8Model):
    """采购订单（U8 ``PO_Pomain`` + ``PO_Podetails``）。"""

    cPOID: str | None = None
    cCode: str | None = None
    dPODate: str | None = None
    cVenCode: str | None = None
    cVenName: str | None = None
    cMemo: str | None = None
    details: list[U8POLine] = Field(default_factory=list)


class U8ArrivalLine(_U8Model):
    iArrivalVouchsID: int | None = None
    cInvCode: str | None = None
    cInvName: str | None = None
    cComUnitName: str | None = None
    iQuantity: float | str | None = None
    iTaxPrice: float | str | None = None
    iUnitPrice: float | str | None = None
    iTaxRate: float | str | None = None
    iMoney: float | str | None = None


class U8ArrivalVouch(_U8Model):
    """采购入库单（U8 ``PU_ArrivalVouch``）。"""

    cCode: str | None = None
    dDate: str | None = None
    cVenCode: str | None = None
    cVenName: str | None = None
    cMemo: str | None = None
    details: list[U8ArrivalLine] = Field(default_factory=list)
