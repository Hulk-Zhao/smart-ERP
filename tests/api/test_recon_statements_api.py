"""``POST /v1/recon/statements``：上传供应商 CSV → 登记数据集 → 跑真实对账。"""

from __future__ import annotations

import json
from pathlib import Path

from conftest import PREFIX, STATEMENT, SYSTEM

DEMO_DIR = Path(__file__).resolve().parents[2] / "fixtures" / "recon" / "upload_demo"
CSV_NAME = "supplier_statement_2026-10.csv"
SUPPLIER = "SUP-DEMO-01"


def _our_dataset() -> str:
    return (DEMO_DIR / "our_dataset.json").read_text(encoding="utf-8")


def _upload(env, name: str, payload: bytes, dataset: str | None = None):
    return env.client.post(
        f"{PREFIX}/recon/statements",
        files={"file": (name, payload, "text/csv")},
        data={"dataset": _our_dataset() if dataset is None else dataset},
        headers=env.headers("initiator"),
    )


def _run(env, statement_file: str, idem: str):
    return env.client.post(
        f"{PREFIX}/recon/runs",
        json={
            "scope": env.scope(system_ids=[SYSTEM, STATEMENT]),
            "suppliers": [SUPPLIER],
            "sources": {"statement_files": [statement_file]},
        },
        headers=env.headers("initiator", idem),
    )


def test_upload_then_run_reports_two_matched_one_price_diff(env):
    payload = (DEMO_DIR / CSV_NAME).read_bytes()
    uploaded = _upload(env, CSV_NAME, payload)
    assert uploaded.status_code == 201, uploaded.text
    assert uploaded.json() == {
        "statement_file": CSV_NAME,
        "supplier_lines": 3,
        "our_lines": 3,
    }

    # 我方行必须原样取自种子（45/900），不能是上传文件里的 45.5/910。
    registered = env.container.recon_datasets.lookup([CSV_NAME])
    assert registered["our_lines"][2]["unit_price"] == "45"
    assert registered["our_lines"][2]["amount"] == "900"

    run = _run(env, CSV_NAME, "k-upload-run")
    assert run.status_code == 201, run.text
    batch = run.json()
    print("BATCH", json.dumps(batch, ensure_ascii=False))
    assert batch["metrics"]["supplier_lines"] == 3
    # 行级匹配按 (doc_no, line_no) 计：3 行都匹配上，其中 1 行单价不同 → 差异 1 条。
    assert batch["metrics"]["matched"] == 3
    assert batch["metrics"]["diff_lines"] == 1
    assert batch["metrics"]["diff_amount"] == "10.0000"
    assert batch["diff_count"] == 1
    assert batch["conclusive"] is True

    diffs = env.client.get(
        f"{PREFIX}/recon/runs/{batch['batch_id']}/diffs",
        params=env.scope_params(system_ids=[SYSTEM, STATEMENT]),
        headers=env.headers(),
    )
    assert diffs.status_code == 200, diffs.text
    items = diffs.json()["items"]
    print("DIFFS", json.dumps(items, ensure_ascii=False))
    assert len(items) == 1
    diff = items[0]
    assert diff["type"] == "unit_price"
    assert diff["ref"] == "file:PO-2026-0001#3"
    assert diff["amount"]["amount"] == "10.0000"
    assert diff["needs_manual"] is False
    assert diff["billing_bucket"] == "billable"


def test_crlf_file_parses_the_same(env):
    lf = (DEMO_DIR / CSV_NAME).read_bytes()
    crlf = lf.replace(b"\n", b"\r\n")
    uploaded = _upload(env, "supplier_statement_crlf.csv", crlf)
    assert uploaded.status_code == 201, uploaded.text
    assert uploaded.json()["supplier_lines"] == 3


def test_bad_row_is_rejected_with_line_number_and_not_registered(env):
    bad = (
        "doc_no,supplier_code,line_no,item_code,item_name,qty,unit,unit_price,amount,"
        "currency,tax_included,tax_rate,period\n"
        "PO-2026-0001,SUP-DEMO-01,1,IT-1001,示例外壳,100,PCS,12.5,1250,CNY,true,0.13,2026-10\n"
        "PO-2026-0001,SUP-DEMO-01,2,IT-1002,示例支架,50,打,8,400,CNY,true,0.13,2026-10\n"
    )
    uploaded = _upload(env, "bad_unit.csv", bad.encode("utf-8"))
    assert uploaded.status_code == 422, uploaded.text
    body = uploaded.json()
    assert body["code"] == "E-PARSE-FAILED"
    assert "第 3 行" in body["detail"]
    assert env.container.recon_datasets.lookup(["bad_unit.csv"]) is None


def test_missing_column_is_rejected(env):
    bad = "doc_no,line_no,item_code,qty,unit,amount\nPO-2026-0001,1,IT-1001,100,PCS,1250\n"
    uploaded = _upload(env, "missing_column.csv", bad.encode("utf-8"))
    assert uploaded.status_code == 422, uploaded.text
    assert uploaded.json()["code"] == "E-PARSE-FAILED"
    assert "缺少必需列" in uploaded.json()["detail"]


def test_our_lines_are_required(env):
    payload = (DEMO_DIR / CSV_NAME).read_bytes()
    uploaded = _upload(env, CSV_NAME, payload, dataset=json.dumps({"master_data": {}}))
    assert uploaded.status_code == 422, uploaded.text
    assert uploaded.json()["code"] == "E-PARSE-FAILED"
    assert "our_lines" in uploaded.json()["detail"]
