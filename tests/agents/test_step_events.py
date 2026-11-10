"""``ticket_step`` 是可变执行投影，不是证据：每次变更必须伴随一条 ``STEP_EVENT``。

S0 裁定（``01-技术架构.md:581`` 步骤级真源、状态字段受控 UPDATE；``:650`` 检查点
= 每步一行终态）不给 ``ticket_step`` 加 append-only 触发器——那会打断断点续跑与
补偿依据。对应的替代约束是：

1. 授权收窄到 ``mark_step`` 真正会写的六列（见 ``0006_audit_image_prev_uniq``）；
2. 每次步骤变更都必须在同一事务里追加 ``STEP_EVENT``（本文件断言）；
3. 任何以步骤行为依据的结论，必须回到 ``ticket_event`` 重放。
"""

from __future__ import annotations

import asyncio

from dbutil import db_env, new_ticket
from sqlalchemy import text

from apps.agents.events import STEP_EVENT


async def _step_event_states(session, ticket_id) -> list[str]:
    rows = (
        await session.execute(
            text(
                "SELECT payload->>'state' AS state FROM erp.ticket_event "
                "WHERE ticket_id = :tid AND event_type = :etype "
                "ORDER BY seq"
            ),
            {"tid": ticket_id, "etype": STEP_EVENT},
        )
    ).all()
    return [row.state for row in rows]


def test_every_step_mutation_emits_step_event(db: str) -> None:
    async def scenario() -> None:
        async with db_env(db) as env:
            ticket = await new_ticket(env.store)
            step = await env.store.intent_step(
                ticket_id=ticket.id,
                step_no=1,
                action_key="fake.draft.create",
                target_system="fake",
                write_level=1,
                idem_key="idem-1",
                request_hash="0" * 64,
                request_snapshot={"amount": "10.00"},
            )
            await env.store.mark_step(step_id=step.id, state="running")
            await env.store.mark_step(
                step_id=step.id,
                state="succeeded",
                latency_ms=42,
                response_snapshot={"ok": True},
            )

            current = await env.store.get_step(step.id)
            async with env.sf() as session:
                states = await _step_event_states(session, ticket.id)

            # 建步骤 + 两次状态推进 = 三条事件，一条都不能少
            assert states == ["intent", "running", "succeeded"]
            # 投影的当前状态必须等于最后一条事件的状态（不一致说明中间被绕过改过）
            assert current.state == states[-1]
            assert current.latency_ms == 42

    asyncio.run(scenario())
