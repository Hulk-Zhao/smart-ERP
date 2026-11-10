"""权限矩阵测试：角色 × 操作 × 资源归属（客户安全尽调证据）。

矩阵口径（与 ``docs/product/tech/permission-matrix.md`` 一一对应）：

- 角色 5 类：发起人 / 审批人 / 审计员 / 平台管理员 / 服务账号；
- 操作 7 类：读工单 / 写工单 / 审批 / 驳回 / 回滚 / 导出审计 / 看影像；
- 资源归属 3 类：本组织 / 跨组织 / 跨批次（跨期间，Scope 无独立批次维度，见文档）。

每个格子只断言「HTTP 结果 + 是否产生外部写入」，不复制业务实现；拒绝类用例
一律断言连接器零预览零写入，证明拒绝发生在 WriteGateway 之前。

硬约束（B1–B4）单独成测：

- B1 审批人 ≠ 发起人：接口层拒绝 + DB CHECK/UNIQUE 双验；
- B2 跨组织一律拒绝，含伪造 org 头的场景；
- B3 服务账号（B 级）强制双人、最高 W1、字段白名单；
- B4 W3 对任何角色都拒绝（网关、审批、回滚三条路径）。
"""

from __future__ import annotations

import asyncio
from decimal import Decimal

import pytest
from matrix_support import (
    ORG,
    ORG2,
    PERIOD,
    PERIOD2,
    PREFIX,
    ROLES,
    SERVICE,
    SYSTEM,
    SYSTEM_B,
    MatrixEnv,
    build_env,
    meta_a,
    meta_b,
    teardown_env,
)
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateTable

from apps.api.service import principal_of
from apps.platform.policy.gateway import (
    FieldScopeViolation,
    ForbiddenWriteError,
    WriteGateway,
)
from apps.platform.policy.levels import (
    ActionSpec,
    RateLimit,
    SceneSpec,
    VerifySpec,
    WriteLevel,
)
from apps.security.approvals import (
    ApprovalService,
    SelfApprovalRejected,
    W3NotApprovable,
)
from apps.security.gate import ApprovalNotSatisfied, LevelNotAllowed, W3AccessDenied
from apps.security.identity import MissingFieldWhitelist
from apps.security.models import ApprovalRecordRow
from apps.security.scope import Scope

OPS = ("read", "write", "approve", "reject", "rollback", "export", "images")
OWNERSHIPS = ("same_org", "cross_org", "cross_period")

#: 资源类型 → 承载该操作的工单种类（见 matrix_support._seed_container）。
KIND_FOR_OP = {
    "read": "read",
    "write": "write",
    "approve": "dual",
    "reject": "dual_reject",
    "rollback": "rolled",
    "export": "rolled",
    "images": "rolled",
}

#: 本组织格子的期望：(判定, HTTP 状态)。跨组织/跨批次一律拒绝，不在此表。
#:
#: 判定语义：allow = 放行；deny = 拒绝且必须零外部写入；dual = 单签放行但
#: 审批链未满足（``satisfied=false``），需第二人才能继续。
EXPECTED: dict[tuple[str, str], tuple[str, int]] = {
    # 发起人：可读、可执行自己的写计划、可回滚自己发起的工单；不得自审。
    ("initiator", "read"): ("allow", 200),
    ("initiator", "write"): ("allow", 200),
    ("initiator", "approve"): ("deny", 409),
    ("initiator", "reject"): ("deny", 409),
    ("initiator", "rollback"): ("allow", 200),
    ("initiator", "export"): ("deny", 403),
    ("initiator", "images"): ("allow", 200),
    # 审批人：只落审批意见；执行写计划必须由发起人提交。
    ("approver", "read"): ("allow", 200),
    ("approver", "write"): ("deny", 403),
    ("approver", "approve"): ("dual", 200),
    ("approver", "reject"): ("allow", 200),
    ("approver", "rollback"): ("deny", 403),
    ("approver", "export"): ("deny", 403),
    ("approver", "images"): ("allow", 200),
    # 审计员：只读；审批/驳回/回滚/写一律拒绝。
    ("auditor", "read"): ("allow", 200),
    ("auditor", "write"): ("deny", 403),
    ("auditor", "approve"): ("deny", 409),
    ("auditor", "reject"): ("deny", 409),
    ("auditor", "rollback"): ("deny", 403),
    ("auditor", "export"): ("allow", 200),
    ("auditor", "images"): ("allow", 200),
    # 平台管理员：可读可导出；不得参与业务复核，也不能替发起人执行写计划。
    ("admin", "read"): ("allow", 200),
    ("admin", "write"): ("deny", 403),
    ("admin", "approve"): ("deny", 409),
    ("admin", "reject"): ("deny", 409),
    ("admin", "rollback"): ("deny", 403),
    ("admin", "export"): ("allow", 200),
    ("admin", "images"): ("allow", 200),
    # 服务账号：无业务身份，不得读工单、看影像、导出审计（C2 收口）。
    ("service_account", "read"): ("deny", 403),
    ("service_account", "write"): ("deny", 403),
    ("service_account", "approve"): ("deny", 409),
    ("service_account", "reject"): ("deny", 409),
    ("service_account", "rollback"): ("deny", 403),
    ("service_account", "export"): ("deny", 403),
    ("service_account", "images"): ("deny", 403),
}

