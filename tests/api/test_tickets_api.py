"""工单路由：列表/详情、审批执行、驳回、终止、补偿、幂等。"""

from __future__ import annotations

import asyncio

from conftest import PREFIX, SYSTEM, TENANT

from apps.api.store import TicketStepRecord
from apps.platform.policy.levels import WriteLevel


def _decision(env, ticket: str, *, role: str = "reviewer", who: str = "reviewer", key: str = "k-review"):
    return env.client.post(
        f"{PREFIX}/approvals/{env.tickets[ticket]}/approve",
        json={"scope": env.scope(), "role": role, "comment": "同意"},
        headers=env.headers(who, key),
    )


def _execute(env, ticket: str, *, key: str = "k-exec", comment: str = "审批通过并执行"):
    return env.client.post(
        f"{PREFIX}/tickets/{env.tickets[ticket]}/actions/approve",
        json={"scope": env.scope(), "comment": comment},
        headers=env.headers("initiator", key),
    )


def test_list_tickets_filters_by_scope_and_paginates(env):
    url = f"{PREFIX}/tickets"

    first = env.client.get(
        url, params=env.scope_params() + [("limit", "2")], headers=env.headers()
    )
    assert first.status_code == 200, first.text
    body = first.json()
    assert len(body["items"]) == 2
    assert body["next_cursor"]

    second = env.client.get(
        url,
        params=env.scope_params() + [("limit", "2"), ("cursor", body["next_cursor"])],
        headers=env.headers(),
    )
    assert second.status_code == 200, second.text
    second_body = second.json()
    first_ids = {item["ticket_id"] for item in body["items"]}
    assert first_ids.isdisjoint({item["ticket_id"] for item in second_body["items"]})

    by_no = env.client.get(
        url,
        params=env.scope_params() + [("ticket_no", "T-1002")],
        headers=env.headers(),
    )
    assert by_no.status_code == 200
    assert [item["ticket_no"] for item in by_no.json()["items"]] == ["T-1002"]

    # 其他组织的工单不得出现在授权 scope 的结果里。
    assert env.tickets["other_org"] not in {
        item["ticket_id"] for item in second_body["items"]
    }


