"""只读快照加载：把 JSON 夹具转成领域模型，不连接任何业务系统。"""

from __future__ import annotations

import json
from collections.abc import Mapping
from decimal import Decimal
from pathlib import Path
from typing import Any

from .models import (
    CurrencyRate,
    Customer,
    Document,
    Item,
    Ledger,
    MasterDataSnapshot,
    PaymentTermContract,
    Period,
    Supplier,
    TaxCode,
    UnitConversion,
)


def _as_decimal(value: Any) -> Decimal:
    return Decimal(str(value))


def _as_optional_decimal(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    return _as_decimal(value)


def snapshot_from_dict(
    raw: Mapping[str, Any], *, source: str = ""
) -> MasterDataSnapshot:
    scope = raw.get("scope") or {}
    org_id = raw.get("org_id") or scope.get("org_id")
    as_of_period = raw.get("as_of_period") or scope.get("period")
    if not as_of_period:
        raise ValueError("快照缺少 as_of_period（或 scope.period），无法确定体检基准期间")

    suppliers = tuple(
        Supplier(
            code=entry["code"],
            name=entry.get("name") or "",
            tax_no=entry.get("tax_no"),
            bank_account=entry.get("bank_account"),
            org_id=entry.get("org_id") or org_id,
            ledger_id=entry.get("ledger_id"),
            currency=entry.get("currency"),
            payment_term=entry.get("payment_term"),
            tax_code=entry.get("tax_code"),
        )
        for entry in raw.get("suppliers", [])
    )
    customers = tuple(
        Customer(
            code=entry["code"],
            name=entry.get("name") or "",
            tax_no=entry.get("tax_no"),
            org_id=entry.get("org_id") or org_id,
            ledger_id=entry.get("ledger_id"),
            currency=entry.get("currency"),
            payment_term=entry.get("payment_term"),
            tax_code=entry.get("tax_code"),
        )
        for entry in raw.get("customers", [])
    )
    items = tuple(
        Item(
            code=entry["code"],
            name=entry.get("name") or "",
            base_unit=entry.get("base_unit"),
            spec=entry.get("spec"),
            category=entry.get("category"),
            org_id=entry.get("org_id") or org_id,
            ledger_id=entry.get("ledger_id"),
            tax_code=entry.get("tax_code"),
        )
        for entry in raw.get("items", [])
    )
    conversions = tuple(
        UnitConversion(
            item_code=entry["item_code"],
            from_unit=entry.get("from_unit") or "",
            to_unit=entry.get("to_unit") or "",
            factor=_as_decimal(entry["factor"]),
            last_used_period=entry.get("last_used_period"),
            usage_count=int(entry.get("usage_count", 0)),
            org_id=entry.get("org_id") or org_id,
        )
        for entry in raw.get("unit_conversions", [])
    )
    documents = tuple(
        Document(
            doc_no=entry["doc_no"],
            doc_type=entry.get("doc_type") or "",
            doc_period=entry.get("doc_period") or "",
            posting_period=entry.get("posting_period")
            or entry.get("doc_period")
            or "",
            org_id=entry.get("org_id") or org_id or "",
            ledger_id=entry.get("ledger_id") or "",
            supplier_code=entry.get("supplier_code"),
            item_code=entry.get("item_code"),
            unit=entry.get("unit"),
            line_no=int(entry.get("line_no", 1)),
            currency=entry.get("currency"),
            exchange_rate=_as_optional_decimal(entry.get("exchange_rate")),
        )
        for entry in raw.get("documents", [])
    )
    periods = tuple(
        Period(
            period=entry["period"],
            closed=bool(entry.get("closed", False)),
            org_id=entry.get("org_id") or org_id,
        )
        for entry in raw.get("periods", [])
    )
    ledgers = tuple(
        Ledger(
            ledger_id=entry["ledger_id"],
            org_id=entry["org_id"],
            name=entry.get("name") or "",
        )
        for entry in raw.get("ledgers", [])
    )
    currency_rates = tuple(
        CurrencyRate(
            currency=entry["currency"],
            period=entry["period"],
            rate=_as_decimal(entry["rate"]),
            ledger_id=entry.get("ledger_id"),
            updated_period=entry.get("updated_period"),
        )
        for entry in raw.get("currency_rates", [])
    )
    tax_codes = tuple(
        TaxCode(
            code=entry["code"],
            rate=_as_decimal(entry["rate"]),
            filing_rate=_as_decimal(entry.get("filing_rate", entry["rate"])),
            name=entry.get("name") or "",
        )
        for entry in raw.get("tax_codes", [])
    )
    payment_contracts = tuple(
        PaymentTermContract(
            party_type=entry.get("party_type") or "",
            party_code=entry["party_code"],
            payment_term=entry.get("payment_term") or "",
            contract_no=entry.get("contract_no") or "",
        )
        for entry in raw.get("payment_contracts", [])
    )
    return MasterDataSnapshot(
        org_id=org_id,
        as_of_period=as_of_period,
        source=source,
        base_currency=raw.get("base_currency") or "CNY",
        suppliers=suppliers,
        customers=customers,
        items=items,
        unit_conversions=conversions,
        documents=documents,
        periods=periods,
        ledgers=ledgers,
        currency_rates=currency_rates,
        tax_codes=tax_codes,
        payment_contracts=payment_contracts,
    )


def load_snapshot(path: str | Path) -> MasterDataSnapshot:
    """从 JSON 文件读取只读快照；``source`` 写相对路径，便于差异清单回溯。"""
    target = Path(path)
    raw = json.loads(target.read_text(encoding="utf-8"))
    return snapshot_from_dict(raw, source=str(target))
