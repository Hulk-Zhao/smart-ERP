"""文件通道：编码、只读能力、缺失降级、过滤与拒绝口径。"""

from __future__ import annotations

from pathlib import Path

from apps.connectors.file import ENTITY_KINDS
from apps.platform.connectors.sdk import ReadQuery
from tests.connectors.file_support import FILE_FIXTURES, make_file_stack, run


def _read(file: str, **filters: object):
    stack = make_file_stack()
    return stack, run(
        stack.connector.read(
            "supplier_statement", ReadQuery(filters={"file": file, **filters})
        )
    )


def test_utf8_statement_maps_to_complete_documents() -> None:
    stack, result = _read("statement_utf8.csv")

    assert result.completeness == "complete"
    assert result.degraded == ()
    assert len(result.items) == 2

    first = result.items[0]
    assert first["kind"] == "supplier_statement"
    assert first["doc_no"] == "ST-2026-0001"
    assert first["doc_date"] == "2026-10-31"
    assert first["party_code"] == "SUP-DEMO-01"
    assert first["party_name"] == "示例供应商一"
    assert len(first["lines"]) == 2
    assert first["total_amount"] == {
        "amount": "1650.0000",
        "currency": "CNY",
        "tax_included": True,
    }
    assert first["authority_system"] == "supplier"
    assert first["source_refs"]["file"] == "statement_utf8.csv#ST-2026-0001"
    assert stack.transport.parse_count == 1


def test_utf8_bom_is_accepted() -> None:
    stack = make_file_stack()
    result = run(
        stack.connector.read(
            "delivery_note", ReadQuery(filters={"file": "delivery_note_bom.csv"})
        )
    )

    assert result.completeness == "complete"
    assert len(result.items) == 1
    document = result.items[0]
    assert document["kind"] == "delivery_note"
    assert document["doc_no"] == "DN-2026-0001"
    assert len(document["lines"]) == 2
    assert document["total_amount"]["amount"] == "990.0000"


def test_dirty_rows_degrade_instead_of_being_silently_dropped() -> None:
    stack, result = _read("statement_bom_dirty.csv")

    assert result.completeness == "partial"
    assert {item["doc_no"] for item in result.items} == {
        "ST-2026-0003",
        "ST-2026-0004",
        "ST-2026-0005",
    }

    reasons = {degraded.reason for degraded in result.degraded}
    # 缺物料编码：该行不进模型，但整单仍保留并登记缺失。
    assert "mapping:missing:lines[2].item_code@ST-2026-0003" in reasons
    # 数量无法解析：保持 None，不填 0。
    assert "mapping:missing:lines[1].quantity@ST-2026-0004" in reasons
    # 金额 ≠ 数量 × 单价（容差 0.01）。
    assert "mapping:amount_mismatch:lines[1].amount@ST-2026-0005" in reasons
    # 缺单号/供应商：整单阻断，进 degraded 而不是伪造一条空单。
    assert "mapping:missing:doc_no@-" in reasons
    assert "mapping:missing:party_code@-" in reasons

    by_doc = {item["doc_no"]: item for item in result.items}
    # 存在不可用行时合计不计算（缺失 ≠ 0）。
    assert by_doc["ST-2026-0003"]["total_amount"] is None
    assert by_doc["ST-2026-0004"]["total_amount"] is None
    assert by_doc["ST-2026-0005"]["total_amount"] is None
    assert by_doc["ST-2026-0004"]["lines"][0]["quantity"] is None
    assert stack.transport.parse_count == 1


def test_read_typed_keeps_blocked_outcome_with_reason() -> None:
    stack = make_file_stack()
    outcomes = run(
        stack.connector.read_typed(
            "supplier_statement", ReadQuery(filters={"file": "statement_bom_dirty.csv"})
        )
    )

    assert len(outcomes) == 4
    blocked = [outcome for outcome in outcomes if outcome.record is None]
    assert len(blocked) == 1
    assert blocked[0].blocked is True
    assert "missing" in blocked[0].reason_codes()

    partial = next(
        outcome
        for outcome in outcomes
        if outcome.record is not None and outcome.record.doc_no == "ST-2026-0003"
    )
    assert partial.completeness == "partial"
    assert partial.record is not None
    assert partial.record.complete is False


