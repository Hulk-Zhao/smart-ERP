# 上传对账单演示数据（最小通路）

- `supplier_statement_2026-09.csv`：UTF-8 + LF；3 行，第 3 行单价与 ERP 不一致（45.5 vs 45）。
- `supplier_statement_2026-09_crlf.csv`：内容同上，UTF-8 + CRLF，用来验证换行不影响解析。
- `our_dataset.json`：我方数据（`our_lines` + `master_data` + `tolerance`），**不是**上传行的副本。

预期画面（实测）：上传返回 3 行并登记；用 `sources.statement_files` 跑对账 → 批次「已对账」，3 行全部行级匹配，其中 2 行零差异、1 行单价差异（`unit_price`，10.0000 CNY，`file:PO-2026-0001#3`），`diff_count=1`、`needs_manual=false`。

复现：

```bash
curl -s -X POST http://127.0.0.1:8010/erp/api/v1/recon/statements \
  -H "X-Session-Token: <token>" \
  -F "file=@fixtures/recon/upload_demo/supplier_statement_2026-09.csv" \
  -F "dataset=$(cat fixtures/recon/upload_demo/our_dataset.json)"
```
