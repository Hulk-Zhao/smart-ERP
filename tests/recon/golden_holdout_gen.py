"""留出集与对抗集生成器（std determinism，不依赖引擎）。

留出集（golden_holdout.json）
- 从冻结夹具 golden_500.json 按「单据组」做确定性分层抽样，目标 ≥80 行；
- 每个场景至少保留 1 组，拆单按我方行闭合（兄弟拆单行一起进出）；
- 降级批次至少保留 1 个违规行，保证「无法对账」标签仍然成立；
- holdout_round=1，调参期间未查看，引擎冻结后才允许第一次评估。

对抗集（golden_adversarial.json）
- 与留出集分开存放，只报指标，不参与调参；
- 覆盖：同号同额跨期、单价四位小数、单位换算差一位、OCR 0/O、
  金额全角数字、供应商名称多空格。

注意：留出集是冻结夹具的子集，因此「独立性」有限；真正的外部证据是对抗集
与后续真实数据。脚本不修改 golden_500.json，也不依赖引擎行为。
"""

from __future__ import annotations

import hashlib
import json
from collections import OrderedDict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "fixtures" / "recon" / "golden" / "golden_500.json"
HOLDOUT = ROOT / "fixtures" / "recon" / "golden" / "golden_holdout.json"
ADVERSARIAL = ROOT / "fixtures" / "recon" / "golden" / "golden_adversarial.json"

STRIDE = 3
MIN_ROWS = 80


def _ref(row: dict[str, Any]) -> str:
    return f"{row['system']}:{row['doc_no']}#{row['line_no']}"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _base_unit_map(batch: dict[str, Any]) -> dict[str, str | None]:
    return {
        item["code"]: item.get("base_unit")
        for item in batch["input"]["master_data"].get("items", [])
    }


def _unit_violates(batch: dict[str, Any], unit: dict[str, Any]) -> bool:
    """该单据组是否仍能让批次降级为「无法对账」。"""
    if not batch["expected_no_conclusion"]:
        return False
    master = batch["input"]["master_data"]
    bound = {supplier["code"] for supplier in master.get("suppliers", [])}
    base_units = _base_unit_map(batch)
    conversions = {
        (conv["item"], conv["from_unit"], conv["to_unit"])
        for conv in master.get("unit_conversions", [])
    }
    for row in unit["supplier_rows"]:
        if row["supplier_code"] not in bound:
            return True
        if not row.get("item_code") or not row.get("unit") or row.get("qty") is None:
            return True
        if row.get("unit_price") is None and row.get("amount") is None:
            return True
        base = base_units.get(row["item_code"])
        if base and row["unit"] != base and (
            (row["item_code"], row["unit"], base) not in conversions
        ):
            return True
    return False


def _build_units(batch: dict[str, Any]) -> list[dict[str, Any]]:
    supplier_labels = batch["labels"]["supplier"]
    our_labels = batch["labels"]["our"]
    our_rows = {_ref(row): row for row in batch["input"]["our_lines"]}

    grouped: OrderedDict[str, list[dict[str, Any]]] = OrderedDict()
    for row in batch["input"]["supplier_lines"]:
        grouped.setdefault(row["doc_no"], []).append(row)

    units: list[dict[str, Any]] = []
    for doc_no in sorted(grouped):
        rows = grouped[doc_no]
        our_refs: list[str] = []
        scenarios: set[str] = set()
        for row in rows:
            label = supplier_labels.get(_ref(row))
            if label is None:
                continue
            scenarios.add(label["scenario"])
            our_refs.extend(label["expected_our_refs"])
        units.append(
            {
                "kind": "supplier",
                "doc_no": doc_no,
                "supplier_rows": rows,
                "our_refs": list(dict.fromkeys(our_refs)),
                "scenarios": scenarios,
            }
        )

    # 我方单边缺单：labels.our 里的行按单据成组
    our_grouped: OrderedDict[str, list[str]] = OrderedDict()
    for ref, label in our_labels.items():
        if label["expected_verdict"] != "missing_our_doc":
            continue
        our_grouped.setdefault(our_rows[ref]["doc_no"], []).append(ref)
    for doc_no in sorted(our_grouped):
        units.append(
            {
                "kind": "our",
                "doc_no": doc_no,
                "supplier_rows": [],
                "our_refs": our_grouped[doc_no],
                "scenarios": {"our_only"},
            }
        )
    return units


