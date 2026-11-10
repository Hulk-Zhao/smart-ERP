"""数据范围（G-05）：``scope{org_id, period, system_ids}`` 必填且只能收窄。

Agent 拿到的范围必须来自会话授权范围，任何「多传一个系统 / 多一个期间」都在
进入业务查询之前被拒绝，而不是靠 SQL 里记得加 where。
"""

from __future__ import annotations

from dataclasses import dataclass

from apps.platform.contracts.errors import ErrorCode


class ScopeViolation(Exception):
    """请求范围超出授权范围（E-AUTHZ-001）。"""

    code: ErrorCode = ErrorCode.AUTHZ_SCOPE


@dataclass(frozen=True, slots=True)
class Scope:
    """一次任务的数据范围；三个字段缺一不可（G-05）。"""

    org_id: str
    period: str
    system_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.org_id.strip():
            raise ScopeViolation("scope.org_id 不得为空（G-05）")
        if not self.period.strip():
            raise ScopeViolation("scope.period 不得为空（G-05）")
        if not self.system_ids:
            raise ScopeViolation("scope.system_ids 不得为空（G-05）")
        if any(not system.strip() for system in self.system_ids):
            raise ScopeViolation("scope.system_ids 含空值")
        if len(set(self.system_ids)) != len(self.system_ids):
            raise ScopeViolation("scope.system_ids 含重复项")

    def as_dict(self) -> dict[str, object]:
        return {
            "org_id": self.org_id,
            "period": self.period,
            "system_ids": list(self.system_ids),
        }


@dataclass(frozen=True, slots=True)
class ScopeGrant:
    """会话/角色被授予的范围集合；Agent 请求范围必须是它的子集。"""

    org_ids: frozenset[str]
    periods: frozenset[str]
    system_ids: frozenset[str]

    def __post_init__(self) -> None:
        if not self.org_ids or not self.periods or not self.system_ids:
            raise ScopeViolation("授权范围不得为空集合（否则等于无权限）")
        if any(not item.strip() for item in self.org_ids | self.periods | self.system_ids):
            raise ScopeViolation("授权范围不得含空字符串")

    def covers(self, requested: Scope) -> bool:
        return (
            requested.org_id in self.org_ids
            and requested.period in self.periods
            and set(requested.system_ids) <= set(self.system_ids)
        )

    def covers_system(self, system: str) -> bool:
        return system in self.system_ids


class ScopeGuard:
    """服务层闸口：把「只能拿被授予的范围」写成可单测的断言。"""

    def __init__(self, grant: ScopeGrant) -> None:
        self._grant = grant

    @property
    def grant(self) -> ScopeGrant:
        return self._grant

    def authorize(self, requested: Scope) -> Scope:
        if not self._grant.covers(requested):
            raise ScopeViolation(
                "请求范围超出授权: "
                f"org={requested.org_id!r} period={requested.period!r} "
                f"systems={sorted(requested.system_ids)!r}"
            )
        return requested

    def authorize_system(self, system: str) -> str:
        if not self._grant.covers_system(system):
            raise ScopeViolation(f"目标系统 {system!r} 不在授权 system_ids 内")
        return system


def merge_grants(left: ScopeGrant, right: ScopeGrant) -> ScopeGrant:
    """多角色范围重叠时取并集（用户可见范围 = 各角色授予范围之和）。"""

    return ScopeGrant(
        org_ids=left.org_ids | right.org_ids,
        periods=left.periods | right.periods,
        system_ids=left.system_ids | right.system_ids,
    )


def intersect_grants(left: ScopeGrant, right: ScopeGrant) -> ScopeGrant:
    """同一请求跨来源时取交集；交集为空则视为无权限。"""

    grant = ScopeGrant(
        org_ids=left.org_ids & right.org_ids,
        periods=left.periods & right.periods,
        system_ids=left.system_ids & right.system_ids,
    )
    return grant
