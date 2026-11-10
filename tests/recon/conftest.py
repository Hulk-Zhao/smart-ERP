"""pytest 公共夹具：把仓库根加入 sys.path，并提供最小 ReconRequest 构造器。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps.recon.loader import build_request  # noqa: E402

SCENARIO_DIR = ROOT / "fixtures" / "recon"


@pytest.fixture
def scenario_dir() -> Path:
    return SCENARIO_DIR


@pytest.fixture
def make_request():
    """构造最小对账请求；默认含已绑定的供应商/物料，便于按需覆盖字段。"""

    def _make(
        our_lines,
        supplier_lines,
        *,
        suppliers=("SUP-A",),
        items=None,
        unit_conversions=(),
        scope=None,
        tolerance=None,
        master_suppliers=None,
    ):
        master_codes = suppliers if master_suppliers is None else master_suppliers
        raw = {
            "scope": scope
            or {
                "org_id": "ORG-TEST",
                "period": "2026-10",
                "system_ids": ["ERP", "STATEMENT"],
            },
            "suppliers": list(suppliers),
            "options": {"tolerance": tolerance or {}},
            "master_data": {
                "suppliers": [
                    {"code": code, "name": f"{code} 示例"} for code in master_codes
                ],
                "items": items
                or [
                    {"code": "IT-A", "name": "测试物料A", "base_unit": "PCS"},
                    {"code": "IT-B", "name": "测试物料B", "base_unit": "PCS"},
                ],
                "unit_conversions": list(unit_conversions),
            },
            "our_lines": list(our_lines),
            "supplier_lines": list(supplier_lines),
        }
        return build_request(raw)

    return _make


def line(
    *,
    system="ERP",
    doc_no="PO-1",
    line_no=1,
    supplier_code="SUP-A",
    item_code="IT-A",
    qty="10",
    unit="PCS",
    unit_price="5",
    amount="50",
    tax_rate="0.13",
    scrap_qty=None,
    period="2026-10",
    item_name=None,
):
    payload = {
        "system": system,
        "doc_no": doc_no,
        "line_no": line_no,
        "supplier_code": supplier_code,
        "item_code": item_code,
        "qty": qty,
        "unit": unit,
        "unit_price": unit_price,
        "amount": amount,
        "tax_rate": tax_rate,
        "period": period,
    }
    if scrap_qty is not None:
        payload["scrap_qty"] = scrap_qty
    if item_name is not None:
        payload["item_name"] = item_name
    return payload


@pytest.fixture
def make_line():
    """返回单据行字典构造器（避免跨测试模块 import conftest）。"""
    return line
