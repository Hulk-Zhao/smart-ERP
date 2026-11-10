"""C7 Agent 输入输出契约：scope 必填、金额对象化、置信度门槛。"""

from __future__ import annotations

from typing import Any, get_args

import pytest
from pydantic import ValidationError

from apps.platform.contracts.agent import (
    AG1ReconcileInput,
    AG1ReconcileOutput,
    TicketStatus,
)
from apps.platform.contracts.canonical import Money
from apps.platform.contracts.ticket import TICKET_STATES

SCOPE = {"org_id": "org-1", "period": "2026-10", "system_ids": ["erp"]}

INPUT: dict[str, Any] = {
    "scope": SCOPE,
    "suppliers": ["SUP-001"],
    "sources": {"erp": "kingdee"},
}


def _output(**overrides: Any) -> dict[str, Any]:
    payload = {
        "ticket_id": "AT-20261110-0001",
        "status": "计划中",
        "evidence": [{"system": "kingdee", "doc_no": "PI-001", "field": "amount"}],
        "batch_id": "B-001",
        "lines": [],
        "diffs": [],
        "actions": [],
        "metrics": {},
    }
    payload.update(overrides)
    return payload


def test_minimal_input_is_valid() -> None:
    parsed = AG1ReconcileInput.model_validate(INPUT)
    assert parsed.scope.period == "2026-10"


def test_scope_is_required() -> None:
    with pytest.raises(ValidationError):
        AG1ReconcileInput.model_validate({k: v for k, v in INPUT.items() if k != "scope"})


def test_scope_fields_are_required() -> None:
    for missing in ("org_id", "period", "system_ids"):
        scope = {k: v for k, v in SCOPE.items() if k != missing}
        payload = {**INPUT, "scope": scope}
        with pytest.raises(ValidationError):
            AG1ReconcileInput.model_validate(payload)


def test_period_must_be_year_month() -> None:
    with pytest.raises(ValidationError):
        AG1ReconcileInput.model_validate({**INPUT, "scope": {**SCOPE, "period": "2026-13"}})


def test_system_ids_cannot_be_empty() -> None:
    with pytest.raises(ValidationError):
        AG1ReconcileInput.model_validate({**INPUT, "scope": {**SCOPE, "system_ids": []}})


def test_input_rejects_unknown_field() -> None:
    with pytest.raises(ValidationError):
        AG1ReconcileInput.model_validate({**INPUT, "sql": "select 1"})


def test_money_rejects_bare_float() -> None:
    with pytest.raises(ValidationError):
        Money(amount=12.5, currency="CNY", tax_included=True)  # type: ignore[arg-type]


def test_money_accepts_string_and_quantizes() -> None:
    money = Money(amount="12.5000", currency="CNY", tax_included=True)
    assert money.value == money.quantized
    assert money.model_dump()["amount"] == "12.5000"


def test_confidence_below_070_rejected() -> None:
    with pytest.raises(ValidationError) as exc:
        AG1ReconcileOutput.model_validate(
            _output(
                diffs=[
                    {
                        "type": "单价不一致",
                        "amount": {"amount": "1.00", "currency": "CNY", "tax_included": True},
                        "owner": "supplier",
                        "suggestion": "以合同价为准",
                        "confidence": 0.69,
                    }
                ]
            )
        )
    assert "不可用" in str(exc.value) or "confidence" in str(exc.value)


def test_confidence_at_070_is_usable() -> None:
    parsed = AG1ReconcileOutput.model_validate(
        _output(
            diffs=[
                {
                    "type": "单价不一致",
                    "amount": {"amount": "1.00", "currency": "CNY", "tax_included": True},
                    "owner": "supplier",
                    "suggestion": "以合同价为准",
                    "confidence": 0.70,
                }
            ]
        )
    )
    assert parsed.diffs[0].confidence == 0.70


def test_output_requires_evidence() -> None:
    with pytest.raises(ValidationError):
        AG1ReconcileOutput.model_validate(_output(evidence=[]))


def test_output_accepts_extra_metrics() -> None:
    parsed = AG1ReconcileOutput.model_validate(_output(extra_metric=1))
    assert parsed.model_extra == {"extra_metric": 1}


def test_output_cannot_plan_w2_action() -> None:
    with pytest.raises(ValidationError):
        AG1ReconcileOutput.model_validate(
            _output(actions=[{"system": "kingdee", "action": "submit_draft", "level": "W2"}])
        )


def test_ticket_status_literal_matches_state_machine() -> None:
    assert set(get_args(TicketStatus)) == set(TICKET_STATES)
