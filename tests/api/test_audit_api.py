"""审计路由：按工单读取影像与哈希链、导出 CSV。"""

from __future__ import annotations

import hashlib

from conftest import PREFIX


def _prepare_executed(env) -> str:
    ticket_id = str(env.tickets["normal"])
    decision = env.client.post(
        f"{PREFIX}/approvals/{ticket_id}/approve",
        json={"scope": env.scope(), "role": "reviewer", "comment": "同意"},
        headers=env.headers("reviewer", "k-audit-review"),
    )
    assert decision.status_code == 200, decision.text
    executed = env.client.post(
        f"{PREFIX}/tickets/{ticket_id}/actions/approve",
        json={"scope": env.scope(), "comment": "执行"},
        headers=env.headers("initiator", "k-audit-exec"),
    )
    assert executed.status_code == 200, executed.text
    return ticket_id


def test_audit_images_and_chain_reflect_executed_and_rollback(env):
    ticket_id = _prepare_executed(env)
    compensated = env.client.post(
        f"{PREFIX}/tickets/{ticket_id}/actions/compensate",
        json={"scope": env.scope(), "comment": "失败", "fail_code": "F2"},
        headers=env.headers("initiator", "k-audit-comp"),
    )
    assert compensated.status_code == 200, compensated.text

    images = env.client.get(
        f"{PREFIX}/audit/tickets/{ticket_id}/images",
        params=env.scope_params() + [("system", "u8")],
        headers=env.headers(),
    )
    assert images.status_code == 200, images.text
    items = images.json()["items"]
    assert len(items) == 2
    assert items[0]["object_type"] == "u8.trade.save_draft"
    assert items[1]["object_type"] == "u8.trade.save_draft:rollback"
    assert isinstance(items[0]["field_diff"], list)
    assert items[0]["hash"]

    chain = env.client.get(
        f"{PREFIX}/audit/tickets/{ticket_id}/chain",
        params=env.scope_params(),
        headers=env.headers(),
    )
    assert chain.status_code == 200, chain.text
    chain_body = chain.json()
    assert chain_body["ok"] is True
    assert chain_body["image_count"] == 2
    assert chain_body["head_hash"] == items[-1]["hash"]
    assert chain_body["detail"] == ""


def test_audit_export_csv_digest_matches_content(env):
    ticket_id = _prepare_executed(env)

    response = env.client.get(
        f"{PREFIX}/audit/export",
        params=env.scope_params() + [("ticket_id", ticket_id)],
        headers=env.headers("auditor"),
    )

    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/csv")
    digest = hashlib.sha256(response.content).hexdigest()
    assert response.headers["X-Content-SHA256"] == digest
    text = response.content.decode("utf-8-sig")
    assert text.startswith("image_id,step_id,system,object_type")
    assert "u8.trade.save_draft" in text


def test_audit_is_scoped_and_empty_ticket_verifies(env):
    out_of_scope = env.client.get(
        f"{PREFIX}/audit/tickets/{env.tickets['other_org']}/chain",
        params=env.scope_params(),
        headers=env.headers(),
    )
    assert out_of_scope.status_code == 403
    assert out_of_scope.json()["code"] == "E-AUTHZ-001"

    empty = env.client.get(
        f"{PREFIX}/audit/tickets/{env.tickets['cancel']}/chain",
        params=env.scope_params(),
        headers=env.headers(),
    )
    assert empty.status_code == 200, empty.text
    assert empty.json()["ok"] is True
    assert empty.json()["image_count"] == 0
