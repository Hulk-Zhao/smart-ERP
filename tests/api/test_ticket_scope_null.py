"""``systems IS NULL`` 在 scope 过滤里的语义：**不可见**（fail-closed）。

NULL 表示「涉及系统未知」，不表示「全部可见」。这里钉三件事：

1. 列表里不出现，且同一 scope 下的正常工单仍在——证明「不出现」不是查询恰好为空；
2. 详情 403 ``E-AUTHZ-001``（不是 200，也不是把「未知」当 404 混过去）；
3. SQL 谓词必须是显式的 ``systems IS NOT NULL``，不能靠 ``IS NULL OR ...`` 的短路
   把未知放行成租户内全部可见。
"""

from __future__ import annotations

from datetime import UTC, datetime

import sqlalchemy as sa
from conftest import INITIATOR, ORG, PERIOD, PREFIX, SCENE, SYSTEM, TENANT
from sqlalchemy.dialects import postgresql

from apps.api.store import TicketRecord
from apps.api.ticket_sql import _scope_where
from apps.platform.contracts.ticket import TicketState
from apps.platform.db.models import Ticket
from apps.security.scope import Scope


def _null_systems_ticket() -> TicketRecord:
    return TicketRecord(
        ticket_no="T-NULL-1",
        tenant_id=TENANT,
        scene_code=SCENE,
        initiator_id=INITIATOR,
        business_key="BK-NULL-1",
        org_id=ORG,
        period=PERIOD,
        systems=None,
        write_level=None,
        amount=None,
        touches_fund=False,
        state=TicketState.PENDING_APPROVAL,
        version=0,
        created_at=datetime(2026, 11, 10, 8, 0, 0, tzinfo=UTC),
    )


def test_null_systems_ticket_is_invisible_in_list(env):
    ticket = _null_systems_ticket()
    env.container.tickets.add(ticket)

    response = env.client.get(
        f"{PREFIX}/tickets",
        params=env.scope_params(),
        headers=env.headers("initiator"),
    )

    assert response.status_code == 200, response.text
    ids = {row["ticket_id"] for row in response.json()["items"]}
    assert str(ticket.ticket_id) not in ids
    # 同 scope 的正常工单必须还在，否则「不出现」可能只是过滤条件写错了。
    assert str(env.tickets["normal"]) in ids


def test_null_systems_ticket_detail_is_rejected_as_out_of_scope(env):
    ticket = _null_systems_ticket()
    env.container.tickets.add(ticket)

    response = env.client.get(
        f"{PREFIX}/tickets/{ticket.ticket_id}",
        params=env.scope_params(),
        headers=env.headers("initiator"),
    )

    assert response.status_code == 403, response.text
    assert response.json()["code"] == "E-AUTHZ-001"


def test_sql_scope_predicate_guards_null_explicitly():
    scope = Scope(org_id=ORG, period=PERIOD, system_ids=(SYSTEM,))
    sql = str(
        sa.select(Ticket.id)
        .where(*_scope_where(scope))
        .compile(dialect=postgresql.dialect())
    )

    assert "systems IS NOT NULL" in sql
    # 没有任何 OR 短路：NULL 只能被拒绝，不能被某个分支顺手放行。
    assert " OR " not in sql
