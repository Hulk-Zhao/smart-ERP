"""三类写拒绝：W3、越权 scope、审批未满足；每类都断言连接器零写入。"""

from __future__ import annotations

from conftest import PREFIX


def _assert_problem(response, *, status: int, code: str) -> None:
    assert response.status_code == status, response.text
    assert response.headers["content-type"].startswith("application/problem+json")
    body = response.json()
    assert body["code"] == code
    assert body["trace_id"] == response.headers["X-Trace-ID"]


def test_w3_action_is_rejected_before_connector(env):
    response = env.client.post(
        f"{PREFIX}/tickets/{env.tickets['w3']}/actions/approve",
        json={"scope": env.scope(), "comment": "尝试执行资金动作"},
        headers=env.headers("initiator", "k-w3"),
    )

    _assert_problem(response, status=403, code="E-POLICY-W3FORBIDDEN")
    assert "W3" in response.json()["detail"]
    assert env.connector.writes == []
    assert env.connector.previews == []


def test_scope_escalation_is_rejected_for_read_and_write(env):
    read = env.client.get(
        f"{PREFIX}/tickets",
        params=env.scope_params(org_id="ORG-2"),
        headers=env.headers(),
    )
    _assert_problem(read, status=403, code="E-AUTHZ-001")

    write = env.client.post(
        f"{PREFIX}/tickets/{env.tickets['normal']}/actions/approve",
        json={"scope": env.scope(org_id="ORG-2"), "comment": "越权执行"},
        headers=env.headers("initiator", "k-scope"),
    )
    _assert_problem(write, status=403, code="E-AUTHZ-001")
    assert env.connector.writes == []
    assert env.connector.previews == []


def test_approval_not_satisfied_blocks_execution(env):
    # 大额 W2 需要 reviewer + approver 两个不同审批人；这里只落一个。
    seed = env.client.post(
        f"{PREFIX}/approvals/{env.tickets['dual']}/approve",
        json={"scope": env.scope(), "role": "reviewer", "comment": "复核通过"},
        headers=env.headers("reviewer", "k-dual-review"),
    )
    assert seed.status_code == 200, seed.text
    assert seed.json()["satisfied"] is False
    assert seed.json()["missing_roles"] == ["approver"]

    response = env.client.post(
        f"{PREFIX}/tickets/{env.tickets['dual']}/actions/approve",
        json={"scope": env.scope(), "comment": "尝试执行"},
        headers=env.headers("initiator", "k-dual-exec"),
    )

    _assert_problem(response, status=409, code="E-POLICY-APPROVAL")
    assert env.connector.writes == []
    assert env.connector.previews == []


def test_self_approval_is_rejected_without_side_effect(env):
    response = env.client.post(
        f"{PREFIX}/approvals/{env.tickets['normal']}/approve",
        json={"scope": env.scope(), "role": "reviewer", "comment": "自己批自己"},
        headers=env.headers("initiator", "k-self-review"),
    )

    _assert_problem(response, status=409, code="E-POLICY-APPROVAL")
    assert env.connector.writes == []
