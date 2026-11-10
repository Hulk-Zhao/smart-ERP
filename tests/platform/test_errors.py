"""C10 错误码：F1–F8 全覆盖、problem+json 形状稳定。"""

from __future__ import annotations

from apps.platform.contracts.errors import (
    ERROR_SPECS,
    ErrorCode,
    ProblemDetail,
    error_contract,
    problem,
)


def test_every_code_has_spec() -> None:
    assert set(ERROR_SPECS) == set(ErrorCode)


def test_fail_codes_covered() -> None:
    fail_codes = {spec.fail_code for spec in ERROR_SPECS.values() if spec.fail_code}
    assert fail_codes == {"F1", "F2", "F3", "F4", "F5", "F6", "F7", "F8"}


def test_w3_error_is_not_retriable() -> None:
    spec = ERROR_SPECS[ErrorCode.POLICY_W3]
    assert spec.retriable is False
    assert spec.http_status == 403


def test_problem_detail_shape() -> None:
    body = problem(ErrorCode.CONN_UNAVAILABLE, "草稿服务 502", trace_id="trace-1")
    assert isinstance(body, ProblemDetail)
    assert body.code is ErrorCode.CONN_UNAVAILABLE
    assert body.retriable is True
    assert body.fail_code == "F5"
    assert body.model_dump()["trace_id"] == "trace-1"


def test_error_contract_matches_enum() -> None:
    contract = error_contract()
    codes = {item["code"] for item in contract["errors"]}  # type: ignore[index]
    assert codes == {code.value for code in ErrorCode}
