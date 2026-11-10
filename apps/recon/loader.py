"""夹具加载：把 JSON 场景转成 ReconRequest（离线可跑，不连数据库/连接器）。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .models import (
    DocLine,
    ItemMaster,
    MasterData,
    Money,
    ReconRequest,
    Scope,
    SupplierMaster,
    Tolerance,
    UnitConversion,
    to_decimal,
)


@dataclass(frozen=True)
class Scenario:
    name: str
    description: str
    request: ReconRequest
    expected: Mapping[str, Any]


def _build_master_data(raw: Mapping[str, Any]) -> MasterData:
    suppliers = {
        entry["code"]: SupplierMaster(
            code=entry["code"],
            name=entry.get("name", entry["code"]),
            aliases=tuple(entry.get("aliases", ())),
            tax_no=entry.get("tax_no"),
        )
        for entry in raw.get("suppliers", [])
    }
    items = {
        entry["code"]: ItemMaster(
            code=entry["code"],
            name=entry.get("name", entry["code"]),
            base_unit=entry.get("base_unit"),
            spec=entry.get("spec"),
        )
        for entry in raw.get("items", [])
    }
    conversions = tuple(
        UnitConversion(
            item=entry["item"],
            from_unit=entry["from_unit"],
            to_unit=entry["to_unit"],
            factor=to_decimal(entry["factor"]),  # type: ignore[arg-type]
        )
        for entry in raw.get("unit_conversions", [])
    )
    return MasterData(suppliers=suppliers, items=items, unit_conversions=conversions)


def _build_line(raw: Mapping[str, Any]) -> DocLine:
    currency = raw.get("currency", "CNY")
    tax_included = raw.get("tax_included", True)
    amount = Money.of(raw.get("amount"), currency, tax_included)
    return DocLine(
        system=raw["system"],
        doc_no=raw["doc_no"],
        line_no=int(raw.get("line_no", 1)),
        supplier_code=raw["supplier_code"],
        item_code=raw.get("item_code"),
        item_name=raw.get("item_name"),
        qty=to_decimal(raw.get("qty")),
        unit=raw.get("unit"),
        unit_price=to_decimal(raw.get("unit_price")),
        amount=amount,
        tax_rate=to_decimal(raw.get("tax_rate")),
        scrap_qty=to_decimal(raw.get("scrap_qty")),
        period=raw.get("period"),
        coord=raw.get("coord"),
    )


def build_request(raw: Mapping[str, Any]) -> ReconRequest:
    scope_raw = raw.get("scope", {})
    scope = Scope(
        org_id=scope_raw.get("org_id"),
        period=scope_raw.get("period"),
        system_ids=tuple(scope_raw.get("system_ids", ())),
    )
    tolerance_raw = raw.get("options", {}).get("tolerance", {})
    tolerance = Tolerance(
        amount=to_decimal(tolerance_raw.get("amount"), places=None)
        or Tolerance().amount,
        price=to_decimal(tolerance_raw.get("price")) or Tolerance().price,
        qty=to_decimal(tolerance_raw.get("qty")) or Tolerance().qty,
    )
    return ReconRequest(
        scope=scope,
        suppliers=tuple(raw.get("suppliers", ())),
        our_lines=tuple(_build_line(line) for line in raw.get("our_lines", ())),
        supplier_lines=tuple(
            _build_line(line) for line in raw.get("supplier_lines", ())
        ),
        master_data=_build_master_data(raw.get("master_data", {})),
        tolerance=tolerance,
    )


def load_scenario(path: str | Path) -> Scenario:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    return Scenario(
        name=payload.get("name", Path(path).stem),
        description=payload.get("description", ""),
        request=build_request(payload["input"]),
        expected=payload.get("expected", {}),
    )


def load_scenarios(directory: str | Path) -> list[Scenario]:
    return [load_scenario(path) for path in sorted(Path(directory).glob("*.json"))]
