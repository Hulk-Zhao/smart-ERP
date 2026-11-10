"""计量表：只读、按行程序性事实、金额来自配置、零写入。

构造一个含四类行的批次：
- 自动放行（引擎判定已匹配、无需人工）
- 人工复核（金额不平、无法归因 → 待人工）
- 争议挂起（多候选置信度接近 → 匹配歧义）
- 其他不计费（供应商有、我方无 → 缺单）

「已确认错放行 / 客户原因」当前无数据源，断言字段为 null 且 data_gaps 逐条报缺。
"""

from __future__ import annotations

import hashlib

from conftest import PERIOD, PREFIX, STATEMENT, SYSTEM

# 与响应字段表一一对应；刻意在测试里硬编码，防止实现与字段表一起漂移。
CSV_COLUMNS = (
    "line_no,side,supplier_code,our_ref,supplier_ref,status,billing_bucket,auto_released,"
    "needs_manual,human_modified,ticket_no,before_ref,after_ref,excluded_reason,"
    "diff_types,owner,confidence,source_refs,evidence"
)

UNIT_PRICE = "0.30"


def _line(
    *,
    system: str,
    doc_no: str,
    item_code: str,
    qty: str = "10",
    unit_price: str = "5",
    amount: str,
) -> dict[str, object]:
    return {
        "system": system,
        "doc_no": doc_no,
        "line_no": 1,
        "supplier_code": "SUP-A",
        "item_code": item_code,
        "qty": qty,
        "unit": "PCS",
        "unit_price": unit_price,
        "amount": amount,
        "tax_rate": "0.13",
        "period": PERIOD,
    }


def _body(env) -> dict[str, object]:
    return {
        "scope": env.scope(system_ids=[SYSTEM, STATEMENT]),
        "suppliers": ["SUP-A"],
        "dataset": {
            "our_lines": [
                _line(system=SYSTEM, doc_no="PO-1", item_code="IT-A", amount="50.00"),
                _line(system=SYSTEM, doc_no="PO-2", item_code="IT-B", amount="50.00"),
                _line(system=SYSTEM, doc_no="PO-A1", item_code="IT-C", amount="50.00"),
                _line(system=SYSTEM, doc_no="PO-A2", item_code="IT-C", amount="50.00"),
            ],
            "supplier_lines": [
                _line(system=STATEMENT, doc_no="PO-1", item_code="IT-A", amount="50.00"),
                _line(system=STATEMENT, doc_no="PO-2", item_code="IT-B", amount="50.50"),
                _line(system=STATEMENT, doc_no="ST-AMB", item_code="IT-C", amount="50.00"),
                _line(system=STATEMENT, doc_no="ST-X", item_code="IT-D", qty="3", amount="15.00"),
            ],
            "master_data": {
                "suppliers": [{"code": "SUP-A", "name": "供应商A"}],
                "items": [
                    {"code": "IT-A", "name": "物料A", "base_unit": "PCS"},
                    {"code": "IT-B", "name": "物料B", "base_unit": "PCS"},
                    {"code": "IT-C", "name": "物料C", "base_unit": "PCS"},
                    {"code": "IT-D", "name": "物料D", "base_unit": "PCS"},
                ],
            },
            "tolerance": {},
        },
    }


def _configure(monkeypatch, **overrides: str) -> None:
    values = {
        "ERP_METERING_UNIT_PRICE": UNIT_PRICE,
        "ERP_METERING_CURRENCY": "CNY",
        "ERP_METERING_PRIOR_AMOUNT": "1.00",
        "ERP_METERING_CAP_AMOUNT": "5.00",
    }
    values.update(overrides)
    for key, value in values.items():
        monkeypatch.setenv(key, value)


def _run_batch(env, key: str) -> str:
    response = env.client.post(
        f"{PREFIX}/recon/runs", json=_body(env), headers=env.headers("initiator", key)
    )
    assert response.status_code == 201, response.text
    return response.json()["batch_id"]


def _statement(env, batch_id: str, **params: str):
    return env.client.get(
        f"{PREFIX}/metering/batches/{batch_id}/statement",
        params=env.scope_params(system_ids=[SYSTEM, STATEMENT]) + list(params.items()),
        headers=env.headers(),
    )