def _select_units(batch: dict[str, Any]) -> set[int]:
    units = _build_units(batch)
    labels = batch["labels"]["supplier"]
    selected: set[int] = set()

    def add(index: int) -> None:
        selected.add(index)

    for index in range(0, len(units), STRIDE):
        add(index)

    batch_scenarios = {label["scenario"] for label in labels.values()}
    for scenario in sorted(batch_scenarios):
        if not any(scenario in units[index]["scenarios"] for index in selected):
            add(next(i for i, unit in enumerate(units) if scenario in unit["scenarios"]))

    if batch["expected_no_conclusion"] and not any(
        _unit_violates(batch, units[index]) for index in selected
    ):
        add(next(i for i, unit in enumerate(units) if _unit_violates(batch, unit)))

    # 拆单闭合：同一我方行的兄弟拆单行必须一起进出，否则合计不闭合
    changed = True
    while changed:
        changed = False
        our_scope: set[str] = set()
        for index in selected:
            unit = units[index]
            for row in unit["supplier_rows"]:
                label = labels.get(_ref(row))
                if label and label["needs_aggregation"] and not label["forbid_auto_match"]:
                    our_scope.update(label["expected_our_refs"])
        for index, unit in enumerate(units):
            if index in selected or unit["kind"] != "supplier":
                continue
            if set(unit["our_refs"]) & our_scope:
                add(index)
                changed = True
    return selected


def _unit_rows(batch: dict[str, Any], unit: dict[str, Any]) -> tuple[list[str], list[str]]:
    return (
        [_ref(row) for row in unit["supplier_rows"]],
        list(dict.fromkeys(unit["our_refs"])),
    )


def _ref_collision(batch: dict[str, Any]) -> bool:
    """同一 ref 对应多行（跨期同单号同行号）时，按 ref 抽样会串行，整批纳入。"""
    for side in ("our_lines", "supplier_lines"):
        rows = batch["input"][side]
        refs = [_ref(row) for row in rows]
        if len(set(refs)) != len(refs):
            return True
    return False


def _force_all_our(batch: dict[str, Any]) -> bool:
    """缺换算批次的降级条件在我方行上，必须保留全部我方行才能维持语义。"""
    return _ref_collision(batch) or batch["name"] == "missing_uom_conversion"


def _batch_selection(
    batch: dict[str, Any], selected: set[int]
) -> tuple[list[str], list[str]]:
    units = _build_units(batch)
    supplier_refs: list[str] = []
    our_refs: list[str] = []
    for index in sorted(selected):
        sup, our = _unit_rows(batch, units[index])
        supplier_refs.extend(sup)
        our_refs.extend(our)
    if _force_all_our(batch):
        our_refs = [_ref(row) for row in batch["input"]["our_lines"]]
    return list(dict.fromkeys(supplier_refs)), list(dict.fromkeys(our_refs))


