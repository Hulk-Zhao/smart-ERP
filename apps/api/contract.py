"""C16 契约的生成与冻结（与 ``contracts/`` 现有冻结机制一致：代码产出，禁止手写）。

用法::

    python -m apps.api.contract --write   # 重新生成冻结文件
    python -m apps.api.contract --check   # CI/评审校验，漂移即非零退出

契约文件 = ``FastAPI().openapi()`` 的确定性序列化（``sort_keys``；OpenAPI 版本取
FastAPI 默认的 3.1.0），因此任何路由/模型变更都会在 ``--check`` 里暴露。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from fastapi import FastAPI

from apps.api.routes import v1_router
from apps.platform.api.errors import install_error_handlers

CONTRACT_ID = "C16"
CONTRACT_VERSION = "1.0.0"
CONTRACT_REL_PATH = Path("contracts/api/product_api.openapi.v1.json")
BASE_URL = "/erp/api"

DESCRIPTION = """\
Smart-ERP 产品 API（C16）。所有路径都挂在 `/erp/api` 下，即本文件的 `servers` + `/v1`。

## 装配
app 装配由平台底座负责：`apps/main.py` 调用 `install_platform(app)`，本模块只向
`apps.platform.api.router.api_router` 注册 `/v1` 业务路由（导入 `apps.api` 即完成挂载），
不修改 `apps/main.py`。`trace_id` 与 problem+json 由平台中间件/异常处理器统一注入。

## 认证与会话
- 请求头 `X-Session-Token: <token>`（或 `Authorization: Bearer <token>`）。
- 缺失/过期/注销统一 401；令牌只映射到服务端会话，不接受客户端自报身份。

## 数据范围（G-05）
- 所有接口都必须携带 `org_id`、`period`、`system_ids`（查询参数或请求体 `scope`）。
- scope 只能收窄，不能越权放大：请求 scope 不在授权范围内返回 403 `E-AUTHZ-001`。

## 写操作治理
- 每个写动作先过 `AccessGate` 四道闸（身份/范围/等级/审批），再执行：
  - 外部写入只能经 `WriteGateway.execute()`（或回滚链路 `RollbackService`）；
  - W3 一律拒绝（`E-POLICY-W3FORBIDDEN`），拒绝时连接器零写入；
  - 内部对象追加（审批记录、状态推进、审计影像）不产生外部写入，只过 `AccessGate`。
- 审批与执行分离：审批人用 `/approvals/*` 落意见；发起人/执行体用
  `/tickets/{id}/actions/approve` 统一过闸并执行已锁定的写计划。

## 计量（结果计费，W0 只读）
- `GET /v1/metering/batches/{batch_id}/statement` 只输出程序性事实（是否自动放行、
  是否需人工、有无证据链），不判断「结论是否正确」；`format=csv` 导出逐行明细。
- 逐行 `billing_bucket`（`billable / auto_released / dispute_hold / other_unbillable`）
  与 statement 的四类计数由**同一个分类函数**产出；差异列表同样返回该字段并支持
  `billing_bucket=` 过滤，避免「计数说争议挂起 2、列表却有 3 行」。
- 缺失字段（已确认错放行/客户原因/是否被人改过/工单号/前像后像）返回 null，并在
  `data_gaps` 写明 `owner_role`（谁负责提供）与 `action`（需要提供什么）；
  单行单价只从 `ERP_METERING_UNIT_PRICE` 读取，未配置返回 503。

## 对账结果留存（append-only）
- `POST /v1/recon/runs` 成功后，批次头与逐行快照落库，statement 读持久化快照；
  进程重启/容器重建后同一批次的数字不变。
- 批次与行快照只追加：库上不可 UPDATE/DELETE（触发器兜底），
  **改判只能显式重跑开新批次**，原批次数据永久保持不变。
- 按原批次重跑：同时提供 `rerun_of`（原批次 id）与 `rerun_reason`（非空原因），
  服务端生成新的批次 id 并在新批次上记录 `rerun_of`；两者都是可选字段，
  不带时行为与旧版一致。
- 幂等：同租户/组织/期间/输入指纹只允许一个首次执行，重复提交 409 `E-IDEM-CONFLICT`；
  行级人工事件的去重键为「租户+操作人+行+事件+10 分钟时间窗」，
  重试返回原事件 `seq`，不因重试把 `seq` 撑大。
- 上线切换：旧进程内存里的历史批次**不迁移**，发布后不可查询，需重新发起对账生成；
  结果数据保留期 ≥24 个月（清理任务尚未实现，已在台账留 TODO）。