def test_filters_narrow_documents_without_guessing() -> None:
    stack = make_file_stack()

    by_doc = run(
        stack.connector.read(
            "supplier_statement",
            ReadQuery(filters={"file": "statement_utf8.csv", "doc_no": "ST-2026-0002"}),
        )
    )
    assert [item["doc_no"] for item in by_doc.items] == ["ST-2026-0002"]

    by_supplier = run(
        stack.connector.read(
            "supplier_statement",
            ReadQuery(
                filters={"file": "statement_utf8.csv", "supplier_code": "SUP-DEMO-01"}
            ),
        )
    )
    assert [item["doc_no"] for item in by_supplier.items] == ["ST-2026-0001"]

    out_of_range = run(
        stack.connector.read(
            "supplier_statement",
            ReadQuery(filters={"file": "statement_utf8.csv", "date_from": "2026-11-01"}),
        )
    )
    assert out_of_range.items == ()
    assert out_of_range.completeness == "complete"


def test_missing_file_is_unavailable_not_empty() -> None:
    _stack, result = _read("no_such_file.csv")

    assert result.items == ()
    assert result.completeness == "unavailable"
    assert [degraded.reason for degraded in result.degraded] == [
        "read_failed:not_found"
    ]


def test_gbk_file_is_rejected_without_guessing_encoding(tmp_path: Path) -> None:
    path = tmp_path / "gbk.csv"
    path.write_bytes(
        "doc_no,doc_date,supplier_code\nST-1,2026-10-31,示例供应商\n".encode("gbk")
    )

    stack = make_file_stack(root=tmp_path)
    result = run(
        stack.connector.read("supplier_statement", ReadQuery(filters={"file": "gbk.csv"}))
    )

    assert result.completeness == "unavailable"
    assert [degraded.reason for degraded in result.degraded] == [
        "read_failed:schema_changed"
    ]


def test_header_missing_required_column_is_rejected(tmp_path: Path) -> None:
    (tmp_path / "bad.csv").write_text(
        "doc_no,doc_date\nST-1,2026-10-31\n", encoding="utf-8"
    )

    stack = make_file_stack(root=tmp_path)
    result = run(
        stack.connector.read("supplier_statement", ReadQuery(filters={"file": "bad.csv"}))
    )

    assert result.completeness == "unavailable"
    assert result.degraded[0].reason == "read_failed:schema_changed"


def test_header_only_file_is_a_valid_empty_read(tmp_path: Path) -> None:
    (tmp_path / "empty.csv").write_text(
        "doc_no,doc_date,supplier_code\n", encoding="utf-8"
    )

    stack = make_file_stack(root=tmp_path)
    result = run(
        stack.connector.read("supplier_statement", ReadQuery(filters={"file": "empty.csv"}))
    )

    assert result.items == ()
    assert result.completeness == "complete"
    assert result.degraded == ()


def test_unknown_columns_are_reported_not_mapped(tmp_path: Path) -> None:
    (tmp_path / "extra.csv").write_text(
        "doc_no,doc_date,supplier_code,memo\n"
        "ST-1,2026-10-31,SUP-1,无关备注\n",
        encoding="utf-8",
    )

    stack = make_file_stack(root=tmp_path)
    result = run(
        stack.connector.read("supplier_statement", ReadQuery(filters={"file": "extra.csv"}))
    )

    assert result.completeness == "partial"
    assert "ignored_columns:memo" in {
        degraded.reason for degraded in result.degraded
    }
    assert "memo" not in result.items[0]


def test_path_escape_and_unsupported_entity_are_refused() -> None:
    stack = make_file_stack()

    escaped = run(
        stack.connector.read(
            "supplier_statement", ReadQuery(filters={"file": "../statement_utf8.csv"})
        )
    )
    assert escaped.completeness == "unavailable"
    assert escaped.degraded[0].reason == "read_failed:invalid_request"

    unsupported = run(
        stack.connector.read("purchase_order", ReadQuery(filters={"file": "statement_utf8.csv"}))
    )
    assert unsupported.completeness == "unavailable"
    assert unsupported.degraded[0].reason == "unsupported_entity:purchase_order"


def test_capabilities_are_read_only() -> None:
    stack = make_file_stack()
    caps = stack.connector.describe()

    assert caps.objects == tuple(ENTITY_KINDS)
    assert caps.actions == ()
    assert caps.authority_map == {
        "supplier_statement": "supplier",
        "delivery_note": "supplier",
    }
    assert stack.connector.meta.system == "file"
    assert stack.connector.meta.access_level == "L1"
    assert stack.connector.meta.version_fingerprint.startswith("sha256:")


def test_fixtures_live_inside_repository() -> None:
    assert FILE_FIXTURES.is_dir()
    assert all(path.suffix == ".csv" for path in FILE_FIXTURES.glob("*.csv"))
