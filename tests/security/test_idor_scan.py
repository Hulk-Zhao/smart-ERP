"""跨租户 / IDOR 全端点扫描（17 条路径）。

威胁模型：攻击者持有**租户 A** 的合法会话，且其授权范围（org/period/system）
与受害者资源完全一致；唯一差别是资源属于**租户 B**。任何泄露都只能归因于
缺少租户校验，而不是 scope 校验。

判定口径（用户验收口径）：
- 资源型端点：必须 403 或 404，不得 200、不得 500；
- 列表型端点：可以 200，但不得出现另一个租户的行；
- 每条探测都断言连接器 ``previews == [] and writes == []``，拒绝必须发生在写闸门之前。

本文件只加测试，不改 ``apps/api``。已知缺口按用户要求「先报不改」，
以 skip 保留目标行为，理由写明当前实测状态；``service.py`` 放行后修复再放开。
"""

from __future__ import annotations

import statistics
import time
from uuid import uuid4

import pytest
from idor_support import (
    FORGED_HEADERS,
    IdorEnv,
    build_env,
    teardown_env,
)
from matrix_support import ORG, PERIOD, PREFIX, SYSTEM


@pytest.fixture
def env() -> IdorEnv:
    built = build_env()
    try:
        yield built
    finally:
        teardown_env()


def _params(env: IdorEnv) -> list[tuple[str, str]]:
    return [("org_id", ORG), ("period", PERIOD), ("system_ids", SYSTEM)]


def _probes(env: IdorEnv) -> dict[str, object]:
    """17 条路径 → 18 个探测（计量表 json / csv 各一次）。"""

    scope = env.scope()
    foreign = str(env.tickets["foreign"])
    foreign_exec = str(env.tickets["foreign_exec"])
    batch = str(env.batch_b)
    return {
        "tickets_list": lambda: env.client.get(
            f"{PREFIX}/tickets", params=_params(env), headers=env.headers("operator")
        ),
        "ticket_detail": lambda: env.client.get(
            f"{PREFIX}/tickets/{foreign}",
            params=_params(env),
            headers=env.headers("operator"),
        ),
        "ticket_approve": lambda: env.client.post(
            f"{PREFIX}/tickets/{foreign}/actions/approve",
            json={"scope": scope, "comment": "idor"},
            headers=env.headers("operator", "idor-approve"),
        ),
        "ticket_reject": lambda: env.client.post(
            f"{PREFIX}/tickets/{foreign}/actions/reject",
            json={"scope": scope, "role": "reviewer", "comment": "idor"},
            headers=env.headers("reviewer", "idor-reject"),
        ),
        "ticket_cancel": lambda: env.client.post(
            f"{PREFIX}/tickets/{foreign}/actions/cancel",
            json={"scope": scope, "comment": "idor"},
            headers=env.headers("operator", "idor-cancel"),
        ),
        "ticket_compensate": lambda: env.client.post(
            f"{PREFIX}/tickets/{foreign_exec}/actions/compensate",
            json={"scope": scope, "comment": "idor", "fail_code": "F1"},
            headers=env.headers("operator", "idor-compensate"),
        ),
        "approvals_pending": lambda: env.client.get(
            f"{PREFIX}/approvals/pending",
            params=_params(env),
            headers=env.headers("reviewer"),
        ),
        "approvals_approve": lambda: env.client.post(
            f"{PREFIX}/approvals/{foreign}/approve",
            json={"scope": scope, "role": "reviewer", "comment": "idor"},
            headers=env.headers("reviewer", "idor-ap-approve"),
        ),
        "approvals_reject": lambda: env.client.post(
            f"{PREFIX}/approvals/{foreign}/reject",
            json={"scope": scope, "role": "reviewer", "comment": "idor"},
            headers=env.headers("reviewer", "idor-ap-reject"),
        ),
        "audit_images": lambda: env.client.get(
            f"{PREFIX}/audit/tickets/{foreign_exec}/images",
            params=_params(env),
            headers=env.headers("operator"),
        ),
        "audit_chain": lambda: env.client.get(
            f"{PREFIX}/audit/tickets/{foreign_exec}/chain",
            params=_params(env),
            headers=env.headers("operator"),
        ),
        "audit_export": lambda: env.client.get(
            f"{PREFIX}/audit/export",
            params=[*_params(env), ("ticket_id", foreign_exec)],
            headers=env.headers("operator"),
        ),
        "recon_run": lambda: env.client.get(
            f"{PREFIX}/recon/runs/{batch}",
            params=_params(env),
            headers=env.headers("operator"),
        ),
        "recon_diffs": lambda: env.client.get(
            f"{PREFIX}/recon/runs/{batch}/diffs",
            params=_params(env),
            headers=env.headers("operator"),
        ),
        "recon_diff_detail": lambda: env.client.get(
            f"{PREFIX}/recon/runs/{batch}/diffs/0",
            params=_params(env),
            headers=env.headers("operator"),
        ),
        "metering_statement": lambda: env.client.get(
            f"{PREFIX}/metering/batches/{batch}/statement",
            params=_params(env),
            headers=env.headers("operator"),
        ),
        "metering_csv": lambda: env.client.get(
            f"{PREFIX}/metering/batches/{batch}/statement",
            params=[*_params(env), ("format", "csv")],
            headers=env.headers("operator"),
        ),
    }