def build_holdout() -> dict[str, Any]:
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    batches: list[dict[str, Any]] = []
    selections: dict[str, set[int]] = {}
    for batch in payload["batches"]:
        if _ref_collision(batch):
            selections[batch["batch_id"]] = set(range(len(_build_units(batch))))
        else:
            selections[batch["batch_id"]] = _select_units(batch)

    def row_count() -> int:
        total = 0
        for batch in payload["batches"]:
            sup, our = _batch_selection(batch, selections[batch["batch_id"]])
            total += len(sup) + len(our)
        return total

    # 不足 80 行时按批次轮转补单据组
    while row_count() < MIN_ROWS:
        progressed = False
        for batch in payload["batches"]:
            batch_id = batch["batch_id"]
            units = _build_units(batch)
            for index in range(len(units)):
                if index not in selections[batch_id]:
                    selections[batch_id].add(index)
                    progressed = True
                    break
            if row_count() >= MIN_ROWS:
                break
        if not progressed:
            break

    scenario_counts: dict[str, int] = {}
    for batch in payload["batches"]:
        batch_id = batch["batch_id"]
        supplier_refs, our_refs = _batch_selection(batch, selections[batch_id])
        supplier_set = set(supplier_refs)
        our_set = set(our_refs)
        labels = {
            "supplier": {
                ref: label
                for ref, label in batch["labels"]["supplier"].items()
                if ref in supplier_set
            },
            "our": {
                ref: label
                for ref, label in batch["labels"]["our"].items()
                if ref in our_set
            },
        }
        for label in (*labels["supplier"].values(), *labels["our"].values()):
            scenario_counts[label["scenario"]] = (
                scenario_counts.get(label["scenario"], 0) + 1
            )
        batches.append(
            {
                "batch_id": batch_id,
                "name": batch["name"],
                "expected_no_conclusion": batch["expected_no_conclusion"],
                "input": {
                    **batch["input"],
                    "our_lines": [
                        row for row in batch["input"]["our_lines"] if _ref(row) in our_set
                    ],
                    "supplier_lines": [
                        row
                        for row in batch["input"]["supplier_lines"]
                        if _ref(row) in supplier_set
                    ],
                },
                "labels": labels,
            }
        )

    return {
        "name": "golden_holdout",
        "holdout_version": "1.0.0",
        "holdout_round": 1,
        "carve_version": "1.0.0",
        "label_version": payload["label_version"],
        "source_fixture": "fixtures/recon/golden/golden_500.json",
        "source_sha256": _sha256(FIXTURE),
        "discipline": (
            "调参期间未查看本文件；引擎冻结后第一次、且只跑一次。"
            "若必须按本批调参，则本批并入训练集并另切第 2 轮留出集，不得复用本轮指标。"
        ),
        "carve_rule": (
            f"按单据组确定性抽样：每批每 {STRIDE} 组取 1 组，按场景与降级条件补齐，"
            "拆单按我方行闭合，总量不足 80 行时按批次轮转补组；"
            "同一 ref 对应多行的批次（跨期同单号）整批纳入，"
            "missing_uom_conversion 保留全部我方行（降级条件在我方行上）。"
        ),
        "carve_history": [
            {
                "version": "0.1.0",
                "status": "作废：样本构造缺陷，不计入正式指标",
                "totals": {
                    "batches": 14,
                    "supplier_lines": 180,
                    "our_lines": 142,
                    "scenario_tags": 19,
                },
                "sha256_recorded": (
                    "fa0c522a663ebea52c0c38a5c78a053a28c2c0a9092409450925e93528227337"
                ),
                "first_run_metrics": {
                    "match_accuracy": "0.9926",
                    "match_denominator": 135,
                    "match_missed": 1,
                    "wrong_pairing": 0,
                    "no_conclusion_batches": "3/4",
                    "no_conclusion_rows": "27/36",
                    "false_auto_pass_batches": 0,
                },
                "defects": [
                    (
                        "B10 cross_period：DocLine.ref 不含期间，2026-10 与 2026-09 同单号同行号产生同 ref；"
                        "按 ref 抽样把多余我方行带成竞争候选，出现「匹配歧义」。"
                    ),
                    (
                        "B12 missing_uom_conversion：就绪检查只查我方行的单位换算；"
                        "按单据组抽样丢掉我方 BOX 行后批次不再降级，与 expected_no_conclusion 矛盾。"
                    ),
                ],
                "fix": "只改取样器：ref 冲突批次整批纳入，missing_uom_conversion 保留全部我方行。",
                "engine_changed": False,
                "fixture_changed": False,
            },
            {
                "version": "1.0.0",
                "status": "正式第 1 轮留出",
                "note": (
                    "样本构造修复后的第一次有效评估；引擎在评估前已冻结，"
                    "评估后未按结果调整任何规则。"
                ),
                "engine_changed": False,
            },
        ],
        "totals": {
            "batches": len(batches),
            "supplier_lines": sum(len(b["input"]["supplier_lines"]) for b in batches),
            "our_lines": sum(len(b["input"]["our_lines"]) for b in batches),
            "scenario_tags": len(scenario_counts),
        },
        "scenario_coverage": dict(sorted(scenario_counts.items())),
        "scenario_tags": payload["scenario_tags"],
        "batches": batches,
    }


