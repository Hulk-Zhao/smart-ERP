"""产品 API 的用例编排：四组路由共用同一套「先过闸、再写、再记账」。

边界（与平台层一致，不自造第二套）：

- **写**：``AccessGate.authorize`` 必过（四道闸）；真正的外部写入只能经
  ``WriteGateway.execute`` / ``RollbackService.execute``，本模块不直接碰连接器。
- **读**：``ScopeGuard`` 先校验 scope，再按 scope 过滤结果，不像 SQL 那样靠自觉。
- **错误**：领域异常在此层抛出，由路由边界的 ``translated()`` 收敛成 problem+json；
  ``trace_id`` 一律由平台中间件注入。
- **幂等**：写接口按 ``Idempotency-Key`` + 请求体指纹做重放，重放命中不再执行。
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime
from decimal import Decimal
from enum import Enum
from typing import Any
from uuid import UUID, uuid4

from apps.agents.state_machine import (
    COMPENSATE_FAIL_CODES,
    TransitionContext,
    resolve_transition,
)
from apps.api.container import ApiContainer
from apps.api.metering import (
    MeteringConfigError,
    MeteringPolicy,
    build_statement,
    classify_billing_bucket,
    statement_csv,
)
from apps.api.pagination import offset_cursor, page_bounds, read_offset
from apps.api.recon_snapshot import BatchSnapshot, snapshot_from_batch
from apps.api.recon_store import ReconConflict, ReconStore, SqlReconStore
from apps.api.schemas import (
    ActionIn,
    ActionOut,
    ApprovalDecisionIn,
    ApprovalDecisionOut,
    ApprovalOut,
    ApprovalTaskOut,
    AuditChainOut,
    AuditImageOut,
    BillingBucket,
    DiscrepancyDraftOut,
    EvidenceOut,
    MeteringStatementOut,
    MissingItemOut,
    MoneyOut,
    ReconBatchOut,
    ReconDatasetIn,
    ReconDiffDetailOut,
    ReconDiffOut,
    ReconLineResultOut,
    ReconMetricsOut,
    ReconRunIn,
    ScopeIn,
    TicketDetailOut,
    TicketOut,
    TicketStepOut,
    UnattributedOut,
)
from apps.api.store import (
    PendingWrite,
    ReconBatchRecord,
    TicketRecord,
    TicketStepRecord,
)
from apps.audit.rollback import (
    RollbackForbidden,
    RollbackKind,
    RollbackPlan,
    RollbackPlanner,
    RollbackService,
    StepWrite,
)
from apps.audit.store import AuditQuery
from apps.platform.api.errors import ProblemError
from apps.platform.contracts.errors import ErrorCode
from apps.platform.contracts.ticket import TicketState
from apps.platform.policy.levels import WriteLevel
from apps.recon.engine import empty_result, reconcile
from apps.recon.loader import build_request
from apps.recon.models import (
    DiffItem,
    DiffType,
    MatchResultLine,
    MissingItem,
    Money,
    ReconMetrics,
)
from apps.security.approvals import TicketFacts
from apps.security.gate import AccessGrant, AccessRequest
from apps.security.identity import SessionToken, UserPrincipal
from apps.security.roles import (
    assert_can_export_audit,
    assert_can_read_ticket,
    assert_can_rollback,
)
from apps.security.scope import Scope, ScopeGuard
from observability.metrics import RECON_FAILURE_UNCLASSIFIED

FAIL_CODE_OPTIONS = tuple(sorted(COMPENSATE_FAIL_CODES))
MAX_AUDIT_LIST = 1000

#: 差异类型对外用 ASCII 码：由枚举成员名派生，新增枚举值自动纳入（不手维护清单）。
DIFF_TYPE_CODES: dict[DiffType, str] = {
    diff_type: diff_type.name.lower() for diff_type in DiffType
}
DIFF_CODE_TO_TYPE = {code: diff_type for diff_type, code in DIFF_TYPE_CODES.items()}

#: 契约查询参数/响应枚举从 ``DiffType`` 派生，避免再手写一份清单
#: （``DIFF_TYPE_CODES`` 漏掉第八类的事故根因就是手维护清单）。
DiffTypeCode = Enum(
    "DiffTypeCode",
    {code: code for code in DIFF_TYPE_CODES.values()},
    type=str,
    module=__name__,
)


# --------------------------------------------------------------------------- 公共小件


def build_scope(payload: ScopeIn) -> Scope:
    return Scope(
        org_id=payload.org_id,
        period=payload.period,
        system_ids=tuple(payload.system_ids),
    )


def principal_of(session: SessionToken) -> UserPrincipal:
    return UserPrincipal(
        tenant_id=session.tenant_id,
        user_id=session.user_id,
        roles=session.roles,
        grant=session.grant,
    )


def request_fingerprint(payload: Mapping[str, Any]) -> str:
    body = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


async def run_idempotent(
    container: ApiContainer,
    *,
    idem_key: str | None,
    fingerprint: str,
    action: Callable[[], Awaitable[tuple[int, Any]]],
) -> tuple[int, Any]:
    """写接口统一入口：缺键 400；同键同参回放；同键不同参 409。"""

    if not idem_key or not idem_key.strip():
        raise ProblemError(
            ErrorCode.IDEM_CONFLICT,
            "写接口必须携带 Idempotency-Key（G-02），缺失直接拒绝",
            status_code=400,
        )
    entry = container.idempotency.lookup(idem_key, fingerprint)
    if entry is not None:
        return entry.status_code, entry.body
    status_code, body = await action()
    container.idempotency.remember(idem_key, fingerprint, status_code, body)
    return status_code, body


def _require_scope(session: SessionToken, scope: Scope) -> Scope:
    return ScopeGuard(session.grant).authorize(scope)


def _authorize(
    container: ApiContainer,
    session: SessionToken,
    scope: Scope,
    *,
    level: WriteLevel,
    ticket_id: str,
    system: str,
    amount: Decimal,
    touches_fund: bool = False,
    field_whitelist: frozenset[str] = frozenset(),
) -> AccessGrant:
    meta = container.connector_meta(system)
    return container.authorize(
        AccessRequest(
            session=session,
            requested_scope=scope,
            connector_meta=meta,
            ticket_id=ticket_id,
            level=level,
            amount=amount,
            touches_fund=touches_fund,
            field_whitelist=field_whitelist,
        )
    )


def _require_ticket(
    container: ApiContainer, session: SessionToken, ticket_id: UUID
) -> TicketRecord:
    """按 ``(tenant_id, ticket_id)`` 取工单；查不到统一 404。

    不使用 403：403 会向攻击者确认「该 id 存在、只是不属于你」，等价于跨租户
    存在性预言机。租户是查询条件，不是取到之后再判断的校验。
    """

    ticket = container.tickets.get(tenant_id=session.tenant_id, ticket_id=ticket_id)
    if ticket is None:
        raise ProblemError(
            ErrorCode.RESOURCE_NOT_FOUND, f"工单 {ticket_id} 不存在", status_code=404
        )
    return ticket


def _require_ticket_in_scope(ticket: TicketRecord, scope: Scope) -> None:
    if ticket.org_id != scope.org_id or ticket.period != scope.period:
        raise ProblemError(
            ErrorCode.AUTHZ_SCOPE,
            f"工单 {ticket.ticket_no} 不在请求 scope（org={scope.org_id} period={scope.period}）",
        )
    if not set(ticket.systems) <= set(scope.system_ids):
        raise ProblemError(
            ErrorCode.AUTHZ_SCOPE,
            f"工单 {ticket.ticket_no} 涉及系统 {list(ticket.systems)} 超出请求 scope",
        )


def _require_initiator(ticket: TicketRecord, session: SessionToken) -> None:
    """审批闸以「(租户, 用户) = (工单租户, 发起人)」判定。

    只比 ``user_id`` 会在「同一 UUID 出现在两个租户」时把身份闸和租户闸一起
    让过去；这里把租户并入身份比较，即便上层查询退化也不会跨租户放行。
    """

    if ticket.tenant_id != session.tenant_id or ticket.initiator_id != session.user_id:
        raise ProblemError(
            ErrorCode.AUTHZ_SCOPE,
            "执行类工单动作须由发起人（执行体）提交；审批人请走 /approvals 接口",
        )


def _required_roles(container: ApiContainer, ticket: TicketRecord) -> tuple[str, ...]:
    if ticket.write_level is WriteLevel.W3:
        return ()
    return container.approvals.required_roles(
        level=ticket.write_level,
        amount=ticket.amount,
        touches_fund=ticket.touches_fund,
    )


def _missing_roles(container: ApiContainer, ticket: TicketRecord) -> tuple[str, ...]:
    if ticket.write_level is WriteLevel.W3:
        return ()
    return container.approvals.missing_roles(
        ticket_id=str(ticket.ticket_id),
        level=ticket.write_level,
        amount=ticket.amount,
        touches_fund=ticket.touches_fund,
        initiator_id=ticket.initiator_id,
    )


def _write_system(ticket: TicketRecord) -> str:
    if ticket.pending_write is not None:
        return ticket.pending_write.system
    if ticket.steps:
        return ticket.steps[-1].system
    return ticket.systems[0]


def _pending_field_whitelist(
    container: ApiContainer, ticket: TicketRecord
) -> frozenset[str]:
    """B 级写只允许落动作登记的可写字段；未登记动作不留空窗。"""

    pending = ticket.pending_write
    if pending is None:
        return frozenset()
    try:
        return container.actions.get(pending.action_key).writable_fields
    except KeyError as exc:
        raise ProblemError(
            ErrorCode.POLICY_LEVEL, f"动作 {pending.action_key} 未注册，拒绝执行"
        ) from exc


# --------------------------------------------------------------------------- 工单


def list_tickets(
    container: ApiContainer,
    session: SessionToken,
    *,
    scope_in: ScopeIn,
    state: str | None,
    scene_code: str | None,
    business_key: str | None,
    ticket_no: str | None,
    initiator_id: UUID | None,
    cursor: str | None,
    limit: int | None,
) -> tuple[list[TicketOut], str | None]:
    assert_can_read_ticket(principal_of(session))
    scope = _require_scope(session, build_scope(scope_in))
    rows, next_cursor = container.tickets.list(
        tenant_id=session.tenant_id,
        scope=scope,
        state=state,
        scene_code=scene_code,
        business_key=business_key,
        ticket_no=ticket_no,
        initiator_id=initiator_id,
        cursor=cursor,
        limit=page_bounds(limit),
    )
    return [_ticket_out(row) for row in rows], next_cursor


def get_ticket(
    container: ApiContainer, session: SessionToken, *, scope_in: ScopeIn, ticket_id: UUID
) -> TicketDetailOut:
    assert_can_read_ticket(principal_of(session))
    scope = _require_scope(session, build_scope(scope_in))
    ticket = _require_ticket(container, session, ticket_id)
    _require_ticket_in_scope(ticket, scope)
    return _ticket_detail(container, ticket)


async def approve_ticket(
    container: ApiContainer,
    session: SessionToken,
    *,
    ticket_id: UUID,
    body: ActionIn,
) -> ActionOut:
    """审批通过并执行写计划：四道闸 → WriteGateway.execute → 审计影像 → 状态推进。"""

    scope = _require_scope(session, build_scope(body.scope))
    ticket = _require_ticket(container, session, ticket_id)
    _require_ticket_in_scope(ticket, scope)
    _require_initiator(ticket, session)
    if ticket.state is not TicketState.PENDING_APPROVAL:
        raise ProblemError(
            ErrorCode.STATE_ILLEGAL,
            f"工单 {ticket.ticket_no} 当前为 {ticket.state.value}，只有待审批可以审批通过",
        )

    grant = _authorize(
        container,
        session,
        scope,
        level=ticket.write_level,
        ticket_id=str(ticket.ticket_id),
        system=_write_system(ticket),
        amount=ticket.amount,
        touches_fund=ticket.touches_fund,
        field_whitelist=_pending_field_whitelist(container, ticket),
    )

    executed: list[TicketStepOut] = []
    if ticket.pending_write is not None:
        step = await _execute_pending_write(
            container,
            ticket,
            ticket.pending_write,
            narrowed_fields=_lease_narrowed_fields(grant),
        )
        executed.append(_step_out(step))

    resolve_transition(
        ticket.state,
        TicketState.EXECUTING,
        "approved",
        TransitionContext(approved=True),
    )
    ticket.state = TicketState.EXECUTING
    ticket.version += 1
    ticket.pending_write = None
    container.tickets.save(ticket)

    return ActionOut(
        ticket_id=ticket.ticket_id,
        action="approve",
        state=ticket.state.value,
        version=ticket.version,
        required_roles=list(grant.required_roles),
        missing_roles=[],
        executed_steps=executed,
    )


def _lease_narrowed_fields(grant: AccessGrant) -> frozenset[str] | None:
    """把租约的字段白名单透传给写闸：只收窄、不放宽写等级。

    委派模式下租约会话的 ``narrowed_fields`` 为空，此时传 ``None`` 表示沿用
    动作自身的可写 scope；服务账号（B 级）才使用租约白名单。
    """

    if grant.lease.mode != "service_account":
        return None
    return grant.lease.narrowed_fields or None


async def _execute_pending_write(
    container: ApiContainer,
    ticket: TicketRecord,
    pending: PendingWrite,
    *,
    narrowed_fields: frozenset[str] | None = None,
) -> TicketStepRecord:
    try:
        action = container.actions.get(pending.action_key)
    except KeyError as exc:
        raise ProblemError(
            ErrorCode.POLICY_LEVEL, f"动作 {pending.action_key} 未注册，拒绝执行"
        ) from exc

    gateway = container.gateway_for(
        pending.system,
        str(ticket.ticket_id),
        _ticket_facts(ticket),
        narrowed_fields=narrowed_fields,
    )
    scene = container.scene_for(ticket.scene_code, ticket.write_level)
    result = await gateway.execute(
        action,
        pending.payload,
        ticket_id=str(ticket.ticket_id),
        idem_key=pending.idem_key,
        scene=scene,
    )

    step = TicketStepRecord(
        step_no=len(ticket.steps) + 1,
        action_key=pending.action_key,
        system=pending.system,
        object_type=action.key,
        object_id=_object_id(pending.payload, result.write.external_id, ticket),
        level=result.level,
        idem_key=pending.idem_key,
        executed_at=datetime.now(UTC),
    )
    image = await container.ledger.append_image(
        tenant_id=ticket.tenant_id,
        ticket_id=ticket.ticket_id,
        step_id=step.step_id,
        system=pending.system,
        object_type=action.key,
        object_id=step.object_id,
        before=result.preview.before,
        after=result.write.response,
        before_ref=result.write.before_ref,
        after_ref=result.write.after_ref,
    )
    step.audit_image_id = image.id
    ticket.steps.append(step)
    return step


def _object_id(
    payload: Mapping[str, Any], external_id: str | None, ticket: TicketRecord
) -> str:
    for key in ("draft_id", "doc_no", "business_key"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return external_id or ticket.business_key or str(ticket.ticket_id)


def reject_ticket(
    container: ApiContainer,
    session: SessionToken,
    *,
    ticket_id: UUID,
    body: ActionIn,
) -> ActionOut:
    """驳回：落审批记录并终止工单；无外部写入，因此不过写闸门。"""

    scope = _require_scope(session, build_scope(body.scope))
    ticket = _require_ticket(container, session, ticket_id)
    _require_ticket_in_scope(ticket, scope)
    if ticket.state is not TicketState.PENDING_APPROVAL:
        raise ProblemError(
            ErrorCode.STATE_ILLEGAL,
            f"工单 {ticket.ticket_no} 当前为 {ticket.state.value}，只有待审批可以驳回",
        )
    if body.role is None:
        raise ProblemError(ErrorCode.POLICY_APPROVAL, "驳回必须指定审批角色（role）")

    _authorize(
        container,
        session,
        scope,
        level=WriteLevel.W0,
        ticket_id=str(ticket.ticket_id),
        system=_write_system(ticket),
        amount=ticket.amount,
        touches_fund=ticket.touches_fund,
    )
    _record_decision(container, ticket, session, body.role, "rejected", body.comment)

    resolve_transition(
        ticket.state, TicketState.REJECTED, "rejected", TransitionContext()
    )
    ticket.state = TicketState.REJECTED
    ticket.version += 1
    ticket.pending_write = None
    container.tickets.save(ticket)
    return ActionOut(
        ticket_id=ticket.ticket_id,
        action="reject",
        state=ticket.state.value,
        version=ticket.version,
        required_roles=list(_required_roles(container, ticket)),
        missing_roles=list(_missing_roles(container, ticket)),
    )


def cancel_ticket(
    container: ApiContainer,
    session: SessionToken,
    *,
    ticket_id: UUID,
    body: ActionIn,
) -> ActionOut:
    """终止：只允许尚未产生外部写入的三个状态，且必须写明原因。"""

    scope = _require_scope(session, build_scope(body.scope))
    ticket = _require_ticket(container, session, ticket_id)
    _require_ticket_in_scope(ticket, scope)
    if not body.comment.strip():
        raise ProblemError(ErrorCode.DATA_MISSING, "终止工单必须写明原因（comment）")

    triggers = {
        TicketState.PLANNED: ("preview_failed", TransitionContext(writes=0)),
        TicketState.NEEDS_REVIEW: ("review_terminated", TransitionContext(writes=0)),
        TicketState.PENDING_APPROVAL: ("approval_terminated", TransitionContext(writes=0)),
    }
    spec = triggers.get(ticket.state)
    if spec is None:
        raise ProblemError(
            ErrorCode.STATE_ILLEGAL,
            f"工单 {ticket.ticket_no} 当前为 {ticket.state.value}，不允许终止",
        )
    trigger, context = spec

    _authorize(
        container,
        session,
        scope,
        level=WriteLevel.W0,
        ticket_id=str(ticket.ticket_id),
        system=_write_system(ticket),
        amount=ticket.amount,
        touches_fund=ticket.touches_fund,
    )
    resolve_transition(ticket.state, TicketState.TERMINATED, trigger, context)
    ticket.state = TicketState.TERMINATED
    ticket.version += 1
    ticket.pending_write = None
    container.tickets.save(ticket)
    return ActionOut(
        ticket_id=ticket.ticket_id,
        action="cancel",
        state=ticket.state.value,
        version=ticket.version,
    )


async def compensate_ticket(
    container: ApiContainer,
    session: SessionToken,
    *,
    ticket_id: UUID,
    body: ActionIn,
) -> ActionOut:
    """触发补偿：先整单推导，再按系统分组经 WriteGateway 回写。"""

    scope = _require_scope(session, build_scope(body.scope))
    ticket = _require_ticket(container, session, ticket_id)
    _require_ticket_in_scope(ticket, scope)
    # C1：自己发起的工单可直接回滚；他人工单仅 IT/CIO 与 CFO，且必须带理由。
    rollback_other = assert_can_rollback(
        principal_of(session),
        initiator_id=ticket.initiator_id,
        reason=body.reason,
    )
    if ticket.state not in (TicketState.EXECUTING, TicketState.COMPENSATING):
        raise ProblemError(
            ErrorCode.STATE_ILLEGAL,
            f"工单 {ticket.ticket_no} 当前为 {ticket.state.value}，只有执行中/补偿中可触发补偿",
        )
    if body.fail_code not in FAIL_CODE_OPTIONS:
        raise ProblemError(
            ErrorCode.DATA_MISSING,
            f"触发补偿必须声明失败分类（fail_code ∈ {list(FAIL_CODE_OPTIONS)}）",
        )

    steps = await _load_step_writes(container, ticket)
    if not steps:
        raise ProblemError(
            ErrorCode.STATE_ILLEGAL, f"工单 {ticket.ticket_no} 没有可补偿的成功步骤"
        )

    max_level = max((WriteLevel.parse(step.level) for step in steps), default=WriteLevel.W0)
    _authorize(
        container,
        session,
        scope,
        level=max_level,
        ticket_id=str(ticket.ticket_id),
        system=_write_system(ticket),
        amount=ticket.amount,
        touches_fund=ticket.touches_fund,
    )

    plan = RollbackPlanner(container.actions).plan(ticket.ticket_id, steps)
    # 先整单判定拒因，避免「部分系统已回写、部分系统被拒」的半截状态。
    if plan.has_forbidden:
        raise RollbackForbidden(
            "回滚被硬拒绝: "
            + "; ".join(
                f"step#{item.step.step_no}:{item.reason}"
                for item in plan.items
                if item.kind is RollbackKind.FORBIDDEN
            )
        )
    grouped = _group_by_system(plan)
    for system, _items in grouped:
        container.connector_meta(system)  # 缺连接器时在改状态之前就报错

    if ticket.state is TicketState.EXECUTING:
        resolve_transition(
            ticket.state,
            TicketState.COMPENSATING,
            "step_failed",
            TransitionContext(fail_code=body.fail_code),
        )
        ticket.state = TicketState.COMPENSATING
        ticket.version += 1
        container.tickets.save(ticket)

    executed: list[TicketStepOut] = []
    manual_tasks: list[str] = []
    for system, items in grouped:
        gateway = container.gateway_for(
            system, str(ticket.ticket_id), _ticket_facts(ticket)
        )
        scene = container.scene_for(ticket.scene_code, max_level)
        service = RollbackService(gateway=gateway, ledger=container.ledger, scene=scene)
        result = await service.execute(
            RollbackPlan(ticket_id=plan.ticket_id, items=tuple(items)),
            confirmed_manual=body.confirmed_manual,
            # C1：回滚他人工单时理由/操作者/角色随回滚影像一起留痕；自己发起的工单不多写。
            reason=(body.reason or "") if rollback_other else "",
            actor_id=session.user_id if rollback_other else None,
            actor_roles=tuple(sorted(session.roles)) if rollback_other else (),
        )
        for done in result.executed:
            executed.append(
                TicketStepOut(
                    step_no=done.item.step.step_no,
                    step_id=done.item.step.step_id,
                    action_key=done.item.action_key,
                    system=done.item.step.audit_image.system,
                    object_type=done.item.step.audit_image.object_type,
                    object_id=done.item.step.audit_image.object_id,
                    state="rolled_back",
                    level=done.level.name,
                    idem_key=done.item.step.idem_key,
                    executed_at=done.audit_image.created_at,
                )
            )
        manual_tasks.extend(
            f"step#{task.step_id}:{task.action_key}:{task.reason}"
            for task in result.manual_tasks
        )

    if manual_tasks:
        resolve_transition(
            TicketState.COMPENSATING,
            TicketState.MANUAL_REQUIRED,
            "compensation_failed",
            TransitionContext(compensation_failed=len(manual_tasks)),
        )
        ticket.state = TicketState.MANUAL_REQUIRED
    else:
        resolve_transition(
            TicketState.COMPENSATING,
            TicketState.ROLLED_BACK,
            "all_compensated",
            TransitionContext(compensation_failed=0),
        )
        ticket.state = TicketState.ROLLED_BACK
    ticket.version += 1
    container.tickets.save(ticket)

    return ActionOut(
        ticket_id=ticket.ticket_id,
        action="compensate",
        state=ticket.state.value,
        version=ticket.version,
        executed_steps=executed,
        manual_tasks=manual_tasks,
    )


async def _load_step_writes(
    container: ApiContainer, ticket: TicketRecord
) -> list[StepWrite]:
    images = await container.ledger.list_images(
        AuditQuery(tenant_id=ticket.tenant_id, ticket_id=ticket.ticket_id, limit=MAX_AUDIT_LIST)
    )
    # 同一 step 会有回滚/人工任务影像；补偿只认最初那次写入影像。
    original: dict[UUID, Any] = {}
    for image in images:
        if image.object_type.endswith(":rollback") or image.object_type.endswith(
            ":manual_task"
        ):
            continue
        original.setdefault(image.step_id, image)
    steps: list[StepWrite] = []
    for record in sorted(ticket.steps, key=lambda item: item.step_no):
        image = original.get(record.step_id)
        if image is None:
            continue
        steps.append(
            StepWrite(
                step_id=record.step_id,
                step_no=record.step_no,
                action_key=record.action_key,
                level=WriteLevel.parse(record.level),
                idem_key=record.idem_key,
                audit_image=image,
            )
        )
    return steps


def _group_by_system(plan: RollbackPlan) -> list[tuple[str, list[Any]]]:
    grouped: dict[str, list[Any]] = {}
    for item in plan.items:
        grouped.setdefault(item.step.audit_image.system, []).append(item)
    return sorted(grouped.items(), key=lambda pair: pair[0])


def _ticket_facts(ticket: TicketRecord) -> TicketFacts:
    return TicketFacts(
        amount=ticket.amount,
        touches_fund=ticket.touches_fund,
        initiator_id=ticket.initiator_id,
    )


# --------------------------------------------------------------------------- 审批


def list_pending_approvals(
    container: ApiContainer,
    session: SessionToken,
    *,
    scope_in: ScopeIn,
    scene_code: str | None,
    cursor: str | None,
    limit: int | None,
) -> tuple[list[ApprovalTaskOut], str | None]:
    scope = _require_scope(session, build_scope(scope_in))
    rows, next_cursor = container.tickets.list(
        tenant_id=session.tenant_id,
        scope=scope,
        state=TicketState.PENDING_APPROVAL.value,
        scene_code=scene_code,
        cursor=cursor,
        limit=page_bounds(limit),
    )
    tasks: list[ApprovalTaskOut] = []
    for ticket in rows:
        required = _required_roles(container, ticket)
        if not required:
            continue
        if ticket.initiator_id == session.user_id:
            continue  # 发起人不得审批自己的工单
        if not (set(required) & set(session.roles)):
            continue
        tasks.append(
            ApprovalTaskOut(
                ticket_id=ticket.ticket_id,
                ticket_no=ticket.ticket_no,
                scene_code=ticket.scene_code,
                business_key=ticket.business_key,
                state=ticket.state.value,
                write_level=ticket.write_level.name,
                amount=_amount_text(ticket.amount),
                touches_fund=ticket.touches_fund,
                initiator_id=ticket.initiator_id,
                required_roles=list(required),
                missing_roles=list(_missing_roles(container, ticket)),
                created_at=ticket.created_at,
            )
        )
    return tasks, next_cursor


def decide_approval(
    container: ApiContainer,
    session: SessionToken,
    *,
    ticket_id: UUID,
    body: ApprovalDecisionIn,
    decision: str,
) -> ApprovalDecisionOut:
    """审批人落审批记录；驳回同时终止工单，通过不代为执行（执行由发起人触发）。"""

    scope = _require_scope(session, build_scope(body.scope))
    ticket = _require_ticket(container, session, ticket_id)
    _require_ticket_in_scope(ticket, scope)
    if ticket.state is not TicketState.PENDING_APPROVAL:
        raise ProblemError(
            ErrorCode.STATE_ILLEGAL,
            f"工单 {ticket.ticket_no} 当前为 {ticket.state.value}，不在待审批",
        )
    _authorize(
        container,
        session,
        scope,
        level=WriteLevel.W0,
        ticket_id=str(ticket.ticket_id),
        system=_write_system(ticket),
        amount=ticket.amount,
        touches_fund=ticket.touches_fund,
    )
    record = _record_decision(
        container, ticket, session, body.role, decision, body.comment
    )

    if decision == "rejected":
        resolve_transition(
            ticket.state, TicketState.REJECTED, "rejected", TransitionContext()
        )
        ticket.state = TicketState.REJECTED
        ticket.version += 1
        ticket.pending_write = None
        container.tickets.save(ticket)

    required = _required_roles(container, ticket)
    return ApprovalDecisionOut(
        ticket_id=ticket.ticket_id,
        role=record.role,
        decision=record.decision,
        decided_at=record.decided_at,
        satisfied=container.approvals.satisfied(
            ticket_id=str(ticket.ticket_id),
            level=ticket.write_level,
            amount=ticket.amount,
            touches_fund=ticket.touches_fund,
            initiator_id=ticket.initiator_id,
        ),
        required_roles=list(required),
        missing_roles=list(_missing_roles(container, ticket)),
        ticket_state=ticket.state.value,
    )


def _record_decision(
    container: ApiContainer,
    ticket: TicketRecord,
    session: SessionToken,
    role: str,
    decision: str,
    comment: str,
):
    return container.approvals.record(
        ticket_id=str(ticket.ticket_id),
        role=role,
        approver=principal_of(session),
        initiator_id=ticket.initiator_id,
        decision=decision,  # type: ignore[arg-type]
        comment=comment,
    )


# --------------------------------------------------------------------------- 对账


async def run_recon(
    container: ApiContainer, session: SessionToken, *, body: ReconRunIn
) -> ReconBatchOut:
    """对账入口：成功记批次结果，失败单独记 ``recon_runs_failed_total``。

    失败与「批次量变少」在告警上是两件事：异常路径必须自己计数，不能被
    ``recon_batches_total`` 的减少掩盖。``stage`` 只在三个封闭阶段间推进。
    """

    stage = "input"

    async def _execute() -> ReconBatchOut:
        nonlocal stage
        scope = _require_scope(session, build_scope(body.scope))
        dataset = body.dataset or container.recon_datasets.lookup(
            body.sources.statement_files
        )
        if dataset is None:
            raise ProblemError(
                ErrorCode.DATA_MISSING,
                "缺少对账数据行：请提供 dataset，或先登记 sources.statement_files 对应的数据集"
                "（本接口不猜测取数来源）",
            )

        batch_id = uuid4()
        _authorize(
            container,
            session,
            scope,
            level=WriteLevel.W0,
            ticket_id=str(batch_id),
            system=container.pick_meta(scope.system_ids).system,
            amount=Decimal(0),
        )

        stage = "engine"
        raw = _recon_raw(scope, body, dataset)
        started = time.monotonic()
        recon_request = build_request(raw)
        if not recon_request.our_lines and not recon_request.supplier_lines:
            result = empty_result("我们的单据与供应商对账单都没有数据行")
        else:
            result = reconcile(recon_request)

        batch = ReconBatchRecord(
            batch_id=batch_id,
            tenant_id=session.tenant_id,
            org_id=scope.org_id,
            period=scope.period,
            system_ids=scope.system_ids,
            suppliers=tuple(body.suppliers),
            status=result.batch_status.value,
            conclusive=result.conclusive,
            status_reason=result.status_reason,
            metrics=_metrics_out(result.metrics).model_dump(mode="json"),
            missing_items=tuple(
                _missing_out(item).model_dump(mode="json") for item in result.missing_items
            ),
            result=result,
            created_by=session.user_id,
            created_at=datetime.now(UTC),
        )
        input_fingerprint = request_fingerprint(body.model_dump(mode="json"))
        stage = "persist"
        try:
            await _recon_store(container).save(
                snapshot_from_batch(
                    batch,
                    input_fingerprint=input_fingerprint,
                    rerun_of=body.rerun_of,
                    rerun_reason=body.rerun_reason,
                    view=_batch_view(batch),
                )
            )
        except ReconConflict as exc:
            raise ProblemError(
                ErrorCode.IDEM_CONFLICT, str(exc), status_code=409
            ) from exc
        container.recon_batches.add(batch)
        # 每批次只记一次：observe_recon_result 已含批次计数，不要再叠加 record_recon_batch。
        container.metrics.observe_recon_result(
            result, duration_seconds=time.monotonic() - started
        )
        return _batch_out(batch)

    try:
        return await _execute()
    except ProblemError as exc:
        container.metrics.record_recon_failure(stage=stage, code=exc.code.value)
        raise
    except Exception:
        container.metrics.record_recon_failure(
            stage=stage, code=RECON_FAILURE_UNCLASSIFIED
        )
        raise


def _recon_raw(scope: Scope, body: ReconRunIn, dataset: Any) -> dict[str, Any]:
    if isinstance(dataset, ReconDatasetIn):
        data = dataset.model_dump()
    else:  # 已登记的数据集允许直接是 AG-1 形状的 dict
        data = dict(dataset)
    return {
        "scope": scope.as_dict(),
        "suppliers": list(body.suppliers),
        "our_lines": data.get("our_lines", []),
        "supplier_lines": data.get("supplier_lines", []),
        "master_data": data.get("master_data", {}),
        "options": {"tolerance": (data.get("tolerance") or {})},
    }


async def get_recon_batch(
    container: ApiContainer, session: SessionToken, *, scope_in: ScopeIn, batch_id: UUID
) -> ReconBatchOut:
    """运行详情：读持久化视图投影，重启后仍可访问。"""

    scope = _require_scope(session, build_scope(scope_in))
    view = await _load_view(container, session, scope, batch_id)
    return ReconBatchOut.model_validate(view["batch"])


def _metering_policy(container: ApiContainer) -> MeteringPolicy:
    if container.metering is not None:
        return container.metering
    try:
        return MeteringPolicy.from_env()
    except MeteringConfigError as exc:
        # 配置缺失是服务未就绪，不是请求错误；拒绝出账，绝不用默认单价兜底。
        raise ProblemError(ErrorCode.DATA_MISSING, str(exc), status_code=503) from exc


def _recon_store(container: ApiContainer) -> ReconStore:
    if container.recon_store is not None:
        return container.recon_store
    store = SqlReconStore()
    container.recon_store = store
    return store


async def _load_snapshot(
    container: ApiContainer,
    session: SessionToken,
    scope: Scope,
    batch_id: UUID,
) -> BatchSnapshot:
    snapshot = await _recon_store(container).load(batch_id, session.tenant_id)
    if snapshot is None:
        raise ProblemError(
            ErrorCode.RESOURCE_NOT_FOUND, f"对账批次 {batch_id} 不存在", status_code=404
        )
    if snapshot.org_id != scope.org_id or snapshot.period != scope.period:
        raise ProblemError(
            ErrorCode.AUTHZ_SCOPE, f"对账批次 {batch_id} 不在请求 scope 内"
        )
    if not set(snapshot.system_ids) <= set(scope.system_ids):
        raise ProblemError(
            ErrorCode.AUTHZ_SCOPE, f"对账批次 {batch_id} 涉及系统超出请求 scope"
        )
    return snapshot


async def get_metering_statement(
    container: ApiContainer,
    session: SessionToken,
    *,
    scope_in: ScopeIn,
    batch_id: UUID,
) -> MeteringStatementOut:
    """只读计量表：读持久化快照（不读内存），不产生任何写入/状态变更。"""

    scope = _require_scope(session, build_scope(scope_in))
    snapshot = await _load_snapshot(container, session, scope, batch_id)
    return build_statement(snapshot, _metering_policy(container))


async def export_metering_csv(
    container: ApiContainer,
    session: SessionToken,
    *,
    scope_in: ScopeIn,
    batch_id: UUID,
) -> tuple[str, bytes, str]:
    """按批导出逐行明细；返回 (文件名, 内容, sha256)。"""

    statement = await get_metering_statement(
        container, session, scope_in=scope_in, batch_id=batch_id
    )
    content, digest = statement_csv(statement)
    return f"metering-{statement.batch_id}.csv", content, digest


async def list_recon_diffs(
    container: ApiContainer,
    session: SessionToken,
    *,
    scope_in: ScopeIn,
    batch_id: UUID,
    diff_type: str | None,
    needs_manual: bool | None,
    supplier_code: str | None,
    billing_bucket: str | None,
    cursor: str | None,
    limit: int | None,
) -> tuple[list[ReconDiffOut], str | None]:
    scope = _require_scope(session, build_scope(scope_in))
    view = await _load_view(container, session, scope, batch_id)
    _require_diff_type(diff_type)
    _require_billing_bucket(billing_bucket)
    size = page_bounds(limit)
    offset = read_offset(cursor)

    rows = [
        item
        for item in view["diffs"]
        if _diff_matches_view(item, diff_type, needs_manual, supplier_code)
        and (billing_bucket is None or item["billing_bucket"] == billing_bucket)
    ]
    window = rows[offset : offset + size + 1]
    page = window[:size]
    next_cursor = offset_cursor(offset + size) if len(window) > size else None
    return [ReconDiffOut.model_validate(item) for item in page], next_cursor


async def get_recon_diff(
    container: ApiContainer,
    session: SessionToken,
    *,
    scope_in: ScopeIn,
    batch_id: UUID,
    diff_id: str,
) -> ReconDiffDetailOut:
    scope = _require_scope(session, build_scope(scope_in))
    view = await _load_view(container, session, scope, batch_id)
    _parse_diff_index(diff_id, len(view["diffs"]))
    detail = view["details"].get(diff_id)
    if detail is None:
        raise ProblemError(
            ErrorCode.DATA_MISSING,
            f"差异 {diff_id} 的详情投影缺失，无法还原",
            status_code=503,
        )
    return ReconDiffDetailOut.model_validate(detail)


def _parse_diff_index(diff_id: str, count: int) -> int:
    if not diff_id.startswith("d") or not diff_id[1:].isdigit():
        raise ProblemError(
            ErrorCode.RESOURCE_NOT_FOUND, f"差异编号 {diff_id!r} 非法", status_code=404
        )
    index = int(diff_id[1:])
    if index < 0 or index >= count:
        raise ProblemError(
            ErrorCode.RESOURCE_NOT_FOUND, f"差异 {diff_id} 不存在", status_code=404
        )
    return index


def _diff_matches_view(
    item: Mapping[str, Any],
    diff_type: str | None,
    needs_manual: bool | None,
    supplier_code: str | None,
) -> bool:
    """按持久化投影里的已序列化字段过滤，与内存版判定口径一致。"""

    if diff_type is not None and item["type"] != diff_type:
        return False
    if supplier_code is not None and item["supplier_code"] != supplier_code:
        return False
    return not (needs_manual is not None and item["needs_manual"] != needs_manual)


def _require_diff_type(code: str | None) -> None:
    if code is not None and code not in DIFF_CODE_TO_TYPE:
        raise ProblemError(
            ErrorCode.PARSE_FAILED,
            f"diff_type={code!r} 非法，可选 {sorted(DIFF_CODE_TO_TYPE)}",
        )


def _diff_bucket_map(batch: ReconBatchRecord) -> dict[int, BillingBucket]:
    """差异 → 所属行的计费桶；与计量表计数共用 ``classify_billing_bucket``。"""

    buckets: dict[int, BillingBucket] = {}
    for line in batch.result.lines:
        bucket = classify_billing_bucket(
            status=line.status.value, needs_manual=line.needs_manual
        )
        for diff in line.diffs:
            buckets[id(diff)] = bucket
    return buckets


def _bucket_of(buckets: Mapping[int, BillingBucket], diff: DiffItem) -> BillingBucket:
    # 无归属行的差异按「不参与计费」处理，避免被误标成可计费。
    return buckets.get(id(diff), BillingBucket.OTHER_UNBILLABLE)


def _require_billing_bucket(code: str | None) -> None:
    if code is not None and code not in {bucket.value for bucket in BillingBucket}:
        raise ProblemError(
            ErrorCode.PARSE_FAILED,
            f"billing_bucket={code!r} 非法，可选 {sorted(b.value for b in BillingBucket)}",
        )


# --------------------------------------------------------------------------- 审计


async def list_audit_images(
    container: ApiContainer,
    session: SessionToken,
    *,
    scope_in: ScopeIn,
    ticket_id: UUID,
    system: str | None,
    step_id: UUID | None,
    cursor: str | None,
    limit: int | None,
) -> tuple[list[AuditImageOut], str | None]:
    assert_can_read_ticket(principal_of(session))
    scope = _require_scope(session, build_scope(scope_in))
    ticket = _require_ticket(container, session, ticket_id)
    _require_ticket_in_scope(ticket, scope)
    size = page_bounds(limit)
    offset = read_offset(cursor)
    images = await container.ledger.list_images(
        AuditQuery(
            tenant_id=ticket.tenant_id,
            ticket_id=ticket.ticket_id,
            system=system,
            step_id=step_id,
            offset=offset,
            limit=size + 1,
        )
    )
    page = images[:size]
    next_cursor = offset_cursor(offset + size) if len(images) > size else None
    return [_image_out(image) for image in page], next_cursor


async def audit_chain(
    container: ApiContainer,
    session: SessionToken,
    *,
    scope_in: ScopeIn,
    ticket_id: UUID,
) -> AuditChainOut:
    assert_can_read_ticket(principal_of(session))
    scope = _require_scope(session, build_scope(scope_in))
    ticket = _require_ticket(container, session, ticket_id)
    _require_ticket_in_scope(ticket, scope)
    verification = await container.ledger.verify(ticket.tenant_id)
    images = await container.ledger.list_images(
        AuditQuery(tenant_id=ticket.tenant_id, ticket_id=ticket.ticket_id, limit=MAX_AUDIT_LIST)
    )
    return AuditChainOut(
        ticket_id=ticket.ticket_id,
        tenant_id=ticket.tenant_id,
        ok=verification.ok,
        checked=verification.checked,
        broken_at=verification.broken_at,
        detail=verification.detail,
        image_count=len(images),
        head_hash=images[-1].hash if images else "",
    )


async def export_audit_csv(
    container: ApiContainer,
    session: SessionToken,
    *,
    scope_in: ScopeIn,
    ticket_id: UUID,
    system: str | None,
) -> tuple[str, bytes, str]:
    """按工单导出审计明细；返回 (文件名, 内容, sha256)。

    导出本身也是一次对审计数据的读取，必须留痕（FR-1304）：先写审计影像，
    写入失败即抛异常、不返回内容——不允许「导出了但没留痕」。
    """

    scope = _require_scope(session, build_scope(scope_in))
    ticket = _require_ticket(container, session, ticket_id)
    _require_ticket_in_scope(ticket, scope)
    # 角色闸放在租户取数之后：跨租户一律 404，不因角色不同泄露工单是否存在。
    assert_can_export_audit(principal_of(session))
    images = await container.ledger.list_images(
        AuditQuery(
            tenant_id=ticket.tenant_id,
            ticket_id=ticket.ticket_id,
            system=system,
            limit=MAX_AUDIT_LIST,
        )
    )
    lines = ["image_id,step_id,system,object_type,object_id,prev_hash,hash,diff_missing,created_at,field_diff"]
    for image in images:
        lines.append(
            ",".join(
                [
                    str(image.id),
                    str(image.step_id),
                    _csv(image.system),
                    _csv(image.object_type),
                    _csv(image.object_id),
                    image.prev_hash,
                    image.hash,
                    "true" if image.diff_missing else "false",
                    image.created_at.isoformat(),
                    _csv(json.dumps([entry.model_dump(mode="json") for entry in image.field_diff], ensure_ascii=False)),
                ]
            )
        )
    content = ("\n".join(lines) + "\n").encode("utf-8-sig")
    digest = hashlib.sha256(content).hexdigest()
    file_name = f"audit-{ticket.ticket_no}.csv"

    await container.ledger.append_image(
        tenant_id=ticket.tenant_id,
        ticket_id=ticket.ticket_id,
        step_id=uuid4(),
        system=system or _write_system(ticket),
        object_type="audit:export",
        object_id=file_name,
        before={"export": "requested"},
        after={
            "export": "delivered",
            "file": file_name,
            "sha256": digest,
            "images": len(images),
            "actor_id": str(session.user_id),
            "actor_roles": sorted(session.roles),
        },
    )
    return file_name, content, digest


def _csv(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


# --------------------------------------------------------------------------- 序列化


def _amount_text(value: Decimal) -> str:
    return f"{value:.4f}"


def _decimal_text(value: Decimal) -> str:
    return format(value.normalize(), "f")


def _money_out(money: Money) -> MoneyOut:
    return MoneyOut(
        amount=_amount_text(money.amount),
        currency=money.currency,
        tax_included=money.tax_included,
    )


def _evidence_out(item: Any) -> EvidenceOut:
    return EvidenceOut(
        system=item.system,
        doc_no=item.doc_no,
        field=item.field,
        coord=dict(item.coord) if item.coord else None,
    )


def _ticket_out(ticket: TicketRecord) -> TicketOut:
    return TicketOut(
        ticket_id=ticket.ticket_id,
        ticket_no=ticket.ticket_no,
        scene_code=ticket.scene_code,
        state=ticket.state.value,
        version=ticket.version,
        initiator_id=ticket.initiator_id,
        business_key=ticket.business_key,
        org_id=ticket.org_id,
        period=ticket.period,
        systems=list(ticket.systems),
        write_level=ticket.write_level.name,
        amount=_amount_text(ticket.amount),
        touches_fund=ticket.touches_fund,
        created_at=ticket.created_at,
        updated_at=ticket.updated_at,
    )


def _ticket_detail(container: ApiContainer, ticket: TicketRecord) -> TicketDetailOut:
    records = container.approvals.records(str(ticket.ticket_id))
    return TicketDetailOut(
        **_ticket_out(ticket).model_dump(),
        steps=[_step_out(step) for step in ticket.steps],
        approvals=[
            ApprovalOut(
                role=record.role,
                approver_id=record.approver_id,
                decision=record.decision,
                comment=record.comment,
                decided_at=record.decided_at,
            )
            for record in records
        ],
        required_roles=list(_required_roles(container, ticket)),
        missing_roles=list(_missing_roles(container, ticket)),
        audit_image_count=len(ticket.steps),
    )


def _step_out(step: TicketStepRecord) -> TicketStepOut:
    return TicketStepOut(
        step_no=step.step_no,
        step_id=step.step_id,
        action_key=step.action_key,
        system=step.system,
        object_type=step.object_type,
        object_id=step.object_id,
        state=step.state,
        level=step.level.name,
        idem_key=step.idem_key,
        executed_at=step.executed_at,
    )


def _metrics_out(metrics: ReconMetrics | None) -> ReconMetricsOut | None:
    if metrics is None:
        return None
    return ReconMetricsOut(
        supplier_lines=metrics.supplier_lines,
        matched=metrics.matched,
        matched_rate=_decimal_text(metrics.matched_rate),
        ambiguous=metrics.ambiguous,
        candidate_only=metrics.candidate_only,
        unmatched=metrics.unmatched,
        diff_lines=metrics.diff_lines,
        diff_rate=_decimal_text(metrics.diff_rate),
        diff_amount=_amount_text(metrics.diff_amount),
        suspected_anomaly=metrics.suspected_anomaly,
        auto_disposal_allowed=metrics.auto_disposal_allowed,
    )


def _missing_out(item: MissingItem) -> MissingItemOut:
    return MissingItemOut(
        kind=item.kind.value,
        code=item.code,
        detail=item.detail,
        source_ref=item.source_ref,
    )


def _batch_out(batch: ReconBatchRecord) -> ReconBatchOut:
    metrics = batch.metrics
    return ReconBatchOut(
        batch_id=batch.batch_id,
        status=batch.status,
        conclusive=batch.conclusive,
        status_reason=batch.status_reason,
        org_id=batch.org_id,
        period=batch.period,
        system_ids=list(batch.system_ids),
        suppliers=list(batch.suppliers),
        metrics=ReconMetricsOut(**metrics) if metrics else None,
        missing_items=[MissingItemOut(**item) for item in batch.missing_items],
        diff_count=batch.diff_count,
        created_by=batch.created_by,
        created_at=batch.created_at,
    )


def _diff_out(
    batch_id: UUID, index: int, diff: DiffItem, bucket: BillingBucket
) -> ReconDiffOut:
    return ReconDiffOut(
        diff_id=f"d{index}",
        type=DIFF_TYPE_CODES[diff.type],
        amount=_money_out(diff.amount),
        owner=diff.owner.value,
        suggestion=diff.suggestion,
        confidence=_decimal_text(diff.confidence),
        needs_manual=diff.needs_manual,
        billing_bucket=bucket,
        ref=diff.ref,
        supplier_code=diff.supplier_code,
        evidence=[_evidence_out(item) for item in diff.evidence],
    )


def _line_out(line: MatchResultLine) -> ReconLineResultOut:
    return ReconLineResultOut(
        item=line.item,
        qty_mine=_decimal_text(line.qty_mine) if line.qty_mine is not None else None,
        qty_supplier=(
            _decimal_text(line.qty_supplier) if line.qty_supplier is not None else None
        ),
        amount=_money_out(line.amount) if line.amount is not None else None,
        match_type=line.match_type.value,
        confidence=_decimal_text(line.confidence),
        status=line.status.value,
        our_ref=line.our_ref,
        supplier_ref=line.supplier_ref,
        needs_manual=line.needs_manual,
        note=line.note,
    )


#: 批次视图投影的 schema 版本。读侧只认当前版本，未知版本明确报错，
#: 不做 best-effort 解析（旧 JSON 的形状半年后没人记得，猜错比报错更贵）。
VIEW_VERSION = 1


def _batch_view(batch: ReconBatchRecord) -> dict[str, Any]:
    """把运行详情/差异列表/差异详情冻成一份投影，随批次一起持久化。

    投影是批次产物的一部分（真源），不是可丢弃缓存：``related_lines``、
    ``draft``、``unattributed`` 与计费桶无法只从 ``recon_line`` 重建。
    """

    buckets = _diff_bucket_map(batch)
    diffs: list[dict[str, Any]] = []
    details: dict[str, Any] = {}
    for index, diff in enumerate(batch.result.diffs):
        bucket = _bucket_of(buckets, diff)
        diff_id = f"d{index}"
        diffs.append(_diff_out(batch.batch_id, index, diff, bucket).model_dump(mode="json"))
        details[diff_id] = _diff_detail_out(batch, index, diff, bucket).model_dump(
            mode="json"
        )
    return {
        "view_version": VIEW_VERSION,
        "batch": _batch_out(batch).model_dump(mode="json"),
        "diffs": diffs,
        "details": details,
    }


async def _load_view(
    container: ApiContainer,
    session: SessionToken,
    scope: Scope,
    batch_id: UUID,
) -> Mapping[str, Any]:
    snapshot = await _load_snapshot(container, session, scope, batch_id)
    view = snapshot.view
    if view is None:
        raise ProblemError(
            ErrorCode.DATA_MISSING,
            f"对账批次 {batch_id} 未持久化视图投影（旧版本写入），请重跑该批次",
            status_code=503,
        )
    version = view.get("view_version")
    if version != VIEW_VERSION:
        raise ProblemError(
            ErrorCode.DATA_MISSING,
            f"对账批次 {batch_id} 视图投影版本 {version!r} 非当前支持版本"
            f" {VIEW_VERSION}，拒绝解析",
            status_code=503,
        )
    return view


def _diff_detail_out(
    batch: ReconBatchRecord, index: int, diff: DiffItem, bucket: BillingBucket
) -> ReconDiffDetailOut:
    base = _diff_out(batch.batch_id, index, diff, bucket)
    draft = next(
        (
            item
            for item in batch.result.discrepancy_drafts
            if item.refs and diff.ref in item.refs
        ),
        None,
    )
    unattributed = next(
        (item for item in batch.result.unattributed if item.ref == diff.ref), None
    )
    return ReconDiffDetailOut(
        **base.model_dump(),
        batch_id=batch.batch_id,
        related_lines=[
            _line_out(line)
            for line in batch.result.lines
            if any(item.ref == diff.ref for item in line.diffs)
        ],
        draft=(
            DiscrepancyDraftOut(
                draft_no=draft.draft_no,
                supplier_code=draft.supplier_code,
                period=draft.period,
                diff_type=draft.diff_type.value,
                amount=_money_out(draft.amount),
                owner=draft.owner.value,
                suggestion=draft.suggestion,
                confidence=_decimal_text(draft.confidence),
                needs_manual=draft.needs_manual,
                refs=list(draft.refs),
                status=draft.status,
            )
            if draft is not None
            else None
        ),
        unattributed=(
            UnattributedOut(
                ref=unattributed.ref,
                amount_diff=_money_out(unattributed.amount_diff),
                reason=unattributed.reason,
                evidence=[_evidence_out(item) for item in unattributed.evidence],
            )
            if unattributed is not None
            else None
        ),
    )


def _image_out(image: Any) -> AuditImageOut:
    return AuditImageOut(
        image_id=image.id,
        ticket_id=image.ticket_id,
        step_id=image.step_id,
        system=image.system,
        object_type=image.object_type,
        object_id=image.object_id,
        before_ref=image.before_ref,
        after_ref=image.after_ref,
        field_diff=[entry.model_dump(mode="json") for entry in image.field_diff],
        diff_missing=image.diff_missing,
        prev_hash=image.prev_hash,
        hash=image.hash,
        created_at=image.created_at,
    )


__all__ = [
    "approve_ticket",
    "audit_chain",
    "build_scope",
    "cancel_ticket",
    "compensate_ticket",
    "decide_approval",
    "export_audit_csv",
    "get_recon_batch",
    "get_recon_diff",
    "get_ticket",
    "list_audit_images",
    "list_pending_approvals",
    "list_recon_diffs",
    "list_tickets",
    "principal_of",
    "reject_ticket",
    "request_fingerprint",
    "run_idempotent",
    "run_recon",
]
