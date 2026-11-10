"""角色闸与业务类别：把 PRD §2.3 的「能力 × 角色」收口到一处。

设计原则：
- **默认拒绝**：未列入白名单的角色一律拒绝，而不是「排除黑名单」；
- **类别与场景绑定**：复核/审批人必须持有与工单场景类别一致的角色
  （``finance`` / ``procurement`` / ``production``），越类即拒；
- **动作与角色解耦**：本模块只回答「能不能」，实际执行仍由 ``AccessGate`` 与
  ``WriteGateway`` 负责，避免出现第二套写入口。
"""

from __future__ import annotations

from collections.abc import Mapping
from uuid import UUID

from apps.security.identity import UserPrincipal

#: 可读工单 / 看影像 / 看审计链的角色（PRD §2.3「查看审计台账」：业务角色本部门、
#: IT/CIO、CFO、审计只读全量）。服务账号只用于执行，不在只读白名单内。
TICKET_READ_ROLES: frozenset[str] = frozenset(
    {"operator", "reviewer", "approver", "cfo", "auditor", "platform_admin"}
)

#: 可导出审计台账的角色（PRD §2.3：IT/CIO、CFO、审计只读）。
AUDIT_EXPORT_ROLES: frozenset[str] = frozenset({"platform_admin", "cfo", "auditor"})

#: 可回滚他人工单的角色（PRD §2.3：IT/CIO 需理由、CFO）。
OTHER_ROLLBACK_ROLES: frozenset[str] = frozenset({"platform_admin", "cfo"})

#: 三类业务归属对应的角色名；同一自然人可同时持有多个类别角色。
BUSINESS_CATEGORIES: frozenset[str] = frozenset(
    {"finance", "procurement", "production"}
)

#: 场景 → 业务类别。新增场景必须在此注册，否则复核/审批按未知类别拒绝。
DEFAULT_SCENE_CATEGORIES: dict[str, str] = {
    # A1 委外加工费对账（当前唯一落地的场景）。
    "ag2_processing_fee": "finance",
}


class RoleDenied(Exception):
    """角色闸拒绝。"""


class CrossCategoryReviewRejected(RoleDenied):
    """复核/审批人的业务类别与工单场景类别不一致。"""


class UnknownBusinessCategory(RoleDenied):
    """场景未登记业务类别，无法证明复核权限归属。"""


class OtherInitiatorRollbackDenied(RoleDenied):
    """非发起人且不具备「回滚他人」角色。"""


class RollbackReasonRequired(RoleDenied):
    """回滚他人工单必须填写理由。"""


class BusinessCategoryPolicy:
    """场景类别注册表 + 复核人类别校验。"""

    def __init__(self, mapping: Mapping[str, str] | None = None) -> None:
        self._mapping: dict[str, str] = dict(
            DEFAULT_SCENE_CATEGORIES if mapping is None else mapping
        )

    def register(self, scene_code: str, category: str) -> None:
        if category not in BUSINESS_CATEGORIES:
            raise ValueError(f"未知业务类别 {category!r}（可选 {sorted(BUSINESS_CATEGORIES)}）")
        self._mapping[scene_code] = category

    def category_for(self, scene_code: str) -> str | None:
        return self._mapping.get(scene_code)

    def assert_reviewer(
        self, *, scene_code: str | None, role: str, principal: UserPrincipal
    ) -> None:
        """复核/审批人必须持有与场景类别一致的角色；CFO 高金额审批跨类别。"""

        if scene_code is None:
            # 仅限内部旧调用；API 路径必须传 scene_code（由 service 层保证）。
            return
        if role not in {"reviewer", "approver"}:
            return
        category = self.category_for(scene_code)
        if category is None:
            raise UnknownBusinessCategory(
                f"场景 {scene_code!r} 未登记业务类别，无法判定复核权限归属"
            )
        if category not in principal.roles:
            raise CrossCategoryReviewRejected(
                f"角色 {sorted(principal.roles)} 不能复核 {category} 类场景 {scene_code!r}"
            )


def assert_can_read_ticket(principal: UserPrincipal) -> None:
    """读工单 / 看影像 / 看审计链：只读白名单。"""

    if not (principal.roles & TICKET_READ_ROLES):
        raise RoleDenied(
            f"角色 {sorted(principal.roles)} 无工单/审计只读权限（PRD §2.3）"
        )


def assert_can_export_audit(principal: UserPrincipal) -> None:
    """导出审计台账：仅 IT/CIO、CFO、审计只读。"""

    if not (principal.roles & AUDIT_EXPORT_ROLES):
        raise RoleDenied(
            f"角色 {sorted(principal.roles)} 无审计导出权限（PRD §2.3 仅 IT/CIO、CFO、审计只读）"
        )


def assert_can_rollback(
    principal: UserPrincipal,
    *,
    initiator_id: UUID,
    reason: str | None,
) -> bool:
    """判定能否回滚：返回 ``True`` 表示回滚的是他人发起的工单。

    - 自己发起的工单：任何持有 scope 的角色都可回滚（与 PRD 一致）；
    - 他人发起的工单：仅 IT/CIO（``platform_admin``）与 CFO，且理由必填；
    - 其余角色、理由缺失或空串：拒绝。
    """

    if principal.user_id == initiator_id:
        return False
    if not (principal.roles & OTHER_ROLLBACK_ROLES):
        raise OtherInitiatorRollbackDenied(
            "非发起人回滚他人工单仅限 IT/CIO 与 CFO（PRD §2.3）"
        )
    if not (reason or "").strip():
        raise RollbackReasonRequired("回滚他人工单必须填写理由并留审计")
    return True


__all__ = [
    "AUDIT_EXPORT_ROLES",
    "BUSINESS_CATEGORIES",
    "DEFAULT_SCENE_CATEGORIES",
    "OTHER_ROLLBACK_ROLES",
    "TICKET_READ_ROLES",
    "BusinessCategoryPolicy",
    "CrossCategoryReviewRejected",
    "OtherInitiatorRollbackDenied",
    "RoleDenied",
    "RollbackReasonRequired",
    "UnknownBusinessCategory",
    "assert_can_export_audit",
    "assert_can_read_ticket",
    "assert_can_rollback",
]
