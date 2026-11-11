"""同步回滚开关：关闭态必须明确拒绝、零连接器调用、工单状态不变。"""

from __future__ import annotations

from conftest import PREFIX

from apps.api.rollback_gate import (
    ENV_ROLLBACK_SYNC_ENABLED,
    sync_rollback_enabled,
)


def test_flag_defaults_to_disabled():
    assert sync_rollback_enabled({}) is False
    assert sync_rollback_enabled({ENV_ROLLBACK_SYNC_ENABLED: ""}) is False
    assert sync_rollback_enabled({ENV_ROLLBACK_SYNC_ENABLED: " false "}) is False
    assert sync_rollback_enabled({ENV_ROLLBACK_SYNC_ENABLED: "0"}) is False
    assert sync_rollback_enabled({ENV_ROLLBACK_SYNC_ENABLED: "true"}) is True
    assert sync_rollback_enabled({ENV_ROLLBACK_SYNC_ENABLED: "1"}) is True


def _prepare_executing(env):
    assert (
        env.client.post(
            f"{PREFIX}/approvals/{env.tickets['normal']}/approve",
            json={"scope": env.scope(), "role": "reviewer", "comment": "同意"},
            headers=env.headers("reviewer", "k-gate-approve"),
        ).status_code
        == 200
    )
    assert (
        env.client.post(
            f"{PREFIX}/tickets/{env.tickets['normal']}/actions/approve",
            json={"scope": env.scope(), "comment": "审批通过并执行"},
            headers=env.headers("initiator", "k-gate-exec"),
        ).status_code
        == 200
    )


def test_disabled_rejects_with_dedicated_code_and_zero_connector_calls(
    env, monkeypatch
):
    _prepare_executing(env)
    previews_before = len(env.connector.previews)
    writes_before = len(env.connector.writes)
    assert writes_before == 1

    monkeypatch.delenv(ENV_ROLLBACK_SYNC_ENABLED, raising=False)

    response = env.client.post(
        f"{PREFIX}/tickets/{env.tickets['normal']}/actions/compensate",
        json={"scope": env.scope(), "comment": "步骤失败", "fail_code": "F1"},
        headers=env.headers("initiator", "k-gate-compensate"),
    )

    assert response.status_code == 503, response.text
    body = response.json()
    assert body["code"] == "E-ROLLBACK-UNAVAILABLE"
    assert body["retriable"] is False
    assert "未调用任何连接器" in body["detail"]

    # 零调用：连预演都不该发生，否则说明拒绝点太靠后。
    assert len(env.connector.previews) == previews_before
    assert len(env.connector.writes) == writes_before

    detail = env.client.get(
        f"{PREFIX}/tickets/{env.tickets['normal']}",
        params=env.scope_params(),
        headers=env.headers(),
    )
    assert detail.status_code == 200, detail.text
    assert detail.json()["state"] == "执行中"
    assert detail.json()["rollback_available"] is False


def test_enabled_keeps_legacy_behaviour_and_advertises_availability(env):
    _prepare_executing(env)

    detail = env.client.get(
        f"{PREFIX}/tickets/{env.tickets['normal']}",
        params=env.scope_params(),
        headers=env.headers(),
    )
    assert detail.json()["rollback_available"] is True

    response = env.client.post(
        f"{PREFIX}/tickets/{env.tickets['normal']}/actions/compensate",
        json={"scope": env.scope(), "comment": "步骤失败", "fail_code": "F1"},
        headers=env.headers("initiator", "k-gate-on"),
    )
    assert response.status_code == 200, response.text
    assert response.json()["state"] == "已回滚"
