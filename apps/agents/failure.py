"""F1–F8 失败分类（PRD §5.0，全部 Agent 复用）。

分类只做两件事：把异常/C10 错误码映射到 F 码，并给出统一处置动作。
运行时不在这里做决策，只按 F 码走状态机。
"""

from __future__ import annotations

from dataclasses import dataclass

from apps.platform.contracts.errors import ERROR_SPECS, ErrorCode, FailCode
from apps.platform.policy.gateway import (
    ApprovalRequiredError,
    FieldScopeViolation,
    ForbiddenWriteError,
    PreviewFailedError,
    VerifyMismatchError,
)


class TransientWriteError(Exception):
    """连接器明确返回的可重试失败（5xx / 限流 / 抖动），对应 F5。"""


@dataclass(frozen=True, slots=True)
class Failure:
    code: FailCode
    title: str
    handling: str
    retriable: bool


FAILURES: dict[str, Failure] = {
    "F1": Failure("F1", "数据缺失", "阻断并输出缺失清单（不猜）", False),
    "F2": Failure("F2", "解析失败", "转人工队列，保留原始件", False),
    "F3": Failure("F3", "匹配歧义", "不下结论，交人裁决", False),
    "F4": Failure("F4", "主数据缺失/未绑定", "触发绑定流程", False),
    "F5": Failure("F5", "接口失败", "退避重试 3 次，失败进补偿", True),
    "F6": Failure("F6", "校验不一致", "立即停止并补偿", False),
    "F7": Failure("F7", "并发冲突", "中止并重新取数（不覆盖）", False),
    "F8": Failure("F8", "外部已生效", "不改数据，生成处置任务", False),
}


def failure_of(code: FailCode | str) -> Failure:
    try:
        return FAILURES[str(code)]
    except KeyError as exc:
        raise ValueError(f"未知失败分类: {code!r}") from exc


def classify_error_code(code: ErrorCode) -> Failure | None:
    """C10 错误码 → F 码；没有 F 码（鉴权/策略类）返回 ``None``。"""

    spec = ERROR_SPECS[code]
    return failure_of(spec.fail_code) if spec.fail_code else None


def classify_exception(exc: BaseException) -> Failure | None:
    """运行时异常 → F 码。``UnknownOutcome`` 不在此映射（必须走 verify）。"""

    if isinstance(exc, PreviewFailedError):
        return failure_of("F1")
    if isinstance(exc, VerifyMismatchError):
        return failure_of("F6")
    if isinstance(exc, TransientWriteError):
        return failure_of("F5")
    if isinstance(exc, (ForbiddenWriteError, ApprovalRequiredError, FieldScopeViolation)):
        return None
    code = getattr(exc, "code", None)
    if isinstance(code, ErrorCode):
        return classify_error_code(code)
    return None