def test_statement_classifies_lines_and_amounts(env, monkeypatch):
    _configure(monkeypatch)
    batch_id = _run_batch(env, "k-metering")

    response = _statement(env, batch_id)
    assert response.status_code == 200, response.text
    body = response.json()

    # 计量表字段（产品文档 07 §2.2.1 口径）
    assert body["batch_id"] == batch_id
    assert body["period"] == PERIOD
    assert body["supplier_scope"] == ["SUP-A"]
    assert body["expected_lines"] == 4
    assert body["auto_released_lines"] == 1
    assert body["billable_lines"] == 1
    assert body["confirmed_misrelease_lines"] is None
    assert body["customer_reason_lines"] is None
    assert body["human_reviewed_lines"] is None
    assert body["unit_price"] == "0.3000"
    assert body["unit_price_source"] == "ERP_METERING_UNIT_PRICE"
    assert body["period_amount"] == "0.3000"
    assert body["prior_cumulative_amount"] == "1.0000"
    assert body["cumulative_amount"] == "1.3000"
    assert body["cap_amount"] == "5.0000"
    assert body["cap_remaining"] == "3.7000"
    assert body["cap_exceeded"] is False

    # 不计费行：争议挂起 2 + 其他 1；错放行/客户原因无数据源
    excluded = body["excluded_lines"]
    assert excluded["dispute_hold"] == 2
    assert excluded["other_unbillable"] == 1
    assert excluded["other_reasons"] == {"unmatched_supplier": 1}
    assert excluded["misrelease"] is None
    assert excluded["customer_reason"] is None
    assert excluded["total"] == 3

    # 有效行是「上界」，口径可解释
    assert "上界" in body["billable_basis"]
    assert "上界" in body["calculation"][-4]
    assert body["line_accounting"].startswith("应处理 4 = 自动放行 1 + 争议挂起 2 + 其他不计费 1")

    # 按行事实：自动放行 / 人工复核 / 争议挂起 / 其他不计费
    lines = {line["supplier_ref"]: line for line in body["lines"]}
    assert len(lines) == 4
    auto = lines[f"{STATEMENT}:PO-1#1"]
    assert auto["status"] == "已匹配"
    assert auto["auto_released"] is True
    assert auto["needs_manual"] is False
    assert auto["excluded_reason"] == ""
    assert auto["confidence"] == "1"
    assert auto["source_refs"] == [f"{SYSTEM}:PO-1#1", f"{STATEMENT}:PO-1#1"]

    manual = lines[f"{STATEMENT}:PO-2#1"]
    assert manual["status"] == "待人工"
    assert manual["needs_manual"] is True
    assert manual["excluded_reason"] == "dispute_hold"

    dispute = lines[f"{STATEMENT}:ST-AMB#1"]
    assert dispute["status"] == "匹配歧义"
    assert dispute["needs_manual"] is True
    assert dispute["excluded_reason"] == "dispute_hold"

    other = lines[f"{STATEMENT}:ST-X#1"]
    assert other["status"] == "供应商未匹配"
    assert other["auto_released"] is False
    assert other["excluded_reason"] == "unmatched_supplier"
    assert other["diff_types"] == ["missing_doc"]
    assert other["owner"] == "supplier"
    assert other["supplier_code"] == "SUP-A"
    assert other["evidence"], "缺单行必须带引擎证据"

    # 计费桶：逐行值与四类计数同源，四桶之和必须等于应处理行数（口径不脱节）
    bucket_of = {ref: line["billing_bucket"] for ref, line in lines.items()}
    assert bucket_of[f"{STATEMENT}:PO-1#1"] == "billable"
    assert bucket_of[f"{STATEMENT}:PO-2#1"] == "dispute_hold"
    assert bucket_of[f"{STATEMENT}:ST-AMB#1"] == "dispute_hold"
    assert bucket_of[f"{STATEMENT}:ST-X#1"] == "other_unbillable"
    bucket_counts = {
        bucket: sum(1 for value in bucket_of.values() if value == bucket)
        for bucket in ("billable", "auto_released", "dispute_hold", "other_unbillable")
    }
    assert bucket_counts == {
        "billable": 1,
        "auto_released": 0,
        "dispute_hold": 2,
        "other_unbillable": 1,
    }
    assert sum(bucket_counts.values()) == body["expected_lines"] == len(body["lines"])
    assert any("计费桶 = billable 1" in item for item in body["calculation"])

    # 缺失字段严格报缺，不用默认值冒充；每条缺口必须给出责任角色与待办动作
    gaps = {gap["field"]: gap for gap in body["data_gaps"]}
    assert {
        "confirmed_misrelease_lines",
        "customer_reason_lines",
        "human_reviewed_lines/human_modified",
        "ticket_no",
        "before_ref/after_ref",
    } <= set(gaps)
    for field, gap in gaps.items():
        assert gap["owner_role"], f"{field} 缺 owner_role"
        assert gap["action"], f"{field} 缺 action"
    assert gaps["customer_reason_lines"]["owner_role"] == "客户成功/商务"
    assert all(line["ticket_no"] is None for line in body["lines"])
    assert all(line["human_modified"] is None for line in body["lines"])

    # 只读含义：不写外部系统（连接器零预演、零写入）；
    # 数据库/快照写入是本批新引入的预期行为，单独断言，不混在同一个断言里。
    assert env.connector.writes == []
    assert env.connector.previews == []
    assert env.recon_store_path.exists()


