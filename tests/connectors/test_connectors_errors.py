"""错误分类：状态码 / 异常 → 三类收敛口径 + F 码。"""

from __future__ import annotations

import httpx
import pytest

from apps.connectors.errors import (
    ConnectorError,
    ErrorClass,
    ErrorKind,
    classify_exception,
    classify_status_code,
)


@pytest.mark.parametrize(
    ("status", "kind", "error_class", "fail_code", "retryable"),
    [
        (429, ErrorKind.RATE_LIMITED, ErrorClass.TRANSIENT, "F5", True),
        (401, ErrorKind.AUTH, ErrorClass.PERMANENT, "F5", False),
        (403, ErrorKind.FORBIDDEN, ErrorClass.PERMANENT, "F5", False),
        (404, ErrorKind.NOT_FOUND, ErrorClass.PERMANENT, "F1", False),
        (409, ErrorKind.CONFLICT, ErrorClass.PERMANENT, "F7", False),
        (422, ErrorKind.INVALID, ErrorClass.PERMANENT, "F5", False),
        (503, ErrorKind.TRANSIENT, ErrorClass.TRANSIENT, "F5", True),
    ],
)
def test_status_code_mapping(status, kind, error_class, fail_code, retryable) -> None:
    assert classify_status_code(status) is kind
    err = ConnectorError(classify_status_code(status), "x", status=status)
    assert err.error_class is error_class
    assert err.fail_code == fail_code
    assert err.retryable is retryable


def test_connect_error_is_transient() -> None:
    err = classify_exception(httpx.ConnectError("connection refused"), phase="write")
    assert err.kind is ErrorKind.TRANSIENT
    assert err.retryable is True


def test_read_timeout_vs_write_timeout() -> None:
    read_err = classify_exception(httpx.ReadTimeout("slow"), phase="read")
    write_err = classify_exception(httpx.ReadTimeout("slow"), phase="write")

    assert read_err.kind is ErrorKind.TIMEOUT
    assert read_err.retryable is True
    # 写在发出去之后超时 → 结果未知，禁止重试
    assert write_err.kind is ErrorKind.UNKNOWN_OUTCOME
    assert write_err.error_class is ErrorClass.UNKNOWN
    assert write_err.retryable is False


def test_decode_error_means_schema_drift() -> None:
    err = classify_exception(httpx.DecodingError("bad gzip"), phase="read")
    assert err.kind is ErrorKind.SCHEMA_CHANGED
    assert err.retryable is False


def test_connector_error_passes_through_unchanged() -> None:
    original = ConnectorError(ErrorKind.CONFLICT, "版本冲突")
    assert classify_exception(original) is original
