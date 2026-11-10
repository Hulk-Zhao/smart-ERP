"""统一错误响应：所有异常都落成 RFC 9457 ``application/problem+json``，且带 ``trace_id``。

错误码沿用 ``contracts/errors/codes.v1.json`` 的 18 个 ``E-*``（冻结契约，改码必须先改契约）。
传输层异常（405/500 等）没有专属业务码，按语义就近映射到现有码，响应状态码仍保留
原始 HTTP 状态，避免为了包装层新增错误码而破坏冻结契约。
"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from apps.platform.api.context import current_trace_id
from apps.platform.contracts.errors import ERROR_SPECS, ErrorCode, problem

PROBLEM_CONTENT_TYPE = "application/problem+json"

# 传输层状态 → 最近似的既有业务码（不新增码）。
_STATUS_TO_CODE: dict[int, ErrorCode] = {
    400: ErrorCode.PARSE_FAILED,
    401: ErrorCode.AUTHZ_SCOPE,
    403: ErrorCode.AUTHZ_SCOPE,
    404: ErrorCode.RESOURCE_NOT_FOUND,
    405: ErrorCode.STATE_ILLEGAL,
    409: ErrorCode.STATE_ILLEGAL,
    412: ErrorCode.CONFLICT_VERSION,
    422: ErrorCode.PARSE_FAILED,
    429: ErrorCode.CONN_RATE_LIMIT,
    503: ErrorCode.CONN_UNAVAILABLE,
}


class ProblemError(Exception):
    """业务侧抛出即可得到统一的 problem+json（不用手写 Response）。"""

    def __init__(
        self,
        code: ErrorCode,
        detail: str,
        *,
        status_code: int | None = None,
    ) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.status_code = status_code or ERROR_SPECS[code].http_status


def _code_for_status(status_code: int) -> ErrorCode:
    mapped = _STATUS_TO_CODE.get(status_code)
    if mapped is not None:
        return mapped
    return ErrorCode.CONN_UNAVAILABLE if status_code >= 500 else ErrorCode.PARSE_FAILED


def problem_response(
    code: ErrorCode,
    detail: str,
    *,
    status_code: int,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    """构造 problem+json 响应；``trace_id`` 取自当前请求上下文。"""

    body = problem(code, detail, trace_id=current_trace_id() or None)
    return JSONResponse(
        status_code=status_code,
        content=body.model_dump(mode="json"),
        media_type=PROBLEM_CONTENT_TYPE,
        headers=headers,
    )


def install_error_handlers(app: FastAPI) -> None:
    """注册四类异常处理器（重复调用无副作用）。"""

    if getattr(app.state, "_erp_error_handlers_installed", False):
        return

    @app.exception_handler(ProblemError)
    async def _handle_problem_error(_: Request, exc: ProblemError) -> JSONResponse:
        return problem_response(exc.code, exc.detail, status_code=exc.status_code)

    @app.exception_handler(RequestValidationError)
    async def _handle_validation(_: Request, exc: RequestValidationError) -> JSONResponse:
        fields = ", ".join(
            ".".join(str(part) for part in error.get("loc", ())) for error in exc.errors()
        )
        return problem_response(
            ErrorCode.PARSE_FAILED,
            f"请求参数非法: {fields or '未知字段'}",
            status_code=422,
        )

    @app.exception_handler(StarletteHTTPException)
    async def _handle_http(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        detail = exc.detail if isinstance(exc.detail, str) else "请求被拒绝"
        return problem_response(
            _code_for_status(exc.status_code),
            detail,
            status_code=exc.status_code,
            headers=exc.headers,
        )

    @app.exception_handler(Exception)
    async def _handle_unexpected(_: Request, exc: Exception) -> JSONResponse:
        return problem_response(
            ErrorCode.CONN_UNAVAILABLE,
            f"服务内部错误: {type(exc).__name__}",
            status_code=500,
        )

    app.state._erp_error_handlers_installed = True
