"""产品 API 的异常翻译：把引擎层异常收敛成平台层 ``ProblemError``。

约束（对齐 ``apps/platform/api``）：

- 不新造错误码，只用 ``contracts/errors/codes.v1.json`` 里已冻结的 ``E-*``；
- ``trace_id`` 由平台中间件注入，这里不生成、不覆盖；
- 响应体由平台 ``install_error_handlers`` 统一落 ``application/problem+json``。

翻译只做「代码 → HTTP 语义」的收敛，不吞异常：不在映射表里的异常原样抛出，
交给平台兜底（500 + E-CONN-UNAVAILABLE），避免把内部错误伪装成业务拒绝。
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from apps.agents.state_machine import TransitionError
from apps.audit.ledger import AuditChainError
from apps.audit.rollback import RollbackForbidden, RollbackRejected
from apps.audit.store import AuditStoreError
from apps.platform.api.errors import ProblemError
from apps.platform.contracts.errors import ErrorCode
from apps.platform.contracts.ticket import IllegalTransition
from apps.platform.policy.gateway import WriteGatewayError
from apps.security.approvals import ApprovalError
from apps.security.gate import AccessDenied
from apps.security.identity import IdentityError
from apps.security.scope import ScopeViolation


class IdemConflict(Exception):
    """同一幂等键携带了不同请求体（G-02）。"""


def as_problem(exc: BaseException) -> ProblemError | None:
    """返回可映射的 ``ProblemError``；不可映射返回 ``None``（交给平台兜底）。"""

    if isinstance(exc, ProblemError):
        return exc

    # 顺序：子类在前，避免父类把更具体的错误码盖掉（W3NotApprovable 等）。
    if isinstance(exc, ScopeViolation):
        return ProblemError(ErrorCode.AUTHZ_SCOPE, str(exc))
    if isinstance(exc, AccessDenied):
        return ProblemError(exc.code, str(exc))
    if isinstance(exc, ApprovalError):
        return ProblemError(exc.code, str(exc))
    if isinstance(exc, WriteGatewayError):
        return ProblemError(exc.code, str(exc))
    if isinstance(exc, RollbackForbidden):
        return ProblemError(ErrorCode.POLICY_W3, str(exc))
    if isinstance(exc, RollbackRejected):
        return ProblemError(ErrorCode.POLICY_APPROVAL, str(exc))
    if isinstance(exc, AuditChainError):
        return ProblemError(ErrorCode.VERIFY_MISMATCH, str(exc))
    if isinstance(exc, AuditStoreError):
        return ProblemError(ErrorCode.STATE_ILLEGAL, str(exc))
    if isinstance(exc, TransitionError):
        return ProblemError(ErrorCode.STATE_ILLEGAL, str(exc))
    if isinstance(exc, IllegalTransition):
        return ProblemError(ErrorCode.STATE_ILLEGAL, str(exc))
    if isinstance(exc, IdentityError):
        # 会话过期 / 租约与工单不匹配：让前端重新登录并重试一次（FR-1401）。
        return ProblemError(ErrorCode.AUTHZ_SCOPE, str(exc), status_code=401)
    if isinstance(exc, IdemConflict):
        return ProblemError(ErrorCode.IDEM_CONFLICT, str(exc))
    if isinstance(exc, ValueError):
        return ProblemError(ErrorCode.PARSE_FAILED, str(exc))
    return None


@contextmanager
def translated() -> Iterator[None]:
    """在路由/服务边界把已知领域异常翻译为 problem+json。"""

    try:
        yield
    except ProblemError:
        raise
    except Exception as exc:
        problem = as_problem(exc)
        if problem is None:
            raise
        raise problem from exc


__all__ = ["IdemConflict", "as_problem", "translated"]