def test_get_ticket_detail_shows_required_and_missing_roles(env):
    response = env.client.get(
        f"{PREFIX}/tickets/{env.tickets['normal']}",
        params=env.scope_params(),
        headers=env.headers(),
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["state"] == "待审批"
    assert body["write_level"] == "W1"
    assert body["approval_required_roles"] == ["reviewer"]
    assert body["approval_missing_roles"] == ["reviewer"]
    assert body["steps"] == []


def test_approve_executes_pending_write_through_gateway(env):
    decision = _decision(env, "normal")
    assert decision.status_code == 200, decision.text
    assert decision.json()["satisfied"] is True

    executed = _execute(env, "normal")
    assert executed.status_code == 200, executed.text
    body = executed.json()
    assert body["state"] == "执行中"
    assert len(body["executed_steps"]) == 1
    assert body["executed_steps"][0]["action_key"] == "u8.trade.save_draft"
    assert env.connector.previews and env.connector.writes
    assert env.connector.writes[0][0] == "u8.trade.save_draft"
    assert env.connector.writes[0][3] == "idem-t1001"

    detail = env.client.get(
        f"{PREFIX}/tickets/{env.tickets['normal']}",
        params=env.scope_params(),
        headers=env.headers(),
    )
    assert detail.status_code == 200
    detail_body = detail.json()
    assert detail_body["state"] == "执行中"
    assert detail_body["audit_image_count"] == 1
    assert len(detail_body["approvals"]) == 1
    assert detail_body["approval_missing_roles"] == []


def test_approve_is_idempotent_and_rejects_key_reuse_with_new_body(env):
    assert _decision(env, "normal").status_code == 200
    first = _execute(env, "normal", key="k-exec")
    assert first.status_code == 200
    assert len(env.connector.writes) == 1

    replay = _execute(env, "normal", key="k-exec")
    assert replay.status_code == 200
    assert replay.json() == first.json()
    assert len(env.connector.writes) == 1

    conflict = _execute(env, "normal", key="k-exec", comment="换个请求体")
    assert conflict.status_code == 409
    assert conflict.json()["code"] == "E-IDEM-CONFLICT"
    assert len(env.connector.writes) == 1


def test_write_without_idempotency_key_is_rejected_before_any_effect(env):
    response = env.client.post(
        f"{PREFIX}/tickets/{env.tickets['reject']}/actions/reject",
        json={"scope": env.scope(), "role": "reviewer", "comment": "不同意"},
        headers=env.headers("reviewer"),
    )

    assert response.status_code == 400
    assert response.json()["code"] == "E-IDEM-CONFLICT"
    assert env.connector.writes == []


def test_role_fields_come_from_the_same_rule_on_a_level_connector(env):
    """A 级连接器不触发强制双签：同一规则、无附加输入 → 两组字段相等。"""

    for ticket, expected in (("normal", ["reviewer"]), ("dual", ["reviewer", "approver"])):
        response = env.client.get(
            f"{PREFIX}/tickets/{env.tickets[ticket]}",
            params=env.scope_params(),
            headers=env.headers(),
        )
        assert response.status_code == 200, response.text
        body = response.json()
        # FakeConnector 是 A 级：force_dual=False，审批组与执行组必须一致。
        assert body["approval_required_roles"] == expected
        assert body["execution_required_roles"] == expected
        assert body["approval_missing_roles"] == body["execution_missing_roles"]


def test_reject_action_records_decision_and_terminates(env):
    response = env.client.post(
        f"{PREFIX}/tickets/{env.tickets['reject']}/actions/reject",
        json={"scope": env.scope(), "role": "reviewer", "comment": "金额不符"},
        headers=env.headers("reviewer", "k-reject"),
    )

    assert response.status_code == 200, response.text
    assert response.json()["state"] == "已驳回"
    assert env.connector.writes == []

    detail = env.client.get(
        f"{PREFIX}/tickets/{env.tickets['reject']}",
        params=env.scope_params(),
        headers=env.headers(),
    ).json()
    assert detail["state"] == "已驳回"
    assert detail["approvals"][0]["decision"] == "rejected"


def test_cancel_requires_comment_and_terminates_planned_ticket(env):
    url = f"{PREFIX}/tickets/{env.tickets['cancel']}/actions/cancel"

    missing = env.client.post(
        url,
        json={"scope": env.scope(), "comment": ""},
        headers=env.headers("initiator", "k-cancel-bad"),
    )
    assert missing.status_code == 422
    assert missing.json()["code"] == "E-DATA-MISSING"

    ok = env.client.post(
        url,
        json={"scope": env.scope(), "comment": "需求取消"},
        headers=env.headers("initiator", "k-cancel"),
    )
    assert ok.status_code == 200, ok.text
    assert ok.json()["state"] == "已终止"
    assert env.connector.writes == []


def test_compensate_rolls_back_through_gateway_and_keeps_audit(env):
    assert _decision(env, "normal").status_code == 200
    assert _execute(env, "normal").status_code == 200
    assert len(env.connector.writes) == 1

    response = env.client.post(
        f"{PREFIX}/tickets/{env.tickets['normal']}/actions/compensate",
        json={"scope": env.scope(), "comment": "步骤失败", "fail_code": "F1"},
        headers=env.headers("initiator", "k-compensate"),
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["state"] == "已回滚"
    assert [step["action_key"] for step in body["executed_steps"]] == [
        "u8.trade.save_draft"
    ]
    assert len(env.connector.writes) == 2
    assert env.connector.writes[1][0] == "u8.trade.delete_draft"
    assert env.connector.writes[1][3].endswith(":undo")

    images = env.client.get(
        f"{PREFIX}/audit/tickets/{env.tickets['normal']}/images",
        params=env.scope_params(),
        headers=env.headers(),
    ).json()["items"]
    assert len(images) == 2
    assert images[-1]["object_type"].endswith(":rollback")


def test_compensate_requires_fail_code_without_writing(env):
    assert _decision(env, "normal").status_code == 200
    assert _execute(env, "normal").status_code == 200

    response = env.client.post(
        f"{PREFIX}/tickets/{env.tickets['normal']}/actions/compensate",
        json={"scope": env.scope(), "comment": "未声明分类"},
        headers=env.headers("initiator", "k-compensate-bad"),
    )

    assert response.status_code == 422
    assert response.json()["code"] == "E-DATA-MISSING"
    assert len(env.connector.writes) == 1


def _seed_step(env, ticket: str, *, step_no: int, object_id: str) -> TicketStepRecord:
    step = TicketStepRecord(
        step_no=step_no,
        action_key="u8.trade.save_draft",
        system=SYSTEM,
        object_type="draft",
        object_id=object_id,
        level=WriteLevel.W1,
        idem_key=f"idem-{object_id}",
    )
    asyncio.run(
        env.container.tickets.record_step(
            tenant_id=TENANT,
            ticket_id=env.tickets[ticket],
            step=step,
            request_hash=f"hash-{object_id}",
        )
    )
    return step


def test_audit_image_count_counts_images_not_steps(env):
    """1 张影像 + 2 个步骤时详情必须显示 1；改回 ``len(ticket.steps)`` 即变红。

    审计影像的正源是 ``erp.audit_image`` 中被本工单步骤引用到的行，不是步骤数：
    这一步没有影像（执行失败 / 被驳回）时两者会分叉，而详情页同时展示
    「审计影像 N 张」与影像列表条数，任何分叉都会被观众当场看到。
    """

    first = _seed_step(env, "normal", step_no=1, object_id="D-IMG-1")
    _seed_step(env, "normal", step_no=2, object_id="D-NO-IMG-2")
    image = asyncio.run(
        env.container.ledger.append_image(
            tenant_id=TENANT,
            ticket_id=env.tickets["normal"],
            step_id=first.step_id,
            system=SYSTEM,
            object_type="draft",
            object_id="D-IMG-1",
            before={"qty": 1},
            after={"qty": 2},
        )
    )
    first.audit_image_id = image.id

    response = env.client.get(
        f"{PREFIX}/tickets/{env.tickets['normal']}",
        params=env.scope_params(),
        headers=env.headers(),
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert len(body["steps"]) == 2
    assert body["audit_image_count"] == 1