def test_statement_csv_matches_field_table(env, monkeypatch):
    _configure(monkeypatch)
    batch_id = _run_batch(env, "k-metering-csv")

    response = _statement(env, batch_id, format="csv")
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/csv")
    digest = hashlib.sha256(response.content).hexdigest()
    assert response.headers["X-Content-SHA256"] == digest
    assert f'metering-{batch_id}.csv' in response.headers["Content-Disposition"]

    rows = response.content.decode("utf-8-sig").splitlines()
    assert rows[0] == CSV_COLUMNS
    assert len(rows) == 1 + 4
    assert rows[1].startswith("1,supplier,")
    assert "已匹配,billable,true,false" in rows[1]
    assert rows[2].startswith("2,supplier,")
    assert "待人工,dispute_hold,false,true" in rows[2]
    assert "dispute_hold" in rows[2]

    assert env.connector.writes == []
    assert env.connector.previews == []


def test_diffs_filter_by_billing_bucket(env, monkeypatch):
    """差异列表的 billing_bucket 与计量表逐行值同源，可按桶过滤。"""

    _configure(monkeypatch)
    batch_id = _run_batch(env, "k-metering-bucket")

    def _diffs(**params: str):
        return env.client.get(
            f"{PREFIX}/recon/runs/{batch_id}/diffs",
            params=env.scope_params(system_ids=[SYSTEM, STATEMENT])
            + list(params.items()),
            headers=env.headers(),
        )

    everything = _diffs()
    assert everything.status_code == 200, everything.text
    items = everything.json()["items"]
    assert items, "该数据集至少应有一条差异"
    buckets = {item["type"]: item["billing_bucket"] for item in items}
    # 缺单行归「其他不计费」；该数据集没有挂在自动放行行上的差异
    assert buckets["missing_doc"] == "other_unbillable"
    assert set(buckets.values()) <= {
        "billable",
        "auto_released",
        "dispute_hold",
        "other_unbillable",
    }

    other = _diffs(billing_bucket="other_unbillable")
    assert other.status_code == 200, other.text
    assert {item["type"] for item in other.json()["items"]} == {"missing_doc"}

    # 桶为空时必须返回空列表，而不是退回全量（过滤条件不被吞掉）
    assert _diffs(billing_bucket="billable").json()["items"] == []
    assert _diffs(billing_bucket="dispute_hold").json()["items"] == []
    assert _diffs(billing_bucket="auto_released").json()["items"] == []

    invalid = _diffs(billing_bucket="not-a-bucket")
    assert invalid.status_code == 422
    assert invalid.json()["code"] == "E-PARSE-FAILED"


def test_statement_requires_configured_unit_price(env, monkeypatch):
    for key in (
        "ERP_METERING_UNIT_PRICE",
        "ERP_METERING_CURRENCY",
        "ERP_METERING_PRIOR_AMOUNT",
        "ERP_METERING_CAP_AMOUNT",
    ):
        monkeypatch.delenv(key, raising=False)
    batch_id = _run_batch(env, "k-metering-no-config")

    response = _statement(env, batch_id)
    assert response.status_code == 503, response.text
    assert response.json()["code"] == "E-DATA-MISSING"
    assert "ERP_METERING_UNIT_PRICE" in response.json()["detail"]
    assert env.connector.writes == []
