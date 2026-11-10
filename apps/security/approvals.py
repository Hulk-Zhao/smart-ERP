"""审批阈值与双人复核（FR-1103 / FR-1403 / G-04）。

代码层先拒绝，DB 的 ``ck_approval_record_sod_self`` 等 CHECK 只是兜底
（见 ``apps/audit/MIGRATION_NEEDS.md``）：

- 发起人不能审批自己的工单；
- 同一人不得在同一工单担任两个审批角色（复核人 ≠ 审批人 ≠ CFO）；
- 平台管理员不得参与业务复核；
- 金额 > 10 万或触及应付 → 至少两个不同审批人；金额 > 50 万 → 必须有 CFO；
- 阈值可配置，但不得低于 G-04 默认下限。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID, uuid4

from apps.platform.contracts.errors import ErrorCode
from apps.platform.policy.levels import WriteLevel
from apps.security.identity import UserPrincipal

DEFAULT_DUAL_AMOUNT = Decimal(100000)
DEFAULT_CFO_AMOUNT = Decimal(500000)

ApprovalRole = Literal["reviewer", "approver", "cfo"]
APPROVAL_ROLES: tuple[str, ...] = ("reviewer", "approver", "cfo")


class ApprovalError(Exception):
    """审批链拒绝。"""

    code: ErrorCode = ErrorCode.POLICY_APPROVAL


class SelfApprovalRejected(ApprovalError):
    """发起人 = 审批人（FR-1403）。"""


class SoDViolation(ApprovalError):
    """职责分离冲突（平台管理员复核 / 同人多角色 / 角色不符）。"""


class DuplicateApprover(ApprovalError):
    """同一人已在该工单留下审批记录（复核人 ≠ 审批人）。"""


class MissingComment(ApprovalError):
    """批准/驳回必须填写意见（FR-107）。"""


class W3NotApprovable(ApprovalError):
    """W3 不存在审批通过路径。"""

    code = ErrorCode.POLICY_W3


class ThresholdBelowFloor(ApprovalError):
    """阈值低于 G-04 默认下限。"""

    code = ErrorCode.POLICY_LEVEL


@dataclass(frozen=True, slots=True)
class ApprovalThresholds:
    dual_amount: Decimal = DEFAULT_DUAL_AMOUNT
    cfo_amount: Decimal = DEFAULT_CFO_AMOUNT

    def __post_init__(self) -> None:
        if self.dual_amount < DEFAULT_DUAL_AMOUNT:
            raise ThresholdBelowFloor(
                f"双人阈值 {self.dual_amount} 低于 G-04 下限 {DEFAULT_DUAL_AMOUNT}"
            )
        if self.cfo_amount < DEFAULT_CFO_AMOUNT:
            raise ThresholdBelowFloor(
                f"CFO 阈值 {self.cfo_amount} 低于 G-04 下限 {DEFAULT_CFO_AMOUNT}"
            )
        if self.cfo_amount < self.dual_amount:
            raise ThresholdBelowFloor("CFO 阈值不得低于双人阈值")


@dataclass(frozen=True, slots=True)
class ApprovalRecord:
    id: str
    ticket_id: str
    role: str
    approver_id: UUID
    initiator_id: UUID
    approver_roles: tuple[str, ...]
    decision: Literal["approved", "rejected"]
    comment: str
    decided_at: datetime
    exception_reason: str | None = None

    def __post_init__(self) -> None:
        if self.role not in APPROVAL_ROLES:
            raise ApprovalError(f"未知审批角色 {self.role!r}")
        if self.decision not in ("approved", "rejected"):
            raise ApprovalError(f"未知审批决定 {self.decision!r}")
        if not self.comment.strip():
            raise MissingComment("审批意见不得为空（FR-107）")
        if self.approver_id == self.initiator_id:
            raise SelfApprovalRejected("发起人不得审批自己的工单（FR-1403）")
        if "platform_admin" in self.approver_roles:
            raise SoDViolation("平台管理员不得参与业务复核（FR-1403）")


class ApprovalService:
    """审批记录的唯一写入口；判定口径与写入口同一处，避免 UI 与执行器分裂。"""

    def __init__(
        self,
        *,
        thresholds: ApprovalThresholds | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._thresholds = thresholds or ApprovalThresholds()
        self._clock = clock or (lambda: datetime.now(UTC))
        self._records: dict[str, list[ApprovalRecord]] = {}

    @property
    def thresholds(self) -> ApprovalThresholds:
        return self._thresholds

    def required_roles(
        self,
        *,
        level: WriteLevel | int | str,
        amount: Decimal | int | str,
        touches_fund: bool,
        force_dual: bool = False,
    ) -> tuple[str, ...]:
        parsed = WriteLevel.parse(level)
        if parsed is WriteLevel.W3:
            raise W3NotApprovable("W3 不可审批通过（FR-1407）")
        if parsed is WriteLevel.W0:
            return ()

        money = _money(amount)
        roles: list[str] = ["reviewer"] if parsed is WriteLevel.W1 else ["approver"]
        if money > self._thresholds.dual_amount or touches_fund:
            if "approver" not in roles:
                roles.append("approver")
            if len(roles) < 2:
                roles.insert(0, "reviewer")
        if money > self._thresholds.cfo_amount and "cfo" not in roles:
            roles.append("cfo")
        if force_dual and len(roles) < 2:
            for candidate in ("reviewer", "approver"):
                if candidate not in roles:
                    roles.append(candidate)
                    break
        return tuple(roles)

    def record(
        self,
        *,
        ticket_id: str,
        role: str,
        approver: UserPrincipal,
        initiator_id: UUID,
        decision: Literal["approved", "rejected"],
        comment: str,
        decided_at: datetime | None = None,
        exception_reason: str | None = None,
    ) -> ApprovalRecord:
        if not ticket_id.strip():
            raise ApprovalError("ticket_id 不得为空")
        if approver.user_id == initiator_id:
            raise SelfApprovalRejected("发起人不得审批自己的工单（FR-1403）")
        if "platform_admin" in approver.roles:
            raise SoDViolation("平台管理员不得参与业务复核（FR-1403）")
        self._assert_role_capability(role, approver.roles)

        existing = self._records.get(ticket_id, [])
        if any(record.approver_id == approver.user_id for record in existing):
            raise DuplicateApprover(
                f"{approver.user_id} 已在工单 {ticket_id} 留下审批记录（复核人 ≠ 审批人）"
            )

        record = ApprovalRecord(
            id=str(uuid4()),
            ticket_id=ticket_id,
            role=role,
            approver_id=approver.user_id,
            initiator_id=initiator_id,
            approver_roles=tuple(sorted(approver.roles)),
            decision=decision,
            comment=comment,
            decided_at=_utc(decided_at or self._clock()),
            exception_reason=exception_reason,
        )
        self._records.setdefault(ticket_id, []).append(record)
        return record

    def records(self, ticket_id: str) -> tuple[ApprovalRecord, ...]:
        return tuple(self._records.get(ticket_id, ()))

    def satisfied(
        self,
        *,
        ticket_id: str,
        level: WriteLevel | int | str,
        amount: Decimal | int | str,
        touches_fund: bool,
        initiator_id: UUID,
        force_dual: bool = False,
    ) -> bool:
        required = self.required_roles(
            level=level, amount=amount, touches_fund=touches_fund, force_dual=force_dual
        )
        if not required:
            return True
        approved = [
            record
            for record in self._records.get(ticket_id, [])
            if record.decision == "approved" and record.initiator_id == initiator_id
        ]
        approved_roles = {record.role for record in approved}
        approvers = {record.approver_id for record in approved}
        return (
            all(role in approved_roles for role in required)
            and len(approvers) >= len(required)
        )

    def missing_roles(
        self,
        *,
        ticket_id: str,
        level: WriteLevel | int | str,
        amount: Decimal | int | str,
        touches_fund: bool,
        initiator_id: UUID,
        force_dual: bool = False,
    ) -> tuple[str, ...]:
        required = self.required_roles(
            level=level, amount=amount, touches_fund=touches_fund, force_dual=force_dual
        )
        approved_roles = {
            record.role
            for record in self._records.get(ticket_id, ())
            if record.decision == "approved" and record.initiator_id == initiator_id
        }
        return tuple(role for role in required if role not in approved_roles)

    @staticmethod
    def _assert_role_capability(role: str, roles: frozenset[str]) -> None:
        if role == "reviewer" and "reviewer" not in roles:
            raise SoDViolation("缺少 reviewer 角色")
        if role == "approver" and not ({"approver", "cfo"} & roles):
            raise SoDViolation("缺少 approver 角色")
        if role == "cfo" and "cfo" not in roles:
            raise SoDViolation("缺少 cfo 角色")


@dataclass(frozen=True, slots=True)
class TicketFacts:
    amount: Decimal
    touches_fund: bool
    initiator_id: UUID


class ApprovalGateAdapter:
    """把 ``ApprovalService`` 接到 ``WriteGateway(approvals=...)`` 的结构化协议上。"""

    def __init__(
        self,
        service: ApprovalService,
        facts: Mapping[str, TicketFacts] | Callable[[str], TicketFacts | None],
    ) -> None:
        self._service = service
        self._facts = facts

    def is_satisfied(self, ticket_id: str, level: WriteLevel) -> bool:
        facts = self._facts(ticket_id) if callable(self._facts) else self._facts.get(ticket_id)
        if facts is None:
            return False  # 无工单事实 → 拒绝，而不是默认放行
        return self._service.satisfied(
            ticket_id=ticket_id,
            level=level,
            amount=facts.amount,
            touches_fund=facts.touches_fund,
            initiator_id=facts.initiator_id,
        )


def _money(value: Decimal | int | str) -> Decimal:
    if isinstance(value, bool):
        raise ApprovalError("金额不得为 bool")
    money = Decimal(str(value))
    if money < 0:
        raise ApprovalError("金额不得为负")
    return money


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(
        UTC
    )


__all__ = [
    "APPROVAL_ROLES",
    "ApprovalError",
    "ApprovalGateAdapter",
    "ApprovalRecord",
    "ApprovalService",
    "ApprovalThresholds",
    "DuplicateApprover",
    "MissingComment",
    "SelfApprovalRejected",
    "SoDViolation",
    "ThresholdBelowFloor",
    "TicketFacts",
    "W3NotApprovable",
]