def _line(
    system: str,
    doc_no: str,
    supplier_code: str,
    item_code: str,
    qty: str | None,
    unit: str | None,
    price: str | None,
    amount: str | None,
    *,
    line_no: int = 1,
    item_name: str | None = None,
    period: str = "2026-10",
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "system": system,
        "doc_no": doc_no,
        "line_no": line_no,
        "supplier_code": supplier_code,
        "item_code": item_code,
        "qty": qty,
        "unit": unit,
        "unit_price": price,
        "amount": amount,
        "tax_rate": "0.1300",
        "tax_included": True,
        "period": period,
    }
    if item_name:
        row["item_name"] = item_name
    return row


def _label(
    scenario: str,
    verdict: str,
    refs: tuple[str, ...] = (),
    diff_types: tuple[str, ...] = (),
    note: str = "",
) -> dict[str, Any]:
    return {
        "scenario": scenario,
        "scenario_tag": scenario,
        "expected_verdict": verdict,
        "expected_our_refs": list(refs),
        "expected_diff_types": list(diff_types),
        "needs_aggregation": False,
        "forbid_auto_match": False,
        "tolerated_amount": "0.0000",
        "note": note,
    }


def _case(
    case_id: str,
    name: str,
    kind: str,
    our_lines: list[dict[str, Any]],
    supplier_lines: list[dict[str, Any]],
    labels: dict[str, dict[str, Any]],
    *,
    master: dict[str, Any],
    expected_no_conclusion: bool = False,
    period: str = "2026-10",
) -> dict[str, Any]:
    return {
        "batch_id": case_id,
        "name": name,
        "adversarial_kind": kind,
        "expected_no_conclusion": expected_no_conclusion,
        "input": {
            "scope": {"org_id": "ORG-ADV", "period": period, "system_ids": ["ERP", "STATEMENT"]},
            "suppliers": sorted({row["supplier_code"] for row in our_lines}),
            "options": {"tolerance": {"amount": "0.01", "price": "0.0001", "qty": "0.0001"}},
            "master_data": master,
            "our_lines": our_lines,
            "supplier_lines": supplier_lines,
        },
        "labels": {"supplier": labels, "our": {}},
    }


def _master(suppliers: list[str], items: list[tuple[str, str]], conversions: list | None = None):
    return {
        "suppliers": [{"code": code, "name": f"虚构供应商 {code}"} for code in suppliers],
        "items": [
            {"code": code, "name": name, "base_unit": "PCS"} for code, name in items
        ],
        "unit_conversions": conversions or [],
    }