LIST_PROBES = ("tickets_list", "approvals_pending")
RESOURCE_PROBES = (
    "ticket_detail",
    "ticket_approve",
    "ticket_reject",
    "ticket_cancel",
    "ticket_compensate",
    "approvals_approve",
    "approvals_reject",
    "audit_images",
    "audit_chain",
    "audit_export",
    "recon_run",
    "recon_diffs",
    "recon_diff_detail",
    "metering_statement",
    "metering_csv",
)

#: 跨租户探测清单：每条都必须 404（列表型则不得出现他租户行）。
CROSS_TENANT_PROBES = {
    "tickets_list": "列表不得含租户 B 工单",
    "ticket_detail": "按 (tenant_id, ticket_id) 查不到 → 404",
    "ticket_reject": "他租户工单不可驳回",
    "ticket_cancel": "他租户工单不可终止",
    "approvals_pending": "待办列表不得含他租户工单",
    "approvals_approve": "他租户工单不可落审批",
    "approvals_reject": "他租户工单不可驳回",
    "audit_images": "他租户影像不可读",
    "audit_chain": "他租户审计链不可读",
    "audit_export": "他租户审计不可导出",
}


def _run(env: IdorEnv, name: str):
    response = _probes(env)[name]()
    assert env.connector.previews == [], f"{name} 越权请求产生了预演"
    assert env.connector.writes == [], f"{name} 越权请求产生了写入"
    return response


@pytest.mark.parametrize("probe", sorted(CROSS_TENANT_PROBES))
def test_cross_tenant_access_is_denied(env: IdorEnv, probe: str) -> None:
    """跨租户一律拒绝：资源型 404（不用 403 泄露存在性），列表型不得含他租户行。"""

    response = _run(env, probe)
    if probe in LIST_PROBES:
        assert response.status_code == 200
        assert "T-B" not in response.text, "列表泄露了另一租户的工单"
    else:
        assert response.status_code == 404, response.text


@pytest.mark.parametrize(
    "probe",
    [
        "ticket_approve",
        "ticket_compensate",
        "recon_run",
        "recon_diffs",
        "recon_diff_detail",
        "metering_statement",
        "metering_csv",
    ],
)
def test_cross_tenant_resource_is_denied(env: IdorEnv, probe: str) -> None:
    """资源型端点：租户不匹配一律 404，且连接器零预演零写入。"""

    response = _run(env, probe)
    assert response.status_code == 404, response.text


def test_cross_tenant_ticket_detail_rejects_forged_tenant_headers(env: IdorEnv) -> None:
    """伪造 ``X-Org-Id`` / ``X-Tenant-Id`` / ``X-Org`` 不得改变租户判定。"""

    foreign = str(env.tickets["foreign"])
    forged_value = str(env.container.session_directory.resolve(env.tokens["foreign_operator"]).tenant_id)
    for header in FORGED_HEADERS:
        response = env.client.get(
            f"{PREFIX}/tickets/{foreign}",
            params=_params(env),
            headers=env.headers("operator", **{header: forged_value}),
        )
        assert env.connector.previews == []
        assert env.connector.writes == []
        assert response.status_code == 404, f"{header}: {response.status_code}"


