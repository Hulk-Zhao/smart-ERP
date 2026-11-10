"""ActionRegistry：高危动作进不了系统（§3.2.3）。

注册期断言（任一不满足即拒绝注册，不存在「先注册再拦截」）：
- ``level == W3`` → 拒绝（FR-1407）；
- ``level >= W1`` 且无补偿 → 拒绝（FR-1102）；
- 无 ``verify`` → 拒绝（§3.1.4 崩溃安全写入的前提）；
- ``writable_fields`` 命中 ``CRITICAL_READONLY_FIELDS`` → 拒绝（G-09）；
- 动作名命中禁止动词表 → 拒绝（FR-1407）。

CI 另有一条遍历注册表的测试（``tests/platform/test_registry_scan.py``），
断言「不存在任何 W3 动作」而不是「运行时拦得住 W3」。
"""

from __future__ import annotations

from typing import Iterator

from apps.platform.contracts.errors import ErrorCode
from apps.platform.policy.levels import (
    CRITICAL_READONLY_FIELDS,
    FORBIDDEN_ACTION_VERBS,
    ActionSpec,
    WriteLevel,
)


class RegistrationRejected(Exception):
    """动作未通过注册期断言。"""

    code: ErrorCode = ErrorCode.POLICY_LEVEL


class ActionRegistry:
    def __init__(self) -> None:
        self._specs: dict[str, ActionSpec] = {}

    def register(self, spec: ActionSpec) -> None:
        validate_action_spec(spec)
        if spec.key in self._specs:
            raise RegistrationRejected(f"动作 {spec.key} 重复注册")
        self._specs[spec.key] = spec

    def get(self, key: str) -> ActionSpec:
        try:
            return self._specs[key]
        except KeyError as exc:
            raise KeyError(f"动作 {key} 未注册") from exc

    def all(self) -> tuple[ActionSpec, ...]:
        return tuple(self._specs.values())

    def __iter__(self) -> Iterator[ActionSpec]:
        return iter(self._specs.values())

    def __len__(self) -> int:
        return len(self._specs)


def validate_action_spec(spec: ActionSpec) -> None:
    parts = spec.key.split(".")
    if len(parts) != 3 or not all(part.strip() for part in parts):
        raise RegistrationRejected(
            f"动作 key 必须是 '{{connector}}.{{alias}}.{{action}}'，实际为 {spec.key!r}"
        )

    verb = parts[-1].lower()
    if verb in FORBIDDEN_ACTION_VERBS:
        raise RegistrationRejected(f"动作名 {verb} 在禁止动词表内（FR-1407）")

    level = WriteLevel.parse(spec.level)
    if level is WriteLevel.W3:
        raise RegistrationRejected(f"W3 动作 {spec.key} 拒绝注册（无执行路径）")

    if level >= WriteLevel.W1:
        if spec.compensation is None:
            raise RegistrationRejected(f"{spec.key} 等级 {level.name} 但未声明补偿（FR-1102）")
        if not spec.writable_fields:
            raise RegistrationRejected(f"{spec.key} 等级 {level.name} 但未声明可写字段")

    if spec.verify is None:
        raise RegistrationRejected(f"{spec.key} 未声明 verify，无法做崩溃安全的写入判定")

    readonly_hits = set(spec.writable_fields) & CRITICAL_READONLY_FIELDS
    if readonly_hits:
        raise RegistrationRejected(
            f"{spec.key} 试图写入只读关键字段 {sorted(readonly_hits)}（G-09）"
        )

    overlap = set(spec.writable_fields) & set(spec.forbidden_fields)
    if overlap:
        raise RegistrationRejected(f"{spec.key} 的只读字段同时被声明为可写: {sorted(overlap)}")


def assert_registry_invariants(registry: ActionRegistry) -> None:
    """遍历注册表的全量校验（CI 用）：任一违规直接失败。"""

    seen: set[str] = set()
    for spec in registry.all():
        validate_action_spec(spec)
        if spec.key in seen:
            raise RegistrationRejected(f"注册表内动作 key 重复: {spec.key}")
        seen.add(spec.key)
        if WriteLevel.parse(spec.level) is WriteLevel.W3:
            raise RegistrationRejected(f"注册表内存在 W3 动作: {spec.key}")
