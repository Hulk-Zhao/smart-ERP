"""C10 错误码与 F 码映射。

对外错误体统一 RFC 9457 ``application/problem+json``，UI 按 ``fail_code``
决定「下一步」（PRD §6.3）。码表是代码常量，冻结件在
``contracts/errors/codes.v1.json``，改码必须先改契约（§4.3）。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict


class ErrorCode(str, Enum):
    AUTHZ_SCOPE = "E-AUTHZ-001"
    AUTHZ_FIELD = "E-AUTHZ-002"
    POLICY_W3 = "E-POLICY-W3FORBIDDEN"
    POLICY_LEVEL = "E-POLICY-LEVEL"
    POLICY_APPROVAL = "E-POLICY-APPROVAL"
    IDEM_CONFLICT = "E-IDEM-CONFLICT"
    STATE_ILLEGAL = "E-STATE-ILLEGAL"
    RESOURCE_NOT_FOUND = "E-RESOURCE-NOTFOUND"
    DATA_MISSING = "E-DATA-MISSING"
    DATA_UNBOUND = "E-DATA-UNBOUND"
    DATA_AMBIGUOUS = "E-DATA-AMBIGUOUS"
    PARSE_FAILED = "E-PARSE-FAILED"
    CONN_UNAVAILABLE = "E-CONN-UNAVAILABLE"
    CONN_RATE_LIMIT = "E-CONN-RATE_LIMIT"
    CONN_SCHEMA_CHANGED = "E-CONN-SCHEMA_CHANGED"
    VERIFY_MISMATCH = "E-VERIFY-MISMATCH"
    CONFLICT_VERSION = "E-CONFLICT-VERSION"
    EXTERNAL_EFFECTIVE = "E-EXTERNAL-EFFECTIVE"
    # 同步回滚入口被开关关闭：不是请求错误，也不是连接器故障，别让 UI 提示「重试」。
    ROLLBACK_UNAVAILABLE = "E-ROLLBACK-UNAVAILABLE"
    # 合同价目取价失败：与 E-DATA-MISSING 分开，运维才能分清「没配价」和「输入缺数据」。
    PRICE_MISSING = "E-PRICE-MISSING"
    PRICE_CURRENCY = "E-PRICE-CURRENCY"


FailCode = Literal["F1", "F2", "F3", "F4", "F5", "F6", "F7", "F8"]


@dataclass(frozen=True, slots=True)
class ErrorSpec:
    code: ErrorCode
    http_status: int
    fail_code: FailCode | None
    retriable: bool
    title: str
    next_step: str
    also_statuses: tuple[int, ...] = ()

    @property
    def http_statuses(self) -> tuple[int, ...]:
        """该码适用的全部 HTTP 状态码（主码 + 同语义的附加状态）。"""

        return tuple(sorted({self.http_status, *self.also_statuses}))


ERROR_SPECS: dict[ErrorCode, ErrorSpec] = {
    ErrorCode.AUTHZ_SCOPE: ErrorSpec(
        ErrorCode.AUTHZ_SCOPE, 403, None, False, "超出数据范围", "提供申请入口"
    ),
    ErrorCode.AUTHZ_FIELD: ErrorSpec(
        ErrorCode.AUTHZ_FIELD, 403, None, False, "字段 scope 不足（资金字段只读）", "联系管理员"
    ),
    ErrorCode.POLICY_W3: ErrorSpec(
        ErrorCode.POLICY_W3, 403, None, False, "W3 动作被拒", "安全事件，无重试"
    ),
    ErrorCode.POLICY_LEVEL: ErrorSpec(
        ErrorCode.POLICY_LEVEL, 403, None, False, "取严后等级不足", "联系管理员改配置"
    ),
    ErrorCode.POLICY_APPROVAL: ErrorSpec(
        ErrorCode.POLICY_APPROVAL, 409, None, False, "审批未满足", "跳审批页"
    ),
    ErrorCode.IDEM_CONFLICT: ErrorSpec(
        ErrorCode.IDEM_CONFLICT, 409, None, False, "同键不同参", "报障"
    ),
    ErrorCode.STATE_ILLEGAL: ErrorSpec(
        ErrorCode.STATE_ILLEGAL, 409, None, False, "非法状态迁移", "报障"
    ),
    ErrorCode.RESOURCE_NOT_FOUND: ErrorSpec(
        ErrorCode.RESOURCE_NOT_FOUND, 404, None, False, "对象不存在", "核对标识后重试"
    ),
    ErrorCode.DATA_MISSING: ErrorSpec(
        ErrorCode.DATA_MISSING,
        422,
        "F1",
        False,
        "缺数据",
        "显示缺失清单（不猜）",
        also_statuses=(503,),
    ),
    ErrorCode.DATA_UNBOUND: ErrorSpec(
        ErrorCode.DATA_UNBOUND, 422, "F4", False, "主数据未绑定", "绑定流程"
    ),
    ErrorCode.DATA_AMBIGUOUS: ErrorSpec(
        ErrorCode.DATA_AMBIGUOUS, 422, "F3", False, "匹配歧义", "人工裁决"
    ),
    ErrorCode.PARSE_FAILED: ErrorSpec(
        ErrorCode.PARSE_FAILED, 422, "F2", False, "解析失败", "转补录（带影像对照）"
    ),
    ErrorCode.CONN_UNAVAILABLE: ErrorSpec(
        ErrorCode.CONN_UNAVAILABLE, 503, "F5", True, "连接器不可用", "只读降级提示 + 重试"
    ),
    ErrorCode.CONN_RATE_LIMIT: ErrorSpec(
        ErrorCode.CONN_RATE_LIMIT, 429, "F5", True, "限流", "自动退避，显示预计恢复"
    ),
    ErrorCode.CONN_SCHEMA_CHANGED: ErrorSpec(
        ErrorCode.CONN_SCHEMA_CHANGED, 502, "F5", False, "版本漂移", "已降 W0，联系运维"
    ),
    ErrorCode.VERIFY_MISMATCH: ErrorSpec(
        ErrorCode.VERIFY_MISMATCH, 409, "F6", False, "写入值不符", "自动补偿中"
    ),
    ErrorCode.CONFLICT_VERSION: ErrorSpec(
        ErrorCode.CONFLICT_VERSION, 409, "F7", False, "并发版本变化", "重新取数"
    ),
    ErrorCode.EXTERNAL_EFFECTIVE: ErrorSpec(
        ErrorCode.EXTERNAL_EFFECTIVE, 409, "F8", False, "外部已生效", "生成处置任务（不可回滚）"
    ),
    # 同步回滚是被开关主动关掉的，不是「等服务起来就好」：
    # retriable=false，避免 UI 把它当 F5 类瞬时故障做自动重试。
    ErrorCode.ROLLBACK_UNAVAILABLE: ErrorSpec(
        ErrorCode.ROLLBACK_UNAVAILABLE,
        503,
        None,
        False,
        "同步回滚入口已关闭",
        "等补偿执行器路径上线；不要重复点击，也不要手工改库",
    ),
    # 取价失败是「服务侧配置缺失」，不是请求数据缺失：不限流、可重试（补价后）。
    # 两个码必须分开：无任何生效价 → 找商务/运维补价格表；有价但币种不符 → 核对配置币种。
    ErrorCode.PRICE_MISSING: ErrorSpec(
        ErrorCode.PRICE_MISSING,
        503,
        "F1",
        True,
        "无生效合同价",
        "在合同价目表登记生效价（找商务/运维），不是补业务单据",
    ),
    ErrorCode.PRICE_CURRENCY: ErrorSpec(
        ErrorCode.PRICE_CURRENCY,
        503,
        "F1",
        True,
        "价目币种与批次币种不符",
        "核对 ERP_METERING_CURRENCY 与合同价目币种；禁止跨币种折算",
    ),
}


class ProblemDetail(BaseModel):
    """RFC 9457 错误体（§5.1）。"""

    model_config = ConfigDict(extra="forbid")

    type: str = "about:blank"
    code: ErrorCode
    title: str
    detail: str
    trace_id: str | None = None
    retriable: bool = False
    fail_code: FailCode | None = None
    # 排障上下文（如 currency / tenant_id / at）。只放「定位问题所需的最小事实」，
    # 严禁把价格表里其他币种的单价塞进来——那是别人的合同价。
    context: dict[str, str] | None = None


def problem(
    code: ErrorCode,
    detail: str,
    *,
    trace_id: str | None = None,
    context: dict[str, str] | None = None,
) -> ProblemDetail:
    spec = ERROR_SPECS[code]
    return ProblemDetail(
        code=code,
        title=spec.title,
        detail=detail,
        trace_id=trace_id,
        retriable=spec.retriable,
        fail_code=spec.fail_code,
        context=context,
    )


def error_contract() -> dict[str, object]:
    """生成 ``contracts/errors/codes.v1.json`` 的内容。"""

    return {
        "contract": "errors",
        "version": "1.0",
        "format": "E-{DOMAIN}-{NNN}",
        "errors": [
            {
                "code": spec.code.value,
                "http_status": spec.http_status,
                "http_statuses": list(spec.http_statuses),
                "fail_code": spec.fail_code,
                "retriable": spec.retriable,
                "title": spec.title,
                "next_step": spec.next_step,
            }
            for spec in sorted(ERROR_SPECS.values(), key=lambda s: s.code.value)
        ],
    }
