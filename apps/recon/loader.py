"""夹具加载：把 JSON 场景转成 ReconRequest（离线可跑，不连数据库/连接器）。"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .models import (
    DocLine,
    ItemMaster,
    MasterData,
    Money,
    ParseIssue,
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


def _build_line(
    raw: Mapping[str, Any], issues: list[ParseIssue], side: str
) -> DocLine:
    currency = raw.get("currency", "CNY")
    tax_included = raw.get("tax_included", True)
    line_no = int(raw.get("line_no", 1))
    ref = f"{raw['system']}:{raw['doc_no']}#{line_no}"

    def _issue(field_name: str, value: object) -> None:
        issues.append(
            ParseIssue(
                ref=ref,
                side=side,
                field=field_name,
                raw=str(value),
                supplier_code=raw.get("supplier_code", ""),
                doc_no=raw["doc_no"],
                line_no=line_no,
            )
        )

    def _number(name: str):
        """解析失败不抛：记行号+原值，字段置 None（缺失≠零），由引擎按行降级。"""
        value = raw.get(name)
        try:
            return to_decimal(value)
        except ValueError:
            _issue(name, value)
            return None

    try:
        amount = Money.of(raw.get("amount"), currency, tax_included)
    except ValueError:
        _issue("amount", raw.get("amount"))
        amount = None
    return DocLine(
        system=raw["system"],
        doc_no=raw["doc_no"],
        line_no=line_no,
        supplier_code=raw["supplier_code"],
        item_code=raw.get("item_code"),
        item_name=raw.get("item_name"),
        qty=_number("qty"),
        unit=raw.get("unit"),
        unit_price=_number("unit_price"),
        amount=amount,
        tax_rate=_number("tax_rate"),
        scrap_qty=_number("scrap_qty"),
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
    defaults = Tolerance()

    def _tolerance(key: str):
        """未显式配置用默认值；显式配 0 表示关闭该容差（0 是有效配置）。"""
        value = tolerance_raw.get(key)
        parsed = to_decimal(value) if value is not None else None
        return getattr(defaults, key) if parsed is None else parsed

    tolerance = Tolerance(
        amount=_tolerance("amount"),
        price=_tolerance("price"),
        qty=_tolerance("qty"),
        rounding=_tolerance("rounding"),
        rounding_batch_limit=_tolerance("rounding_batch_limit"),
    )
    issues: list[ParseIssue] = []
    return ReconRequest(
        scope=scope,
        suppliers=tuple(raw.get("suppliers", ())),
        our_lines=tuple(
            _build_line(line, issues, "our") for line in raw.get("our_lines", ())
        ),
        supplier_lines=tuple(
            _build_line(line, issues, "supplier")
            for line in raw.get("supplier_lines", ())
        ),
        master_data=_build_master_data(raw.get("master_data", {})),
        tolerance=tolerance,
        parse_issues=tuple(issues),
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
