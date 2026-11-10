"""通用执行壳的入参契约与写前闸门（不触库）。"""

from __future__ import annotations

import pytest

from apps.platform.contracts.errors import ErrorCode
from apps.platform.policy.gateway import BreakerState
from apps.platform.policy.levels import WriteLevel

from apps.agents.breaker import BreakerPolicy, CircuitBreaker
from apps.agents.runtime import PlannedStep
from apps.agents.scenes import ALL_SCENE_CODES, scene_of
from apps.agents.shell import (
    AgentInputRejected,
    AgentShell,
    BreakerOpenError,
    LowConfidenceError,
    SceneLevelExceeded,
    validate_input,
)
from support import make_action


def _base_input(**overrides: object) -> dict[str, object]:
    data: dict[str, object] = {
        "scene_code": "AG-1",
        "scope": {"org_id": "org-1", "period": "2026-10", "system_ids": ["erp"]},
        "trigger_kind": "timer",
        "business_key": "2026-10/SUP-1",
        "payload": {},
    }
    data.update(overrides)
    return data


def _shell(breaker: CircuitBreaker | None = None) -> AgentShell:
    return AgentShell(
        scene_of("AG-1"),
        runtime=None,  # type: ignore[arg-type]
        gateway=None,  # type: ignore[arg-type]
        breaker=breaker or CircuitBreaker(BreakerPolicy()),
    )


def _step(key: str = "fake.draft.create", level: WriteLevel = WriteLevel.W1) -> PlannedStep:
    return PlannedStep(
        action=make_action(key, level=level),
        target_system="fake",
        payload={"supplier": "SUP-1", "amount": "10.00"},
        idem_key=f"idem-{key}",
    )


def test_all_eight_scenes_registered() -> None:
    assert ALL_SCENE_CODES == tuple(f"AG-{index}" for index in range(1, 9))
    assert scene_of("AG-5").scene.max_level is WriteLevel.W0
    assert scene_of("AG-4").scene.max_level is WriteLevel.W2


def test_scope_is_required() -> None:
    raw = _base_input()
    raw.pop("scope")
    with pytest.raises(AgentInputRejected) as excinfo:
        validate_input(raw, scene_of("AG-1"))
    assert excinfo.value.code is ErrorCode.AUTHZ_SCOPE


def test_scope_requires_period_and_systems() -> None:
    with pytest.raises(AgentInputRejected) as excinfo:
        validate_input(
            _base_input(scope={"org_id": "org-1", "period": "2026-13", "system_ids": ["erp"]}),
            scene_of("AG-1"),
        )
    assert excinfo.value.code is ErrorCode.AUTHZ_SCOPE


def test_money_must_be_object_not_bare_number() -> None:
    with pytest.raises(AgentInputRejected) as excinfo:
        validate_input(_base_input(money={"diff_amount": 100}), scene_of("AG-1"))
    assert excinfo.value.code is ErrorCode.AUTHZ_FIELD

    with pytest.raises(AgentInputRejected):
        validate_input(_base_input(payload={"diff_amount": 100}), scene_of("AG-1"))

    request = validate_input(
        _base_input(
            money={
                "diff_amount": {"amount": "100.0000", "currency": "CNY", "tax_included": True}
            }
        ),
        scene_of("AG-1"),
    )
    assert request.money["diff_amount"].value == 100


def test_confidence_below_070_is_unusable() -> None:
    with pytest.raises(LowConfidenceError) as excinfo:
        validate_input(_base_input(confidence=0.69), scene_of("AG-1"))
    assert excinfo.value.code is ErrorCode.DATA_AMBIGUOUS
    validate_input(_base_input(confidence=0.70), scene_of("AG-1"))


def test_w3_action_is_rejected_before_any_call() -> None:
    # 计划层就拒绝：W3 进不了执行计划（壳子内部还有一层兜底分支）
    with pytest.raises(ValueError, match="W3"):
        _step(level=WriteLevel.W3)


def test_action_above_scene_cap_is_rejected() -> None:
    with pytest.raises(SceneLevelExceeded):
        _shell().assert_writable([_step(level=WriteLevel.W2)])


def test_breaker_open_blocks_every_write() -> None:
    breaker = CircuitBreaker(BreakerPolicy(min_samples=5))
    for _ in range(5):
        breaker.record("AG-1", ok=True)
    breaker.record("AG-1", ok=False, fail_code="F5")
    breaker.record("AG-1", ok=False, fail_code="F5")
    assert breaker.state("AG-1") == BreakerState.W0_ONLY
    shell = _shell(breaker)
    with pytest.raises(BreakerOpenError):
        shell.assert_writable([_step()])
    assert shell.breaker_cap("AG-1") is WriteLevel.W0