#: 期望值已按 C1/C2 收口，由 ``apps/api/service.py`` 的服务层闸口保证。


@pytest.fixture
def env():
    environment = build_env()
    try:
        yield environment
    finally:
        teardown_env()


def _expected(role: str, op: str, ownership: str) -> tuple[str, int]:
    if ownership != "same_org":
        return ("deny", 403)
    return EXPECTED[(role, op)]


def _call(env: MatrixEnv, role: str, op: str, ownership: str):
    ticket_id = env.ticket(KIND_FOR_OP[op], ownership)
    idem = f"idem-{role}-{op}-{ownership}"

    if op == "read":
        return env.client.get(
            f"{PREFIX}/tickets/{ticket_id}",
            params=env.scope_params(),
            headers=env.headers(role),
        )
    if op == "write":
        return env.client.post(
            f"{PREFIX}/tickets/{ticket_id}/actions/approve",
            json={"scope": env.scope(), "comment": "矩阵：执行写计划"},
            headers=env.headers(role, idem),
        )
    if op == "approve":
        return env.client.post(
            f"{PREFIX}/approvals/{ticket_id}/approve",
            json={"scope": env.scope(), "role": "approver", "comment": "矩阵：审批"},
            headers=env.headers(role, idem),
        )
    if op == "reject":
        return env.client.post(
            f"{PREFIX}/approvals/{ticket_id}/reject",
            json={"scope": env.scope(), "role": "approver", "comment": "矩阵：驳回"},
            headers=env.headers(role, idem),
        )
    if op == "rollback":
        return env.client.post(
            f"{PREFIX}/tickets/{ticket_id}/actions/compensate",
            json={"scope": env.scope(), "comment": "矩阵：回滚", "fail_code": "F2"},
            headers=env.headers(role, idem),
        )
    if op == "export":
        return env.client.get(
            f"{PREFIX}/audit/export",
            params=env.scope_params() + [("ticket_id", str(ticket_id))],
            headers=env.headers(role),
        )
    if op == "images":
        return env.client.get(
            f"{PREFIX}/audit/tickets/{ticket_id}/images",
            params=env.scope_params(),
            headers=env.headers(role),
        )
    raise AssertionError(f"未知操作 {op!r}")


def _matrix_cases() -> list:
    cases = []
    for role in ROLES:
        for op in OPS:
            for ownership in OWNERSHIPS:
                verdict, status = _expected(role, op, ownership)
                cases.append(
                    pytest.param(
                        role,
                        op,
                        ownership,
                        verdict,
                        status,
                        id=f"{role}__{op}__{ownership}",
                    )
                )
    return cases


@pytest.mark.parametrize("role,op,ownership,verdict,status", _matrix_cases())
def test_permission_matrix_cell(env, role, op, ownership, verdict, status):
    """矩阵单格：判定与 HTTP 状态；拒绝必须发生在连接器之前。"""

    response = _call(env, role, op, ownership)

    assert response.status_code == status, (
        f"{role}/{op}/{ownership} 期望 {status}，实际 {response.status_code}：{response.text}"
    )

    if verdict == "dual":
        body = response.json()
        assert body["satisfied"] is False
        assert body["missing_roles"], "双人复核未满足时必须明确缺少哪个角色"

    if verdict == "deny":
        assert response.headers["content-type"].startswith("application/problem+json")
        assert env.connector.writes == []
        assert env.connector.previews == []
        assert env.connector_b.writes == []

    if verdict == "allow" and op in {"write", "rollback"}:
        assert env.connector.writes, "放行的写/回滚必须真实经过 WriteGateway"


# --------------------------------------------------------------------------- B1


