"""成本归因：按工单/Agent/步骤累计，缺 token 时保持 None 并给覆盖度。"""

from __future__ import annotations

import pytest

from observability import CostLedger, UsageError


def test_calls_and_duration_aggregate_by_ticket_agent_step() -> None:
    ledger = CostLedger()
    ledger.record_call(ticket_id="T-1", agent="AG-1", step_no=1, duration_ms=12.5)
    ledger.record_call(ticket_id="T-1", agent="AG-1", step_no=1, duration_ms=7.5, calls=2)
    ledger.record_call(ticket_id="T-1", agent="AG-2", step_no=2, duration_ms=5.0)

    rows = ledger.summary()
    assert rows == [
        {
            "ticket_id": "T-1",
            "agent": "AG-1",
            "step_no": 1,
            "calls": 3,
            "duration_ms": 20.0,
            "models": [],
            "scenes": [],
            "input_tokens": None,
            "output_tokens": None,
            "cost_micros": None,
            "token_coverage": "0/3",
        },
        {
            "ticket_id": "T-1",
            "agent": "AG-2",
            "step_no": 2,
            "calls": 1,
            "duration_ms": 5.0,
            "models": [],
            "scenes": [],
            "input_tokens": None,
            "output_tokens": None,
            "cost_micros": None,
            "token_coverage": "0/1",
        },
    ]


def test_llm_placeholder_does_not_fabricate_tokens() -> None:
    ledger = CostLedger()
    record = ledger.record_llm_call(
        ticket_id="T-1",
        agent="AG-1",
        model="rule-based",
        prompt_version="prompt-v1",
        step_no=1,
        duration_ms=3.2,
    )

    assert record.input_tokens is None and record.output_tokens is None
    assert record.cost_micros is None
    assert record.tokens_measured is False

    row = ledger.llm_usage_rows(tenant_id="tenant-1")[0]
    assert row["model"] == "rule-based"
    assert row["prompt_version"] == "prompt-v1"
    assert row["input_tokens"] is None and row["output_tokens"] is None
    assert row["cost_micros"] is None
    assert row["tenant_id"] == "tenant-1"

    summary = ledger.summary()[0]
    assert summary["token_coverage"] == "0/1"
    assert summary["input_tokens"] is None
    assert summary["models"] == ["rule-based"]


def test_measured_tokens_only_sum_covered_calls() -> None:
    ledger = CostLedger()
    ledger.record_llm_call(
        ticket_id="T-1",
        agent="AG-1",
        model="m1",
        prompt_version="p1",
        step_no=1,
        input_tokens=100,
        output_tokens=40,
        cost_micros=1200,
        price_version="price-v1",
    )
    ledger.record_llm_call(
        ticket_id="T-1",
        agent="AG-1",
        model="m1",
        prompt_version="p1",
        step_no=1,
        duration_ms=1.0,
    )

    summary = ledger.summary()[0]
    assert summary["calls"] == 2
    assert summary["token_coverage"] == "1/2"
    assert summary["input_tokens"] == 100
    assert summary["output_tokens"] == 40
    assert summary["cost_micros"] == 1200

    rows = ledger.llm_usage_rows()
    assert len(rows) == 2
    assert rows[1]["input_tokens"] is None


def test_non_llm_calls_are_excluded_from_llm_usage_rows() -> None:
    ledger = CostLedger()
    ledger.record_call(ticket_id="T-1", agent="AG-1", step_no=1, duration_ms=1.0)
    assert ledger.llm_usage_rows() == []


def test_invalid_usage_is_rejected() -> None:
    ledger = CostLedger()
    with pytest.raises(UsageError):
        ledger.record_call(ticket_id="", agent="AG-1")
    with pytest.raises(UsageError):
        ledger.record_call(ticket_id="T-1", agent="")
    with pytest.raises(UsageError):
        ledger.record_call(ticket_id="T-1", agent="AG-1", step_no=0)
    with pytest.raises(UsageError):
        ledger.record_call(ticket_id="T-1", agent="AG-1", input_tokens=-1)
    with pytest.raises(UsageError):
        ledger.record_llm_call(ticket_id="T-1", agent="AG-1", model="", prompt_version="p")
    with pytest.raises(UsageError):
        ledger.record_llm_call(ticket_id="T-1", agent="AG-1", model="m", prompt_version="")