## 幂等与分页
- 写接口（POST）必须带 `Idempotency-Key`：同键同参回放首次响应，同键不同参 409；
  缺失 400 `E-IDEM-CONFLICT`。
- 列表统一 `{items, next_cursor}`，`limit` 默认 20、上限 100；游标不透明，
  非法游标 422 `E-PARSE-FAILED`。

## 错误格式
`application/problem+json`，字段：`type`、`title`、`status`、`detail`、`code`、
`trace_id`、`retriable`、`fail_code`。常用映射：

| HTTP | code | 含义 |
| --- | --- | --- |
| 400 | E-IDEM-CONFLICT | 写接口缺少 Idempotency-Key |
| 401 | E-AUTHZ-001 | 会话缺失/失效 |
| 403 | E-AUTHZ-001 | 超出授权 scope |
| 403 | E-AUTHZ-002 | 字段范围越界 |
| 403 | E-POLICY-W3FORBIDDEN | W3 或需要更高权限 |
| 403 | E-POLICY-LEVEL | 动作未注册/等级不足 |
| 404 | E-RESOURCE-NOTFOUND | 对象不存在（工单/对账批次/差异；跨租户同样 404） |
| 409 | E-POLICY-APPROVAL | 审批未满足、审批意见缺失、重复审批 |
| 409 | E-IDEM-CONFLICT | 同键不同参 |
| 409 | E-STATE-ILLEGAL | 状态迁移/重放非法 |
| 409 | E-VERIFY-MISMATCH | 写入校验或审计链校验不一致 |
| 422 | E-DATA-MISSING | 缺数据（数据集为空、原因/分类等必填项缺失） |
| 422 | E-PARSE-FAILED | 入参/游标/金额解析失败 |
| 503 | E-CONN-UNAVAILABLE | 连接器不可用或凭据交换失败 |
| 503 | E-DATA-MISSING | 计量配置缺失（未配置 ERP_METERING_UNIT_PRICE） |

`E-DATA-MISSING` 只表示「数据/配置缺失」（422 请求侧缺必填数据、503 服务侧配置缺失）；
凡「对象不存在」一律 404 `E-RESOURCE-NOTFOUND`，客户端不必再用字符串猜语义。
"""


def build_app() -> FastAPI:
    app = FastAPI(
        title="Smart-ERP 产品 API",
        description=DESCRIPTION,
        version=CONTRACT_VERSION,
        servers=[{"url": BASE_URL}],
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    install_error_handlers(app)
    app.include_router(v1_router)
    return app


def build_contract() -> dict[str, Any]:
    spec = build_app().openapi()
    info = spec.setdefault("info", {})
    info["x-contract-id"] = CONTRACT_ID
    info["x-contract-version"] = CONTRACT_VERSION
    return spec


def dump_contract(spec: dict[str, Any]) -> str:
    return json.dumps(spec, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def contract_path() -> Path:
    return Path(__file__).resolve().parents[2] / CONTRACT_REL_PATH


def write_contract(path: Path | None = None) -> Path:
    target = path or contract_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(dump_contract(build_contract()), encoding="utf-8")
    return target


def check_contract(path: Path | None = None) -> list[str]:
    target = path or contract_path()
    if not target.exists():
        return [f"缺少冻结契约文件: {target}"]
    expected = dump_contract(build_contract())
    actual = target.read_text(encoding="utf-8")
    if expected == actual:
        return []
    expected_lines = expected.splitlines()
    actual_lines = actual.splitlines()
    for index, (want, got) in enumerate(zip(expected_lines, actual_lines), start=1):
        if want != got:
            return [
                f"{target} 与代码漂移（第 {index} 行）",
                f"  期望: {want[:160]}",
                f"  实际: {got[:160]}",
                "请执行 python -m apps.api.contract --write 后重新评审",
            ]
    return [
        f"{target} 与代码漂移：行数 {len(actual_lines)} != {len(expected_lines)}",
        "请执行 python -m apps.api.contract --write 后重新评审",
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="C16 产品 API 契约冻结工具")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--write", action="store_true", help="重新生成冻结文件")
    group.add_argument("--check", action="store_true", help="校验冻结文件是否漂移")
    args = parser.parse_args(argv)

    if args.write:
        target = write_contract()
        print(f"已写入 {target}")
        return 0

    problems = check_contract()
    if problems:
        print("\n".join(problems), file=sys.stderr)
        return 1
    print("C16 契约与代码一致")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