def test_b1_self_approval_rejected_at_service_layer(env):
    """接口层：发起人不得审批自己的工单（FR-1403）。"""

    session = env.container.session_directory.resolve(env.tokens["initiator"])
    with pytest.raises(SelfApprovalRejected):
        env.container.approvals.record(
            ticket_id="B1-SELF",
            role="reviewer",
            approver=principal_of(session),
            initiator_id=session.user_id,
            decision="approved",
            comment="自己批自己",
        )


def test_b1_self_approval_db_check_and_unique_constraints():
    """DB 层兜底：CHECK 审批人 ≠ 发起人；UNIQUE 同一人不得在同一工单留两条记录。"""

    ddl = str(
        CreateTable(ApprovalRecordRow.__table__).compile(dialect=postgresql.dialect())
    )
    assert "CHECK (approver_id <> initiator_id)" in ddl
    assert "UNIQUE (ticket_id, approver_id)" in ddl


# --------------------------------------------------------------------------- B2


def test_b2_forged_org_headers_cannot_widen_scope(env):
    """伪造 org 请求头不得改变可见范围：结果只含本组织工单。"""

    forged = {
        **env.headers("initiator"),
        "X-Org-Id": ORG2,
        "X-Org": ORG2,
        "X-Tenant-Id": str(
            env.container.session_directory.resolve(env.tokens["initiator"]).tenant_id
        ),
    }
    response = env.client.get(
        f"{PREFIX}/tickets", params=env.scope_params(), headers=forged
    )

    assert response.status_code == 200, response.text
    ticket_nos = {item["ticket_no"] for item in response.json()["items"]}
    assert "T-RD" in ticket_nos, "本组织工单必须可见"
    assert all(not ticket_no.endswith("cross_org") for ticket_no in ticket_nos)


def test_b2_forged_org_param_is_rejected(env):
    """把 org 直接写进查询参数/请求体（等价于伪造 org 头）→ 403。"""

    read = env.client.get(
        f"{PREFIX}/tickets",
        params=env.scope_params(org_id=ORG2),
        headers={**env.headers("initiator"), "X-Org-Id": ORG2},
    )
    assert read.status_code == 403
    assert read.json()["code"] == "E-AUTHZ-001"

    write = env.client.post(
        f"{PREFIX}/tickets/{env.ticket('write', 'cross_org')}/actions/approve",
        json={"scope": env.scope(org_id=ORG2), "comment": "伪造 org 执行"},
        headers={
            **env.headers("initiator", "idem-b2-org"),
            "X-Org-Id": ORG2,
        },
    )
    assert write.status_code == 403
    assert write.json()["code"] == "E-AUTHZ-001"
    assert env.connector.writes == []
    assert env.connector.previews == []


def test_b2_cross_period_uses_same_deny_path(env):
    """跨批次（跨期间）与跨组织同口径：资源级 scope 校验直接拒绝。"""

    response = env.client.get(
        f"{PREFIX}/tickets/{env.ticket('read', 'cross_period')}",
        params=env.scope_params(period=PERIOD2),
        headers=env.headers("initiator"),
    )
    assert response.status_code == 403
    assert response.json()["code"] == "E-AUTHZ-001"

    # 请求本期间、资源在另一期间，同样拒绝（不是靠前端过滤）。
    other = env.client.get(
        f"{PREFIX}/tickets/{env.ticket('read', 'cross_period')}",
        params=env.scope_params(),
        headers=env.headers("auditor"),
    )
    assert other.status_code == 403


# --------------------------------------------------------------------------- B3


def _b_request(env: MatrixEnv, *, level=WriteLevel.W1, whitelist=frozenset({"draft_id"})):
    session = env.container.session_directory.resolve(env.tokens["service_account"])
    return (
        session,
        Scope(ORG, PERIOD, (SYSTEM_B,)),
        level,
        whitelist,
    )


def _b_access_request(session, scope, level, whitelist):
    from apps.security.gate import AccessRequest

    return AccessRequest(
        session=session,
        requested_scope=scope,
        connector_meta=meta_b(),
        ticket_id="B3-LEASE",
        level=level,
        amount=Decimal("880.00"),
        field_whitelist=whitelist,
    )


def test_b3_b_level_requires_field_whitelist(env):
    """B 级连接器缺字段白名单 → 身份闸直接拒绝（fail-closed，不降级放大）。"""

    session, scope, level, _ = _b_request(env, whitelist=frozenset())
    with pytest.raises(MissingFieldWhitelist):
        env.container.authorize(_b_access_request(session, scope, level, frozenset()))


def test_b3_b_level_caps_at_w1(env):
    session, scope, _, whitelist = _b_request(env, level=WriteLevel.W2)
    with pytest.raises(LevelNotAllowed):
        env.container.authorize(
            _b_access_request(session, scope, WriteLevel.W2, whitelist)
        )


