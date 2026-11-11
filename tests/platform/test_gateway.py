"""WriteGateway：唯一写出口、W3 无执行分支、配置只能降。"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Mapping
from typing import Any

import pytest

from apps.platform.api.errors import ProblemError
from apps.platform.contracts.errors import ErrorCode
from apps.platform.policy import gateway as gateway_module
from apps.platform.policy.gateway import (
    ApprovalRequiredError,
    FieldScopeViolation,
    ForbiddenWriteError,
    PreviewFailedError,
    VerifyMismatchError,
    WriteGateway,
)
from apps.platform.policy.levels import ActionSpec, SceneSpec, WriteLevel
from observability.metrics import MetricsRegistry
from tests.platform.support import AllowAllApprovals, FakeConnector, make_action

SCENE = SceneSpec(code="AG1", max_level=WriteLevel.W2)
TICKET = "AT-20261110-0001"
IDEM = "idem-0001"


def _run(gateway: WriteGateway, action: ActionSpec, payload: Mapping[str, Any], **kwargs: Any):
    return asyncio.run(
        gateway.execute(
            action,
            payload,
            ticket_id=TICKET,
            idem_key=IDEM,
            scene=kwargs.pop("scene", SCENE),
        )
    )


def test_w3_has_no_execution_branch() -> None:
    connector = FakeConnector(preview_before={"draft": "D-001"})
    gateway = WriteGateway(connector, approvals=AllowAllApprovals())
    action = ActionSpec(
        key="fakerp.alias.pay_invoice",
        level=WriteLevel.W3,
        writable_fields=frozenset({"note"}),
        compensation=make_action().compensation,
        verify=make_action().verify,
    )

    with pytest.raises(ForbiddenWriteError):
        _run(gateway, action, {"note": "x"})
    assert connector.calls == [], "W3 不得产生任何连接器调用"


def test_w1_requires_approval() -> None:
    connector = FakeConnector(preview_before={"draft": "D-001"})
    gateway = WriteGateway(connector)  # 默认 DenyAllApprovals

    with pytest.raises(ApprovalRequiredError):
        _run(gateway, make_action(WriteLevel.W1), {"note": "x"})
    assert connector.calls == []


def test_approved_write_runs_preview_write_verify_in_order() -> None:
    connector = FakeConnector(preview_before={"draft": "D-001"})
    gateway = WriteGateway(connector, approvals=AllowAllApprovals())

    result = _run(gateway, make_action(WriteLevel.W1), {"note": "x"})

    assert connector.calls == ["preview", "write", "verify"]
    assert result.level is WriteLevel.W1
    assert result.verify.matches()


def test_connector_cap_lowers_level_and_skips_approval() -> None:
    connector = FakeConnector(preview_before={"draft": "D-001"})
    gateway = WriteGateway(
        connector, connector_cap=lambda action_key: WriteLevel.W0
    )

    result = _run(gateway, make_action(WriteLevel.W2), {"note": "x"})

    assert result.level is WriteLevel.W0
    assert connector.calls == ["preview", "write", "verify"]


def test_scene_cap_lowers_level() -> None:
    connector = FakeConnector(preview_before={"draft": "D-001"})
    gateway = WriteGateway(connector)
    scene = SceneSpec(code="AG1", max_level=WriteLevel.W0)

    result = _run(gateway, make_action(WriteLevel.W1), {"note": "x"}, scene=scene)

    assert result.level is WriteLevel.W0


def test_breaker_readonly_blocks_writes() -> None:
    connector = FakeConnector(preview_before={"draft": "D-001"})
    gateway = WriteGateway(
        connector,
        breaker_cap=lambda scene_code: WriteLevel.W0,
        approvals=AllowAllApprovals(),
    )

    result = _run(gateway, make_action(WriteLevel.W1), {"note": "x"})
    assert result.level is WriteLevel.W0


def test_field_outside_scope_rejected_before_any_call() -> None:
    connector = FakeConnector(preview_before={"draft": "D-001"})
    gateway = WriteGateway(connector, approvals=AllowAllApprovals())

    with pytest.raises(FieldScopeViolation):
        _run(gateway, make_action(WriteLevel.W1), {"note": "x", "gl_account": "1001"})
    assert connector.calls == []


def test_declared_forbidden_field_rejected() -> None:
    connector = FakeConnector(preview_before={"draft": "D-001"})
    gateway = WriteGateway(connector, approvals=AllowAllApprovals())
    action = make_action(
        WriteLevel.W1, writable=frozenset({"note", "draft_id"}), forbidden=frozenset({"draft_id"})
    )

    with pytest.raises(FieldScopeViolation):
        _run(gateway, action, {"draft_id": "D-001"})
    assert connector.calls == []


def test_preview_without_snapshot_means_zero_write() -> None:
    connector = FakeConnector(preview_before=None)
    gateway = WriteGateway(connector, approvals=AllowAllApprovals())

    with pytest.raises(PreviewFailedError):
        _run(gateway, make_action(WriteLevel.W1), {"note": "x"})
    assert connector.calls == ["preview"]


def test_verify_mismatch_raises_f6() -> None:
    connector = FakeConnector(preview_before={"draft": "D-001"})
    connector.verify_status = "mismatch"
    gateway = WriteGateway(connector, approvals=AllowAllApprovals())

    with pytest.raises(VerifyMismatchError):
        _run(gateway, make_action(WriteLevel.W1), {"note": "x"})


def test_no_bypass_switch_in_gateway_api() -> None:
    forbidden_names = {"bypass", "force", "allow_w3", "skip_approval", "dry_run_override"}
    execute_params = set(inspect.signature(WriteGateway.execute).parameters)
    init_params = set(inspect.signature(WriteGateway.__init__).parameters)
    assert not (execute_params & forbidden_names)
    assert not (init_params & forbidden_names)
    assert not hasattr(gateway_module, "ALLOW_W3")


def test_successful_write_counts_stage_total_only() -> None:
    """① 成功写入 → preview/write/verify 各 +1，失败计数保持 0。"""

    registry = MetricsRegistry()
    connector = FakeConnector(preview_before={"draft": "D-001"})
    gateway = WriteGateway(connector, approvals=AllowAllApprovals(), metrics=registry)

    result = _run(gateway, make_action(WriteLevel.W1), {"note": "x"})

    assert result.level is WriteLevel.W1
    for stage in ("preview", "write", "verify"):
        assert registry.write_stage_total.value(stage=stage) == 1.0, stage
        assert registry.write_stage_failures_total.value(stage=stage) == 0.0, stage


def test_problem_error_counts_failure_and_propagates_unchanged() -> None:
    """② 写阶段抛 ProblemError → 失败 +1，且调用方拿到的异常原样不变。"""

    registry = MetricsRegistry()
    connector = FakeConnector(preview_before={"draft": "D-001"})
    boom = ProblemError(ErrorCode.CONN_UNAVAILABLE, "连接器挂了")

    async def explode(*_args: Any, **_kwargs: Any) -> Any:
        connector.calls.append("write")
        raise boom

    connector.write = explode  # type: ignore[method-assign]
    gateway = WriteGateway(connector, approvals=AllowAllApprovals(), metrics=registry)

    with pytest.raises(ProblemError) as caught:
        _run(gateway, make_action(WriteLevel.W1), {"note": "x"})

    # 同一个异常实例被重新抛出，错误码/详情/HTTP 状态均与埋点前一致
    assert caught.value is boom
    assert caught.value.code is ErrorCode.CONN_UNAVAILABLE
    assert caught.value.detail == "连接器挂了"
    assert caught.value.status_code == boom.status_code
    assert registry.write_stage_total.value(stage="write") == 1.0
    assert registry.write_stage_failures_total.value(stage="write") == 1.0
    assert registry.write_stage_failures_total.value(stage="preview") == 0.0
    assert registry.write_stage_failures_total.value(stage="verify") == 0.0
    assert connector.calls == ["preview", "write"], "写入失败不得继续进入 verify"


def test_broken_metrics_do_not_affect_write_path() -> None:
    """观测故障必须被兜住：指标写入抛错时，成功写入仍返回完整结构。"""

    class BrokenMetrics:
        def record_stage(self, *_args: Any, **_kwargs: Any) -> None:
            raise RuntimeError("metrics 坏了")

    connector = FakeConnector(preview_before={"draft": "D-001"})
    gateway = WriteGateway(
        connector,
        approvals=AllowAllApprovals(),
        metrics=BrokenMetrics(),  # type: ignore[arg-type]
    )

    result = _run(gateway, make_action(WriteLevel.W1), {"note": "x"})

    assert connector.calls == ["preview", "write", "verify"]
    assert result.write.external_id == "D-001"
    assert result.verify.matches()