def test_cross_tenant_recon_ignores_forged_headers(env: IdorEnv) -> None:
    """对账路径已按 token 的 tenant 查询：伪造头仍是 404（对照组）。"""

    batch = str(env.batch_b)
    for header in FORGED_HEADERS:
        response = env.client.get(
            f"{PREFIX}/recon/runs/{batch}",
            params=_params(env),
            headers=env.headers("operator", **{header: "ORG-1"}),
        )
        assert env.connector.writes == []
        assert response.status_code == 404, f"{header}: {response.status_code}"


def test_foreign_tenant_token_cannot_read_local_ticket(env: IdorEnv) -> None:
    """反向组合：租户 B 的 token 读租户 A 的工单，同样必须 404。"""

    own = str(env.tickets["own"])
    response = env.client.get(
        f"{PREFIX}/tickets/{own}",
        params=_params(env),
        headers=env.headers("foreign_operator"),
    )
    assert env.connector.writes == []
    assert response.status_code == 404, response.text


def test_cross_tenant_colliding_initiator_must_not_execute(env: IdorEnv) -> None:
    """最坏组合：他租户工单的发起人 UUID 与会话用户相同。

    租户已进入查询条件与身份比较 ``(tenant_id, user_id)``，因此即便 UUID 碰撞，
    另一租户的工单在查询层就查不到（404），不会落到执行闸。
    """

    collide = str(env.tickets["foreign_collide"])
    response = env.client.post(
        f"{PREFIX}/tickets/{collide}/actions/approve",
        json={"scope": env.scope(), "comment": "idor 碰撞"},
        headers=env.headers("operator", "idor-collide"),
    )
    assert response.status_code == 404, response.text
    assert env.connector.writes == []
    assert env.connector.previews == []


def test_same_tenant_peer_approval_denied_with_explicit_reason(env: IdorEnv) -> None:
    """同租户但非本人发起：拒绝要是角色/审批语义（403/409），不能退化成 404。"""

    other = str(env.tickets["other_user"])
    response = env.client.post(
        f"{PREFIX}/approvals/{other}/approve",
        json={"scope": env.scope(), "role": "reviewer", "comment": "越权尝试"},
        headers=env.headers("operator", "same-tenant-approve"),
    )
    assert response.status_code in (403, 409), response.text
    body = response.json()
    assert body["code"] != "E-DATA-MISSING"
    assert body.get("detail"), "拒绝理由不得为空"


def test_same_tenant_peer_reject_denied_with_explicit_reason(env: IdorEnv) -> None:
    """同上：operator 不得以 reviewer 角色驳回同租户他人的工单。"""

    other = str(env.tickets["other_user"])
    response = env.client.post(
        f"{PREFIX}/tickets/{other}/actions/reject",
        json={"scope": env.scope(), "role": "reviewer", "comment": "越权驳回"},
        headers=env.headers("operator", "same-tenant-reject"),
    )
    assert response.status_code in (403, 409), response.text
    assert response.json()["code"] != "E-DATA-MISSING"


def test_random_identifiers_have_uniform_404_without_timing_signal(env: IdorEnv) -> None:
    """标识符枚举：随机 uuid 打 100 次 × 3 类资源，全部 404、无 500、无显著耗时差。"""

    samples: dict[str, list[float]] = {}
    for name, path in (
        ("ticket", f"{PREFIX}/tickets/{{id}}"),
        ("images", f"{PREFIX}/audit/tickets/{{id}}/images"),
        ("recon", f"{PREFIX}/recon/runs/{{id}}"),
    ):
        durations: list[float] = []
        for _ in range(100):
            url = path.format(id=uuid4())
            started = time.perf_counter()
            response = env.client.get(
                url, params=_params(env), headers=env.headers("operator")
            )
            durations.append((time.perf_counter() - started) * 1000)
            assert response.status_code == 404, f"{name}: {response.status_code}"
        samples[name] = durations

    medians = {name: statistics.median(values) for name, values in samples.items()}
    assert min(medians.values()) > 0
    ratio = max(medians.values()) / min(medians.values())
    assert ratio < 10, f"不同资源类型的 404 中位耗时差异过大: {medians}"
    assert env.connector.writes == []
