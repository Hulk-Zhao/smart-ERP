"""注册期断言：高危动作进不了系统。"""

from __future__ import annotations

import pytest

from apps.platform.policy.levels import ActionSpec, WriteLevel
from apps.platform.policy.registry import ActionRegistry, RegistrationRejected
from tests.platform.support import make_action


def _reject(spec: ActionSpec) -> None:
    registry = ActionRegistry()
    with pytest.raises(RegistrationRejected):
        registry.register(spec)
    assert len(registry) == 0


def test_w3_action_rejected() -> None:
    _reject(make_action(WriteLevel.W3, key="fakerp.alias.pay_invoice"))


def test_w1_without_compensation_rejected() -> None:
    _reject(make_action(WriteLevel.W1, compensation=False))


def test_action_without_verify_rejected() -> None:
    _reject(make_action(WriteLevel.W0, verify=False))


def test_critical_readonly_field_rejected() -> None:
    _reject(make_action(writable=frozenset({"note", "stock_qty"})))


def test_forbidden_verb_rejected() -> None:
    _reject(make_action(key="fakerp.alias.post_voucher"))


def test_bad_key_shape_rejected() -> None:
    _reject(make_action(key="save_draft"))


def test_duplicate_rejected() -> None:
    registry = ActionRegistry()
    registry.register(make_action())
    with pytest.raises(RegistrationRejected):
        registry.register(make_action())


def test_writable_and_forbidden_overlap_rejected() -> None:
    _reject(make_action(writable=frozenset({"note"}), forbidden=frozenset({"note"})))


def test_legal_action_registers() -> None:
    registry = ActionRegistry()
    action = make_action()
    registry.register(action)
    assert registry.get(action.key) is action
    assert list(registry) == [action]