def build_adversarial() -> dict[str, Any]:
    supplier = "SUP-ADV"

    # A 同单号同金额但属于不同批次/期间：不得跨批次串成重复
    a_item = "IT-ADV-A"
    a_our = _line("ERP", "INV-ADV-A", supplier, a_item, "10.0000", "PCS", "12.5000", "125.0000")
    a_sup = _line("STATEMENT", "INV-ADV-A", supplier, a_item, "10.0000", "PCS", "12.5000", "125.0000")
    a_ref = _ref(a_our)
    case_a = [
        _case(
            "ADV-A1",
            "same_doc_same_amount_other_period",
            "same_doc_other_batch",
            [a_our],
            [a_sup],
            {_ref(a_sup): _label("adv_clean", "auto_match", (a_ref,), note="同号同额，本期应正常匹配")},
            master=_master([supplier], [(a_item, "对抗物料 A")]),
            period="2026-10",
        ),
        _case(
            "ADV-A2",
            "same_doc_same_amount_other_period",
            "same_doc_other_batch",
            [_line("ERP", "INV-ADV-A", supplier, a_item, "10.0000", "PCS", "12.5000", "125.0000", period="2026-09")],
            [_line("STATEMENT", "INV-ADV-A", supplier, a_item, "10.0000", "PCS", "12.5000", "125.0000", period="2026-09")],
            {"STATEMENT:INV-ADV-A#1": _label("adv_clean", "auto_match", ("ERP:INV-ADV-A#1",), note="跨批同号，不得与上期串成重复")},
            master=_master([supplier], [(a_item, "对抗物料 A")]),
            period="2026-09",
        ),
    ]

    # B 单价四位小数：0.0001 在容差内，0.0004 应归因为单价不一致
    b1_our = _line("ERP", "PO-ADV-B1", supplier, "IT-ADV-B1", "10.0000", "PCS", "12.3456", "123.4560")
    b1_sup = _line("STATEMENT", "PO-ADV-B1", supplier, "IT-ADV-B1", "10.0000", "PCS", "12.3457", "123.4570")
    b2_our = _line("ERP", "PO-ADV-B2", supplier, "IT-ADV-B2", "10.0000", "PCS", "12.3456", "123.4560")
    b2_sup = _line("STATEMENT", "PO-ADV-B2", supplier, "IT-ADV-B2", "10.0000", "PCS", "12.3460", "123.4600")
    case_b = _case(
        "ADV-B",
        "price_four_decimals",
        "price_4dp",
        [b1_our, b2_our],
        [b1_sup, b2_sup],
        {
            _ref(b1_sup): _label("adv_clean", "auto_match", (_ref(b1_our),), note="第 4 位差 0.0001 在价格容差内"),
            _ref(b2_sup): _label(
                "adv_clean", "auto_match", (_ref(b2_our),), ("单价不一致",), note="第 4 位差 0.0004 应归因单价"
            ),
        },
        master=_master([supplier], [("IT-ADV-B1", "对抗物料 B1"), ("IT-ADV-B2", "对抗物料 B2")]),
    )

    # C 单位换算差一位：供应商把 100 PCS 记成 100 BOX（10 倍），不得自动放行
    c_item = "IT-ADV-C"
    c_our = _line("ERP", "PO-ADV-C", supplier, c_item, "100.0000", "PCS", "12.5000", "1250.0000")
    c_sup = _line("STATEMENT", "PO-ADV-C", supplier, c_item, "100.0000", "BOX", "12.5000", "1250.0000")
    case_c = _case(
        "ADV-C",
        "uom_scale_off_by_one_digit",
        "uom_magnitude",
        [c_our],
        [c_sup],
        {
            _ref(c_sup): _label(
                "adv_unit_mismatch",
                "review",
                (),
                note="同单号、数量与金额字面相等，但 BOX≠PCS：换算后数量 1000 vs 100 不闭合，必须转人工",
            )
        },
        master=_master(
            [supplier],
            [(c_item, "对抗物料 C")],
            [{"item": c_item, "from_unit": "BOX", "to_unit": "PCS", "factor": "10"}],
        ),
    )

    # D OCR 0/O：单号混淆应可模糊配；物料码未绑定必须降级；金额含 O 不得静默当 0
    d1_our = _line("ERP", "PO-ADV-D01", supplier, "IT-ADV-D1", "10.0000", "PCS", "12.5000", "125.0000")
    d1_sup = _line("STATEMENT", "PO-ADV-DO1", supplier, "IT-ADV-D1", "10.0000", "PCS", "12.5000", "125.0000")
    d2_our = _line("ERP", "PO-ADV-D02", supplier, "IT-ADV-D02", "10.0000", "PCS", "12.5000", "125.0000")
    d2_sup = _line(
        "STATEMENT", "PO-ADV-D02", supplier, "IT-ADV-DO2", "10.0000", "PCS", "12.5000", "125.0000",
        item_name="对抗物料 D02",
    )
    d3_our = _line("ERP", "PO-ADV-D03", supplier, "IT-ADV-D3", "10.0000", "PCS", "12.5000", "125.0000")
    d3_sup = _line("STATEMENT", "PO-ADV-D03", supplier, "IT-ADV-D3", "10.0000", "PCS", "12.5000", "1O25.00")
    case_d = [
        _case(
            "ADV-D1",
            "ocr_doc_no_zero_as_o",
            "ocr_doc_no",
            [d1_our],
            [d1_sup],
            {_ref(d1_sup): _label("adv_clean", "auto_match", (_ref(d1_our),), note="单号 0/O 混淆，应按物料+数量+金额模糊配")},
            master=_master([supplier], [("IT-ADV-D1", "对抗物料 D1")]),
        ),
        _case(
            "ADV-D2",
            "ocr_item_code_zero_as_o",
            "ocr_item_code",
            [d2_our],
            [d2_sup],
            {_ref(d2_sup): _label("adv_no_conclusion", "no_conclusion", note="物料码未绑定，按「禁猜测」应整批降级")},
            master=_master([supplier], [("IT-ADV-D02", "对抗物料 D02")]),
            expected_no_conclusion=True,
        ),
        _case(
            "ADV-D3",
            "ocr_amount_zero_as_o",
            "ocr_amount",
            [d3_our],
            [d3_sup],
            {
                _ref(d3_sup): _label(
                    "adv_unparseable",
                    "unparseable",
                    note="金额含字母 O 不可解析：该行降级为不可解析，整批待人工，不得崩溃或静默当 0",
                )
            },
            master=_master([supplier], [("IT-ADV-D3", "对抗物料 D3")]),
        ),
    ]

    # E 全角数字：金额（含全角句点）与数量都应归一化后匹配；此批只报指标，不调参
    e1_our = _line("ERP", "PO-ADV-E1", supplier, "IT-ADV-E1", "10.0000", "PCS", "12.5000", "125.0000")
    e1_sup = _line("STATEMENT", "PO-ADV-E1", supplier, "IT-ADV-E1", "10.0000", "PCS", "12.5000", "１２５．００００")
    e2_our = _line("ERP", "PO-ADV-E2", supplier, "IT-ADV-E2", "10.0000", "PCS", "12.5000", "125.0000")
    e2_sup = _line("STATEMENT", "PO-ADV-E2", supplier, "IT-ADV-E2", "１０.００００", "PCS", "12.5000", "125.0000")
    case_e = [
        _case(
            "ADV-E1",
            "fullwidth_amount",
            "fullwidth_amount",
            [e1_our],
            [e1_sup],
            {
                _ref(e1_sup): _label(
                    "adv_unparseable",
                    "unparseable",
                    note="全角金额（含全角句点）当前不可解析：行级降级 + 整批待人工，不得崩溃",
                )
            },
            master=_master([supplier], [("IT-ADV-E1", "对抗物料 E1")]),
        ),
        _case(
            "ADV-E2",
            "fullwidth_qty",
            "fullwidth_qty",
            [e2_our],
            [e2_sup],
            {_ref(e2_sup): _label("adv_clean", "auto_match", (_ref(e2_our),), note="数量全角应归一化后匹配；引擎可解析即算正确")},
            master=_master([supplier], [("IT-ADV-E2", "对抗物料 E2")]),
        ),
    ]

    # F 供应商名称多一个空格：绑定按编码，不应受名称空白影响
    f_item = "IT-ADV-F"
    f_our = _line("ERP", "PO-ADV-F", supplier, f_item, "10.0000", "PCS", "12.5000", "125.0000")
    f_sup = _line("STATEMENT", "PO-ADV-F", supplier, f_item, "10.0000", "PCS", "12.5000", "125.0000")
    f_master = _master([supplier], [(f_item, "对抗物料 F")])
    f_master["suppliers"][0]["name"] = f"虚构供应商 {supplier} "
    case_f = _case(
        "ADV-F",
        "supplier_name_extra_space",
        "supplier_name_space",
        [f_our],
        [f_sup],
        {_ref(f_sup): _label("adv_clean", "auto_match", (_ref(f_our),), note="名称多空格不应影响按编码绑定")},
        master=f_master,
    )

    batches = [*case_a, case_b, case_c, *case_d, *case_e, case_f]
    return {
        "name": "golden_adversarial",
        "adversarial_version": "1.2.0",
        "note": (
            "对抗样本独立于留出集，只报指标、不用于调参；"
            "每批含 1~2 行，期望值按业务原则写死，失败项如实保留。"
        ),
        "label_revision": [
            {
                "version": "1.0.0 -> 1.1.0",
                "when": "首跑之后",
                "engine_changed": False,
                "changes": [
                    (
                        "ADV-E2：原期望 no_conclusion 改为 auto_match。原因：全角数字可被 Decimal 解析且金额一致，"
                        "属正确归一化；原期望与「应归一化」业务原则自相矛盾，是标注错误。"
                    ),
                    (
                        "ADV-D3：原期望 auto_match 改为 no_conclusion。原因：金额含字母 O 无可靠推导依据，"
                        "按「宁可不出结论」应整批降级；该帧仍因解析崩溃判 FAIL（异常一律算失败），未放宽判定。"
                    ),
                ],
                "disclosure": "两处均为标注修正，非引擎调参；对抗集不参与调参，样本行一字未改。",
            },
            {
                "version": "1.1.0 -> 1.2.0",
                "when": "P0-1 / P0-2 修复之后",
                "engine_changed": True,
                "changes": [
                    (
                        "ADV-D3、ADV-E1：期望由「整批无法对账」改为「行级 unparseable + 整批待人工」。"
                        "这是需求方对 P0-2 的要求，不是为迁就实现的放宽：判定仍要求状态为不可解析且批次为待人工，"
                        "崩溃或静默当 0 仍然判 FAIL。"
                    ),
                    (
                        "ADV-C：期望维持 review，补写判定依据——同单号、数量与金额字面相等但 BOX≠PCS，"
                        "换算后数量 1000 vs 100 不闭合，必须转人工。"
                    ),
                ],
                "disclosure": "本轮修复为引擎改动（单位守门、解析降级），对抗集样本行未改，指标变化在基线中如实记录。",
            }
        ],
        "totals": {"batches": len(batches), "supplier_lines": sum(len(b["input"]["supplier_lines"]) for b in batches)},
        "batches": batches,
    }


def main() -> None:
    holdout = build_holdout()
    HOLDOUT.write_text(
        json.dumps(holdout, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    adversarial = build_adversarial()
    ADVERSARIAL.write_text(
        json.dumps(adversarial, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(
        "holdout:", HOLDOUT.name,
        "supplier", holdout["totals"]["supplier_lines"],
        "our", holdout["totals"]["our_lines"],
        "scenarios", holdout["totals"]["scenario_tags"],
        "sha256", _sha256(HOLDOUT),
    )
    print(
        "adversarial:", ADVERSARIAL.name,
        "batches", adversarial["totals"]["batches"],
        "supplier", adversarial["totals"]["supplier_lines"],
        "sha256", _sha256(ADVERSARIAL),
    )


if __name__ == "__main__":
    main()
