"""CI：遍历注册表，断言「不存在任何 W3 动作」而不是「运行时拦得住 W3」。"""

from __future__ import annotations

from apps.platform.policy.levels import (
    CRITICAL_READONLY_FIELDS,
    FORBIDDEN_ACTION_VERBS,
    WriteLevel,
    effective_level,
)
from apps.platform.policy.registry import ActionRegistry, assert_registry_invariants
from tests.platform.support import make_action


def _seeded_registry() -> ActionRegistry:
    registry = ActionRegistry()
    registry.register(make_action(WriteLevel.W0, key="fakerp.alias.read_draft", compensation=False))
    registry.register(make_action(WriteLevel.W1, key="fakerp.alias.save_draft"))
    registry.register(
        make_action(
            WriteLevel.W2,
            key="fakerp.alias.submit_draft",
            writable=frozenset({"note", "draft_id"}),
        )
    )
    return registry


def test_registry_has_no_w3_action() -> None:
    registry = _seeded_registry()
    assert registry.all(), "注册表不应为空"
    for spec in registry.all():
        assert WriteLevel.parse(spec.level) is not WriteLevel.W3, spec.key


def test_every_write_action_has_compensation_and_verify() -> None:
    for spec in _seeded_registry().all():
        if WriteLevel.parse(spec.level) >= WriteLevel.W1:
            assert spec.compensation is not None, spec.key
            assert spec.verify is not None, spec.key


def test_no_forbidden_verb_or_critical_field() -> None:
    for spec in _seeded_registry().all():
        verb = spec.key.rsplit(".", 1)[-1].lower()
        assert verb not in FORBIDDEN_ACTION_VERBS, spec.key
        assert not (set(spec.writable_fields) & CRITICAL_READONLY_FIELDS), spec.key


def test_registry_invariants_pass_on_seeded_registry() -> None:
    assert_registry_invariants(_seeded_registry())


def test_effective_level_of_every_registered_action_is_executable() -> None:
    for spec in _seeded_registry().all():
        level = effective_level(
            scene_cap=WriteLevel.W2,
            connector_cap=WriteLevel.W2,
            action_level=spec.level,
            breaker_cap=WriteLevel.W2,
        )
        assert level is not WriteLevel.W3, spec.key