def test_b3_b_level_forces_dual_review_and_records_degradation(env):
    """B 级：单签不满足，必须两个不同审批人；租约留降级痕迹且最高 W1。"""

    session, scope, level, whitelist = _b_request(env)
    request = _b_access_request(session, scope, level, whitelist)

    with pytest.raises(ApprovalNotSatisfied):
        env.container.authorize(request)

    env.container.approvals.record(
        ticket_id="B3-LEASE",
        role="approver",
        approver=principal_of(
            env.container.session_directory.resolve(env.tokens["approver"])
        ),
        initiator_id=SERVICE,
        decision="approved",
        comment="B 级：第一签",
    )
    with pytest.raises(ApprovalNotSatisfied):
        env.container.authorize(request)

    env.container.approvals.record(
        ticket_id="B3-LEASE",
        role="reviewer",
        approver=principal_of(
            env.container.session_directory.resolve(env.tokens["reviewer"])
        ),
        initiator_id=SERVICE,
        decision="approved",
        comment="B 级：第二签",
    )
    grant = env.container.authorize(request)

    assert grant.lease.mode == "service_account"
    assert grant.lease.requires_dual_review is True
    assert grant.lease.max_level is WriteLevel.W1
    assert grant.lease.narrowed_fields == whitelist
    assert grant.lease.degradation is not None
    assert env.container.broker.degradations


def test_c4_b_level_ticket_completes_intent_to_verify_over_http(env):
    """C4 目标行为：B 级工单经 HTTP 跑完 intent→verify。

    双签满足 → API 传字段白名单 → 租约收窄字段 → 真实预演、写入、校验各一次。
    """

    ticket_id = env.ticket("b_level", "same_org")
    initiator = principal_of(
        env.container.session_directory.resolve(env.tokens["service_account"])
    )
    env.container.approvals.record(
        ticket_id=str(ticket_id),
        role="approver",
        approver=principal_of(
            env.container.session_directory.resolve(env.tokens["approver"])
        ),
        initiator_id=SERVICE,
        decision="approved",
        comment="C4：第一签",
    )
    env.container.approvals.record(
        ticket_id=str(ticket_id),
        role="reviewer",
        approver=principal_of(
            env.container.session_directory.resolve(env.tokens["reviewer"])
        ),
        initiator_id=initiator.user_id,
        decision="approved",
        comment="C4：第二签",
    )

    response = env.client.post(
        f"{PREFIX}/tickets/{ticket_id}/actions/approve",
        json={"scope": env.scope(system_ids=[SYSTEM_B]), "comment": "C4：执行 B 级写计划"},
        headers=env.headers("service_account", "idem-c4-e2e"),
    )

    assert response.status_code == 200, response.text
    assert [write[0] for write in env.connector_b.writes] == ["mes.wo.save"]
    assert env.connector_b.verifies, "B 级写入后必须完成校验"
    assert env.connector_b.previews, "写入前必须预演"


class _AllowAll:
    def is_satisfied(self, ticket_id: str, level: WriteLevel) -> bool:
        return True


def test_b3_narrowed_fields_reject_out_of_whitelist_payload(env):
    """修复后：租约白名单外的字段一律拒写，且拒绝发生在预演之前。"""

    session, scope, level, whitelist = _b_request(env)
    request = _b_access_request(session, scope, level, whitelist)
    env.container.approvals.record(
        ticket_id="B3-LEASE",
        role="approver",
        approver=principal_of(
            env.container.session_directory.resolve(env.tokens["approver"])
        ),
        initiator_id=SERVICE,
        decision="approved",
        comment="B 级：第一签",
    )
    env.container.approvals.record(
        ticket_id="B3-LEASE",
        role="reviewer",
        approver=principal_of(
            env.container.session_directory.resolve(env.tokens["reviewer"])
        ),
        initiator_id=SERVICE,
        decision="approved",
        comment="B 级：第二签",
    )
    grant = env.container.authorize(request)
    assert grant.lease.narrowed_fields == frozenset({"draft_id"})

    action = env.container.actions.get("mes.wo.save")
    gateway = WriteGateway(
        env.connector_b,
        approvals=_AllowAll(),
        narrowed_fields=grant.lease.narrowed_fields,
    )
    with pytest.raises(FieldScopeViolation):
        asyncio.run(
            gateway.execute(
                action,
                {"draft_id": "D-B3", "note": "白名单外的字段"},
                ticket_id="B3-LEASE",
                idem_key="idem-b3-narrow",
                scene=SceneSpec(code="ag2_processing_fee", max_level=WriteLevel.W2),
            )
        )

    assert env.connector_b.previews == []
    assert env.connector_b.writes == []


