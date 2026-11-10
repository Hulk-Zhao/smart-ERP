"""四路取严与等级语义。"""

from __future__ import annotations

import pytest

from apps.platform.policy.levels import WriteLevel, effective_level, is_executable


def test_min_across_four_sources() -> None:
    level = effective_level(
        scene_cap=WriteLevel.W2,
        connector_cap=WriteLevel.W1,
        action_level=WriteLevel.W2,
        breaker_cap=WriteLevel.W0,
    )
    assert level is WriteLevel.W0


def test_config_cannot_raise_action_level() -> None:
    # 连接器配置给到 W2，但动作本身是 W0 → 仍是 W0
    level = effective_level(
        scene_cap="W2", connector_cap="W2", action_level="W0", breaker_cap="W2"
    )
    assert level is WriteLevel.W0


def test_scene_cap_cannot_be_exceeded_by_action() -> None:
    level = effective_level(
        scene_cap=WriteLevel.W0,
        connector_cap=WriteLevel.W2,
        action_level=WriteLevel.W2,
        breaker_cap=WriteLevel.W2,
    )
    assert level is WriteLevel.W0


def test_w3_is_sticky() -> None:
    level = effective_level(
        scene_cap=WriteLevel.W3,
        connector_cap=WriteLevel.W2,
        action_level=WriteLevel.W2,
        breaker_cap=WriteLevel.W2,
    )
    assert level is WriteLevel.W3
    assert is_executable(level) is False


def test_parse_accepts_ints_and_labels() -> None:
    assert WriteLevel.parse(1) is WriteLevel.W1
    assert WriteLevel.parse("w2") is WriteLevel.W2
    with pytest.raises(ValueError):
        WriteLevel.parse("W9")


def test_w3_is_not_executable() -> None:
    assert is_executable("W0") is True
    assert is_executable("W2") is True
    assert is_executable("W3") is False
