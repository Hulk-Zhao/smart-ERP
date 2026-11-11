"""供应商对账单 CSV → 对账行级数据集：正常、脏数据、端到端。

说明：这里的数据全部是测试构造，**不是种子数据**；``our_lines`` 恒为空，
要跑真实对账仍需 ERP/MES/WMS 提供我方行。
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from apps.api.schemas import ReconLineIn
from apps.docparse.statement_csv import (
    StatementCsvError,
    map_supplier_statement_csv,
    normalize_unit,
)
from apps.recon import build_request, reconcile

HEADER = (
    "doc_no,supplier_code,line_no,item_code,item_name,qty,unit,unit_price,amount,"
    "currency,tax_included,period\n"
)
ROW_1 = "DZ-2026-001,S001,1,M-001,钢卷,10,公斤,12.5,125.00,CNY,是,2026-03\n"
ROW_2 = "DZ-2026-001,S001,2,M-002,铝板,5,张,20,100.00,CNY,true,2026-03\n"


def _map(text: str, **kwargs):
    return map_supplier_statement_csv(
        text.encode("utf-8"), filename="statement.csv", **kwargs
    )


def test_normal_statement_maps_supplier_lines():
    statement = _map(HEADER + ROW_1 + ROW_2)

    assert statement.source_file == "statement.csv"
    assert statement.our_lines == ()  # 文件通道拿不到我方行，不伪造
    assert len(statement.supplier_lines) == 2

    first = statement.supplier_lines[0]
    assert first["system"] == "file"
    assert first["doc_no"] == "DZ-2026-001"
    assert first["line_no"] == 1
    assert first["supplier_code"] == "S001"
    assert first["item_code"] == "M-001"
    assert first["qty"] == Decimal(10)
    assert first["unit"] == "KG"  # 公斤 → KG
    assert first["unit_price"] == Decimal("12.5")
    assert first["amount"] == Decimal("125.00")
    assert first["tax_included"] is True
    assert first["coord"] == {"file": "statement.csv", "line": 2}

    # 与 API 输入契约逐字段对齐（多余/缺失字段都会被 pydantic 拒绝）
    for row in statement.supplier_lines:
        ReconLineIn.model_validate(row)

    dataset = statement.as_dataset()
    assert dataset["our_lines"] == []
    assert len(dataset["supplier_lines"]) == 2


def test_missing_required_column_rejects_whole_file():
    header = "doc_no,supplier_code,line_no,item_code,item_name,qty,unit\n"
    rows = "DZ-1,S001,1,M-001,钢卷,10,公斤\n"

    with pytest.raises(StatementCsvError) as info:
        _map(header + rows)

    assert info.value.missing_columns == ("amount 或 unit_price",)
    assert info.value.issues == ()


def test_invalid_amount_reports_line_and_value():
    rows = ROW_1 + "DZ-2026-001,S001,3,M-003,铜棒,2,米,20.00,abc,CNY,是,2026-03\n"

    with pytest.raises(StatementCsvError) as info:
        _map(HEADER + rows)

    issue = info.value.issues[0]
    assert (issue.line_no, issue.field, issue.value, issue.code) == (
        3,
        "amount",
        "abc",
        "E_BAD_NUMBER",
    )
    assert "不补 0" in issue.message


def test_empty_qty_is_rejected_not_treated_as_zero():
    rows = "DZ-2026-001,S001,1,M-001,钢卷,,公斤,12.5,125.00,CNY,是,2026-03\n"

    with pytest.raises(StatementCsvError) as info:
        _map(HEADER + rows)

    issue = info.value.issues[0]
    assert (issue.line_no, issue.field, issue.code) == (2, "qty", "E_QTY_EMPTY")
    assert "缺失≠零" in issue.message


def test_unknown_unit_is_rejected_with_allowed_values():
    # 「件」不并入「个」：主数据不把箱/件当等价单位，这里也不猜
    rows = "DZ-2026-001,S001,1,M-001,钢卷,10,件,12.5,125.00,CNY,是,2026-03\n"

    with pytest.raises(StatementCsvError) as info:
        _map(HEADER + rows)

    issue = info.value.issues[0]
    assert (issue.line_no, issue.field, issue.code) == (2, "unit", "E_UNKNOWN_UNIT")
    assert "KG" in issue.message


def test_duplicate_row_is_rejected_with_both_line_numbers():
    rows = ROW_1 + ROW_1.replace(",1,", ",1,")

    with pytest.raises(StatementCsvError) as info:
        _map(HEADER + rows)

    issue = info.value.issues[0]
    assert issue.code == "E_DUPLICATE_LINE"
    assert issue.line_no == 3
    assert "第 2 行" in issue.message


def test_empty_file_is_rejected():
    with pytest.raises(StatementCsvError) as info:
        _map("")

    assert "文件为空" in str(info.value)


def test_header_only_file_is_rejected():
    with pytest.raises(StatementCsvError) as info:
        _map(HEADER)

    assert "没有数据行" in str(info.value)


def test_crlf_file_maps_same_as_lf():
    statement = _map((HEADER + ROW_1 + ROW_2).replace("\n", "\r\n"))

    assert len(statement.supplier_lines) == 2
    assert statement.supplier_lines[0]["item_name"] == "钢卷"
    assert "\r" not in statement.supplier_lines[0]["currency"]


def test_unit_normalization_keeps_ambiguous_units_apart():
    assert normalize_unit("公斤") == "KG"
    assert normalize_unit("ｋｇ") == "KG"  # 全角 + 大小写
    assert normalize_unit("㎡") == "M2"
    assert normalize_unit("件") is None  # 不与「个」混为一谈
    assert normalize_unit("打") is None


def test_end_to_end_dataset_feeds_reconcile():
    """CSV → dataset.supplier_lines → build_request → reconcile（库内调用）。

    这批数据是测试构造，不是种子；our_lines 为空，所以匹配数为 0 属预期。
    """

    statement = _map(HEADER + ROW_1 + ROW_2)
    dataset = statement.as_dataset()

    request = build_request(
        {
            "scope": {"org_id": "org-test", "period": "2026-03", "system_ids": ["file"]},
            "suppliers": ["S001"],
            **dataset,
            "master_data": {
                "suppliers": [{"code": "S001", "name": "蓝票科技有限公司"}],
                "items": [
                    {"code": "M-001", "name": "钢卷", "base_unit": "KG"},
                    {"code": "M-002", "name": "铝板", "base_unit": "SHEET"},
                ],
            },
        }
    )

    result = reconcile(request)

    assert result.metrics.supplier_lines == 2
    assert result.metrics.matched == 0  # 没有 our_lines，不产生虚假匹配
    assert len(request.supplier_lines) == 2
