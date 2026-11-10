"""决策链还原：从一条工单的 span 还原「决策 → 预演 → 写入 → 校验 → 补偿」。"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from apps.platform.policy.gateway import WriteGateway

from observability import (
    CHAIN_PHASES,
    TracedConnector,
    Tracer,
    build_decision_chain,
    render_decision_chain,
    render_trace,
)

from observability_support import (
    PAYLOAD,
    SCENE,
    AllowAll,
    FakeConnector,
    make_action,
)


def _run_full_chain():
    async def scenario():
        tracer = Tracer()
        trace = tracer.start_trace(ticket_id="T-1001", agent="AG-1")
        connector = FakeConnector()
        traced = TracedConnector(connector, tracer, trace)
        gateway = WriteGateway(traced, approvals=AllowAll())
        action = make_action()

        with tracer.step(
            trace, step_no=1, action_key=action.key, level=action.level, system="u8"
        ):
            with tracer.decision(
                trace, kind="match", rule_id="rule.match.purchase_order", confidence=0.88
            ):
                pass
            await gateway.execute(
                action,
                PAYLOAD,
                ticket_id="T-1001",
                idem_key="idem-1",
                scene=SCENE,
            )

        # 运行时在步骤之外触发补偿，链路仍应按 action_key 归位
        await traced.compensate(
            SimpleNamespace(action_key=action.key, original_idem_key="idem-1"),
            idem_key="idem-1:undo",
        )
        return tracer, trace, connector

    return asyncio.run(scenario())


def test_full_chain_is_restored_from_one_ticket() -> None:
    _, trace, connector = _run_full_chain()

    assert connector.writes == [(make_action().key, "T-1001", "idem-1")]
    assert connector.compensations == ["idem-1:undo"]

    chain = build_decision_chain(trace)
    assert chain.ticket_id == "T-1001"
    assert chain.agent == "AG-1"
    assert len(chain.steps) == 1
    assert chain.complete is True
    assert chain.missing == ()
    assert chain.phases == CHAIN_PHASES

    step = chain.steps[0]
    assert step.step_no == 1
    assert step.action_key == make_action().key
    assert step.phase("decision").attributes["rule_id"] == "rule.match.purchase_order"
    assert step.phase("preview").attributes["field_count"] == 2
    assert step.phase("write").attributes["idem_key"] == "idem-1"
    assert step.phase("verify").attributes["verify_result"] == "match"
    assert step.phase("compensation").attributes["compensate_status"] == "compensated"
    # 补偿 span 物理上不在步骤内，但按 action_key 唯一匹配归位
    assert step.phase("compensation").parent_id is None


def test_decision_chain_text_has_fixed_phase_order() -> None:
    _, trace, _ = _run_full_chain()
    text = render_decision_chain(build_decision_chain(trace))

    assert "决策链" in text
    assert "步骤 ticket.step #1" in text
    positions = [text.index(label) for label in ("决策 ", "预演 ", "写入 ", "校验 ", "补偿 ")]
    assert positions == sorted(positions)
    assert "链路覆盖: 决策 → 预演 → 写入 → 校验 → 补偿（5/5，完整）" in text


def test_trace_tree_is_readable_and_nested() -> None:
    _, trace, _ = _run_full_chain()
    text = render_trace(trace)

    lines = text.splitlines()
    assert lines[0].startswith(f"trace {trace.trace_id} ticket=T-1001 agent=AG-1")
    assert "status=ok" in lines[0]
    assert any("├─ ticket.step #1" in line or "└─ ticket.step #1" in line for line in lines)
    assert any("gateway.preview connector=u8 fields=2" in line for line in lines)
    assert any("gateway.write level=W1 idem=idem-1" in line for line in lines)
    assert any("gateway.verify result=match" in line for line in lines)
    assert any("gateway.compensate" in line for line in lines)

    write_line = next(line for line in lines if "gateway.write" in line)
    assert write_line.startswith("│  ├─") or write_line.startswith("│  └─")
    assert "external_id=EXT-1" in write_line


def test_missing_phases_are_reported_not_filled() -> None:
    tracer = Tracer()
    trace = tracer.start_trace(ticket_id="T-2002", agent="AG-2")
    with tracer.step(trace, step_no=1, action_key="u8.po.draft", level="W1", system="u8"):
        with tracer.decision(trace, kind="draft", model="rule-based", prompt_version="n/a"):
            pass
        with tracer.span(trace, "gateway.preview", connector="u8", field_count=1):
            pass

    chain = build_decision_chain(trace)
    assert chain.complete is False
    assert chain.missing == ("write", "verify", "compensation")

    text = render_decision_chain(chain)
    assert "（缺失：写入、校验、补偿）" in text
    assert "（2/5，不完整）" in text


def test_chain_without_steps_is_not_claimed_complete() -> None:
    tracer = Tracer()
    trace = tracer.start_trace(ticket_id="T-3003", agent="AG-3")
    with tracer.span(trace, "gateway.preview", connector="u8", field_count=0):
        pass

    chain = build_decision_chain(trace)
    assert chain.steps == ()
    assert chain.complete is False
