"""演示用：供应商对账单 CSV 上传 → 对账数据集登记。

边界（与 ``B-RECON-FILE-WIRING`` 卡一致）：

- 只做 **api 侧登记**：multipart 字节 → ``map_supplier_statement_csv`` →
  ``container.recon_datasets.register(filename, dataset)``；不读目录、不连网关。
- ``our_lines`` / ``master_data`` 必须由调用方显式提供（演示用 ``fixtures/recon``
  的种子），**绝不拿上传的供应商行反填我方行**——那等于自己跟自己比。
- 数据集是进程内登记（api 单 worker）；演示流程中途不要重启 api。
- 解析失败抛 ``StatementCsvError``（``DocParseError`` 子类，不在
  ``apps.api.errors`` 映射表里），这里显式收敛为 422 ``E-PARSE-FAILED``，
  detail 带文件行号，不让它退化成 500。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import PurePath
from typing import Any

from apps.api.container import ApiContainer
from apps.docparse.statement_csv import StatementCsvError, map_supplier_statement_csv
from apps.platform.api.errors import ProblemError
from apps.platform.contracts.errors import ErrorCode

#: 上传文件名缺省值（浏览器/curl 未带 filename 时）
DEFAULT_FILENAME = "statement.csv"


@dataclass(frozen=True, slots=True)
class IngestedStatement:
    """登记结果：只回可核对的文件名与行数，不回显任何数据行。"""

    statement_file: str
    supplier_lines: int
    our_lines: int


def _reject(detail: str) -> ProblemError:
    return ProblemError(ErrorCode.PARSE_FAILED, detail, status_code=422)


def parse_our_side(dataset_raw: str) -> dict[str, Any]:
    """解析并校验「我方数据」表单字段（our_lines 必填，master_data/tolerance 可选）。"""

    try:
        parsed = json.loads(dataset_raw)
    except json.JSONDecodeError as exc:
        raise _reject(
            f"dataset 不是合法 JSON：第 {exc.lineno} 行第 {exc.colno} 列：{exc.msg}"
        ) from exc
    if not isinstance(parsed, dict):
        raise _reject("dataset 必须是 JSON 对象（含 our_lines，可选 master_data/tolerance）")

    our_lines = parsed.get("our_lines")
    if not isinstance(our_lines, list) or not our_lines:
        raise _reject("dataset.our_lines 必须是非空数组（我方 ERP 已有行，不能由上传文件反填）")
    for index, line in enumerate(our_lines, start=1):
        if not isinstance(line, dict):
            raise _reject(f"dataset.our_lines 第 {index} 条不是 JSON 对象")

    master_data = parsed.get("master_data", {})
    if not isinstance(master_data, dict):
        raise _reject("dataset.master_data 必须是 JSON 对象")

    dataset: dict[str, Any] = {
        "our_lines": our_lines,
        "master_data": master_data,
    }
    tolerance = parsed.get("tolerance")
    if tolerance is not None:
        if not isinstance(tolerance, dict):
            raise _reject("dataset.tolerance 必须是 JSON 对象")
        dataset["tolerance"] = tolerance
    return dataset


def ingest_statement(
    container: ApiContainer,
    *,
    filename: str,
    source: bytes,
    our_side_raw: str,
) -> IngestedStatement:
    """解析上传的供应商 CSV，与我方数据合并后登记；失败整文件拒收。"""

    name = PurePath(filename or "").name or DEFAULT_FILENAME
    try:
        statement = map_supplier_statement_csv(source, filename=name)
    except StatementCsvError as exc:
        raise _reject(str(exc)) from exc

    dataset = parse_our_side(our_side_raw)
    dataset["supplier_lines"] = [dict(line) for line in statement.supplier_lines]
    container.recon_datasets.register(name, dataset)
    return IngestedStatement(
        statement_file=name,
        supplier_lines=len(dataset["supplier_lines"]),
        our_lines=len(dataset["our_lines"]),
    )


__all__ = [
    "DEFAULT_FILENAME",
    "IngestedStatement",
    "ingest_statement",
    "parse_our_side",
]
