"""审批路由：待办 / 通过 / 驳回；审批本身不产生外部写入。"""

from __future__ import annotations

from uuid import UUID

import pytest

from conftest import PREFIX, SCENE, _principal


def test_pending_only_lists_tickets_the_user_can_approve(env):
    url = f"{PREFIX}/approvals/pending"

    reviewer = env.client.get(
        url, params=env.scope_params(), headers=env.headers("reviewer")
    )
    assert reviewer.status_code == 200, reviewer.text
    reviewer_ids = {item["ticket_id"] for item in reviewer.json()["items"]}
    assert str(env.tickets["normal"]) in reviewer_ids
    assert str(env.tickets["dual"]) in reviewer_ids
    assert str(env.tickets["w3"]) not in reviewer_ids  # W3 无审批通过路径

    initiator = env.client.get(
        url, params=env.scope_params(), headers=env.headers("initiator")
    )
    assert initiator.status_code == 200
    assert initiator.json()["items"] == []
    assert env.connector.writes == []


def test_approve_records_decision_without_executing(env):
    response = env.client.post(
        f"{PREFIX}/approvals/{env.tickets['normal']}/approve",
        json={"scope": env.scope(), "role": "reviewer", "comment": "复核通过"},
        headers=env.headers("reviewer", "k-approval"),
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["decision"] == "approved"
    assert body["satisfied"] is True
    assert body["ticket_state"] == "待审批"
    assert env.connector.writes == []

    detail = env.client.get(
        f"{PREFIX}/tickets/{env.tickets['normal']}",
        params=env.scope_params(),
        headers=env.headers(),
    ).json()
    assert detail["state"] == "待审批"
    assert detail["approval_missing_roles"] == []


def test_approve_reject_requires_comment_and_is_not_self_approvable(env):
    empty_comment = env.client.post(
        f"{PREFIX}/approvals/{env.tickets['normal']}/approve",
        json={"scope": env.scope(), "role": "reviewer", "comment": ""},
        headers=env.headers("reviewer", "k-empty-comment"),
    )
    assert empty_comment.status_code == 409
    assert empty_comment.json()["code"] == "E-POLICY-APPROVAL"

    duplicate = env.client.post(
        f"{PREFIX}/approvals/{env.tickets['normal']}/approve",
        json={"scope": env.scope(), "role": "reviewer", "comment": "第一次"},
        headers=env.headers("reviewer", "k-dup-1"),
    )
    assert duplicate.status_code == 200

    again = env.client.post(
        f"{PREFIX}/approvals/{env.tickets['normal']}/approve",
        json={"scope": env.scope(), "role": "reviewer", "comment": "第二次"},
        headers=env.headers("reviewer", "k-dup-2"),
    )
    assert again.status_code == 409
    assert again.json()["code"] == "E-POLICY-APPROVAL"
    assert env.connector.writes == []


def test_reject_decision_moves_ticket_to_rejected(env):
    response = env.client.post(
        f"{PREFIX}/approvals/{env.tickets['reject']}/reject",
        json={"scope": env.scope(), "role": "reviewer", "comment": "数量不符"},
        headers=env.headers("reviewer", "k-reject-decision"),
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["decision"] == "rejected"
    assert body["ticket_state"] == "已驳回"
    assert env.connector.writes == []


#: 无 finance 角色、但持有三种审批角色的身份（跨类别复核必须被拒）。
_CROSS_CATEGORY_APPROVER = UUID("99999999-9999-9999-9999-999999999999")


@pytest.mark.parametrize("action", ["approve", "reject"])
def test_cross_category_decision_is_rejected_by_category_gate(env, action):
    """finance 类场景只允许持 finance 角色的复核/审批人落票。

    拒因必须来自业务类别闸：除状态码外，detail 要指出场景与类别；
    且不得落下审批记录（否则等于「报错但已写」）。
    """

    token = env.container.session_directory.issue(
        env.container.broker,
        _principal(
            _CROSS_CATEGORY_APPROVER,
            frozenset({"reviewer", "approver", "cfo"}),
        ),
    )
    ticket_id = env.tickets["normal"]
    response = env.client.post(
        f"{PREFIX}/approvals/{ticket_id}/{action}",
        json={"scope": env.scope(), "role": "approver", "comment": "越类审批"},
        headers={
            "X-Session-Token": token,
            "Idempotency-Key": f"k-cross-category-{action}",
        },
    )

    assert response.status_code == 403, response.text
    body = response.json()
    assert body["code"] == "E-AUTHZ-001"
    assert "不能复核 finance 类场景" in body["detail"], body
    assert SCENE in body["detail"], body
    assert env.container.approvals.records(str(ticket_id)) == ()
    assert env.connector.writes == []

