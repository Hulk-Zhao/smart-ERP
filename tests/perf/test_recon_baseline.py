"""T704 压测脚本的静态约束：数据集可复现、不默认连库。"""

from __future__ import annotations

import importlib.util
from decimal import Decimal
from pathlib import Path

import pytest

SCRIPT = Path(__file__).with_name("recon_baseline.py")


def _load():
    spec = importlib.util.spec_from_file_location("t704_recon_baseline", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def baseline():
    return _load()


def test_dataset_is_one_to_one_and_unique(baseline) -> None:
    samples = [(Decimal(100), Decimal(1000))]
    raw = baseline.build_raw(2_000, samples, "unit")
    dataset = raw["dataset"]
    ours, sups = dataset["our_lines"], dataset["supplier_lines"]

    assert len(ours) == len(sups) == 2_000
    keys = [(row["supplier_code"], row["item_code"]) for row in ours]
    assert len(set(keys)) == len(keys), (
        "(supplier,item) 必须唯一，否则会出现候选笛卡尔积"
    )
    assert all(row["doc_no"].startswith("GR-") for row in ours)
    assert all(row["doc_no"].startswith("ST-") for row in sups)
    assert all(row["doc_no"].endswith("-unit") for row in ours)

    master = dataset["master_data"]
    supplier_codes = {row["code"] for row in master["suppliers"]}
    item_codes = {row["code"] for row in master["items"]}
    assert {row["supplier_code"] for row in ours} <= supplier_codes
    assert {row["item_code"] for row in ours} <= item_codes


def test_every_hundredth_row_has_quantity_gap(baseline) -> None:
    samples = [(Decimal(10), Decimal(100))]
    raw = baseline.build_raw(300, samples)
    ours = raw["dataset"]["our_lines"]
    sups = raw["dataset"]["supplier_lines"]

    for index, (our, sup) in enumerate(zip(ours, sups, strict=True)):
        expected = (
            Decimal(our["qty"]) * Decimal("1.02")
            if index % 100 == 0
            else Decimal(our["qty"])
        )
        assert Decimal(sup["qty"]) == expected, index


def test_generation_is_deterministic_per_tag(baseline) -> None:
    samples = [(Decimal(1), Decimal(2))]
    first = baseline.build_raw(500, samples, "same")
    second = baseline.build_raw(500, samples, "same")
    assert first == second
    other = baseline.build_raw(500, samples, "other")
    first_doc = first["dataset"]["our_lines"][0]["doc_no"]
    assert first_doc != other["dataset"]["our_lines"][0]["doc_no"]


def test_load_samples_skips_rows_without_qty_or_amount(baseline, tmp_path) -> None:
    csv_path = tmp_path / "recon_case.csv"
    csv_path.write_text(
        "qty_mine,our_amount\n"
        "10,100\n"
        ",100\n"
        "10,\n"
        "20,200\n",
        encoding="utf-8",
    )
    assert baseline.load_samples(csv_path) == [
        (Decimal(10), Decimal(100)),
        (Decimal(20), Decimal(200)),
    ]


def test_empty_sample_file_is_rejected(baseline, tmp_path) -> None:
    csv_path = tmp_path / "empty.csv"
    csv_path.write_text("qty_mine,our_amount\n,,\n", encoding="utf-8")
    with pytest.raises(SystemExit):
        baseline.load_samples(csv_path)
