"""C1 统一数据模型的 5 主对象（S1 维护）。

复用 S0 冻结的 ``Money`` / ``Evidence`` / ``Canonical`` 基类，只做「往上加」：

- ``Party`` / ``Item`` / ``Document`` / ``Transaction`` 四个实体 + S0 的 ``Money``；
- 每条记录都带 ``source_refs`` / ``authority_system`` / ``evidence`` / ``model_version``；
- 缺失字段一律 ``None`` + ``missing_fields``（写明原因），**不填 0、不猜默认值**
  （PRD §7.3、FR-1504「空值显示 — 不显示 0」）。
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from apps.platform.contracts.canonical import Canonical, Money

Qty = Annotated[str, StringConstraints(pattern=r"^-?\d{1,14}(\.\d{1,6})?$")]
Rate = Annotated[str, StringConstraints(pattern=r"^-?\d{1,6}(\.\d{1,6})?$")]

MissingReason = Literal[
    "missing",  # 源系统该字段为空
    "unit_conversion_missing",  # 单位换算缺失（DQ-02）
    "base_unit_missing",  # 物料基本单位为空（DQ-02）
    "amount_mismatch",  # 金额 ≠ Σ(数量×单价) ±0.01（DQ-05）
    "unmapped",  # 源字段没有对应口径（02 §2.2：无法归一的不进模型）
]


class MissingField(BaseModel):
    """缺失清单的一行：给上游 Agent 与前端「缺失清单」直接消费。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    field: str
    reason: MissingReason
    source: str
    doc_no: str | None = None
    detail: str | None = None


class CanonicalRecord(Canonical):
    model_config = ConfigDict(extra="allow", frozen=True)

    missing_fields: list[MissingField] = Field(default_factory=list)

    @property
    def complete(self) -> bool:
        return not self.missing_fields


class Party(CanonicalRecord):
    """供应商/客户。``code`` 必填：缺编码时映射层直接阻断（PRD §7.1 供应商行）。"""

    kind: Literal["supplier", "customer"] = "supplier"
    code: str = Field(min_length=1)
    name: str | None = None
    aliases: list[str] = Field(default_factory=list)
    tax_no: str | None = None
    payment_terms: str | None = None


class UnitConversion(CanonicalRecord):
    """单位换算（委外必需，DQ-02 要求闭合）。缺了只能标缺失，不能按 1:1 猜。"""

    item_code: str = Field(min_length=1)
    from_unit: str = Field(min_length=1)
    to_unit: str = Field(min_length=1)
    factor: Qty


class Item(CanonicalRecord):
    code: str = Field(min_length=1)
    name: str | None = None
    spec: str | None = None
    base_unit: str | None = None
    item_type: str | None = None
    conversions: list[UnitConversion] = Field(default_factory=list)


class DocumentLine(CanonicalRecord):
    line_no: int = Field(ge=1)
    item_code: str = Field(min_length=1)
    item_name: str | None = None
    quantity: Qty | None = None
    unit: str | None = None
    unit_price: Money | None = None
    amount: Money | None = None
    tax_rate: Rate | None = None


class Document(CanonicalRecord):
    """单据：采购订单 / 入库单 / 发票 / 付款草稿共用（``kind`` 区分）。"""

    kind: Literal[
        "purchase_order",
        "inbound_order",
        "arrival_vouch",
        "invoice",
        "payment_draft",
        # 文件通道（T105）：对账单/送货单是 PRD 02 §3 列明的单据类型，
        # 此前 Literal 未收录，属于增量补齐（只增不改）。
        "supplier_statement",
        "delivery_note",
    ]
    doc_no: str = Field(min_length=1)
    doc_date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    party_code: str = Field(min_length=1)
    party_name: str | None = None
    lines: list[DocumentLine] = Field(default_factory=list)
    total_amount: Money | None = None
    status: str | None = None


class Transaction(CanonicalRecord):
    """收发/报工流水（对账取数用，S2 消费）。"""

    kind: Literal["receipt", "issue", "report", "scrap"]
    txn_id: str = Field(min_length=1)
    occurred_at: str
    item_code: str = Field(min_length=1)
    party_code: str | None = None
    quantity: Qty
    unit: str
    ref_doc_no: str | None = None


__all__ = [
    "CanonicalRecord",
    "Document",
    "DocumentLine",
    "Item",
    "MissingField",
    "Money",
    "Party",
    "Qty",
    "Rate",
    "Transaction",
    "UnitConversion",
]