# --------------------------------------------------------------------------- B4


def test_b4_w3_is_denied_for_every_role(env):
    """W3 在身份/范围/审批之前就被拒；5 类角色无一例外。"""

    for role in ROLES:
        session = env.container.session_directory.resolve(env.tokens[role])
        request = _w3_request(session)
        with pytest.raises(W3AccessDenied):
            env.container.authorize(request)

    assert env.connector.previews == []
    assert env.connector.writes == []


def _w3_request(session):
    from apps.security.gate import AccessRequest

    return AccessRequest(
        session=session,
        requested_scope=Scope(ORG, PERIOD, (SYSTEM,)),
        connector_meta=meta_a(),
        ticket_id="T-W3",
        level=WriteLevel.W3,
        amount=Decimal("100.00"),
    )


def test_b4_w3_api_path_is_rejected_without_side_effect(env):
    response = env.client.post(
        f"{PREFIX}/tickets/{env.ticket('w3', 'same_org')}/actions/approve",
        json={"scope": env.scope(), "comment": "尝试执行 W3"},
        headers=env.headers("initiator", "idem-b4-api"),
    )

    assert response.status_code == 403
    assert response.json()["code"] == "E-POLICY-W3FORBIDDEN"
    assert env.connector.writes == []
    assert env.connector.previews == []


# --------------------------------------------------------------------------- C2


def _images(env: MatrixEnv, ticket_id) -> list[dict]:
    response = env.client.get(
        f"{PREFIX}/audit/tickets/{ticket_id}/images",
        params=env.scope_params(),
        headers=env.headers("auditor"),
    )
    assert response.status_code == 200, response.text
    return response.json()["items"]


def test_c2_export_writes_one_export_audit_record(env):
    """导出必须留痕：每次成功导出恰好追加一条 ``audit:export`` 影像。"""

    ticket_id = env.ticket("rolled", "same_org")
    before = _images(env, ticket_id)

    response = env.client.get(
        f"{PREFIX}/audit/export",
        params=env.scope_params() + [("ticket_id", str(ticket_id))],
        headers=env.headers("auditor"),
    )
    assert response.status_code == 200, response.text

    after = _images(env, ticket_id)
    assert len(after) == len(before) + 1, "导出没有留下审计记录"
    assert after[-1]["object_type"] == "audit:export"
    assert after[-1]["object_id"].startswith("audit-")


def test_c2_export_is_refused_when_audit_append_fails(env, monkeypatch):
    """审计写入失败 → 拒绝导出：返回 problem+json，且不产出任何文件内容。"""

    from apps.audit.store import AuditStoreError

    async def _boom(**_kwargs):
        raise AuditStoreError("注入故障：审计存储不可写")

    monkeypatch.setattr(env.container.ledger, "append_image", _boom)

    response = env.client.get(
        f"{PREFIX}/audit/export",
        params=env.scope_params()
        + [("ticket_id", str(env.ticket("rolled", "same_org")))],
        headers=env.headers("auditor"),
    )

    assert response.status_code == 409, response.text
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["code"] == "E-STATE-ILLEGAL"
    assert "content-disposition" not in {key.lower() for key in response.headers}
    assert not response.content.startswith(b"\xef\xbb\xbfimage_id,")


def test_b4_write_gateway_has_no_w3_branch(env):
    """即使伪造一个 W3 ActionSpec，写闸门也没有执行分支。"""

    w3_action = ActionSpec(
        key="u8.pay.execute",
        level=WriteLevel.W3,
        writable_fields=frozenset({"amount"}),
        verify=VerifySpec(kind="read_by_external_id", fields=("payment_id",)),
        rate_limit=RateLimit(),
    )
    gateway = WriteGateway(env.connector)

    with pytest.raises(ForbiddenWriteError):
        asyncio.run(
            gateway.execute(
                w3_action,
                {"amount": "1.00"},
                ticket_id="T-W3",
                idem_key="idem-b4-gateway",
                scene=SceneSpec(code="ag2_processing_fee", max_level=WriteLevel.W3),
            )
        )
    assert env.connector.previews == []


def test_b4_w3_is_not_approvable(env):
    """审批链也不存在 W3 分支：任何角色都无法把 W3 批成可执行。"""

    with pytest.raises(W3NotApprovable):
        ApprovalService().required_roles(
            level=WriteLevel.W3, amount=Decimal("100.00"), touches_fund=False
        )
