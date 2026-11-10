"""C16 契约：必须由代码生成，且与已冻结文件保持一致。"""

from __future__ import annotations

from typing import Any

from apps.api.contract import CONTRACT_ID, build_contract, check_contract
from apps.platform.contracts.ticket import TICKET_STATES
from apps.recon.models import DiffType

REQUIRED_PATHS = (
    "/v1/tickets",
    "/v1/tickets/{ticket_id}",
    "/v1/tickets/{ticket_id}/actions/approve",
    "/v1/tickets/{ticket_id}/actions/reject",
    "/v1/tickets/{ticket_id}/actions/cancel",
    "/v1/tickets/{ticket_id}/actions/compensate",
    "/v1/recon/runs",
    "/v1/recon/runs/{batch_id}",
    "/v1/recon/runs/{batch_id}/diffs",
    "/v1/recon/runs/{batch_id}/diffs/{diff_id}",
    "/v1/approvals/pending",
    "/v1/approvals/{ticket_id}/approve",
    "/v1/approvals/{ticket_id}/reject",
    "/v1/audit/tickets/{ticket_id}/images",
    "/v1/audit/tickets/{ticket_id}/chain",
    "/v1/audit/export",
    "/v1/metering/batches/{batch_id}/statement",
)


def test_frozen_contract_matches_generated_code():
    assert check_contract() == []


def test_contract_covers_all_product_routes():
    contract = build_contract()
    assert contract["info"]["x-contract-id"] == CONTRACT_ID
    assert contract["servers"] == [{"url": "/erp/api"}]
    assert contract["openapi"].startswith("3.1")
    for path in REQUIRED_PATHS:
        assert path in contract["paths"], path


def _resolve(spec: dict[str, Any], schema: dict[str, Any]) -> dict[str, Any]:
    seen = 0
    while "$ref" in schema:
        ref = schema["$ref"]
        assert ref.startswith("#/components/schemas/"), ref
        schema = spec["components"]["schemas"][ref.rsplit("/", 1)[-1]]
        seen += 1
        assert seen < 10, "schema $ref 出现循环"
    return schema


def _enum_values(spec: dict[str, Any], schema: dict[str, Any]) -> set[str] | None:
    schema = _resolve(spec, schema)
    if "enum" in schema:
        return set(schema["enum"])
    for branch in schema.get("anyOf", []):
        values = _enum_values(spec, branch)
        if values is not None:
            return values
    return None


def test_contract_enums_are_derived_from_code():
    """契约枚举 == 代码枚举（双向差集为空），禁止再手写清单。"""

    contract = build_contract()
    parameters = contract["paths"]["/v1/recon/runs/{batch_id}/diffs"]["get"][
        "parameters"
    ]
    diff_param = next(item for item in parameters if item["name"] == "diff_type")
    contract_diff_types = _enum_values(contract, diff_param["schema"])
    code_diff_types = {item.name.lower() for item in DiffType}
    assert contract_diff_types == code_diff_types, (
        f"契约多出 {contract_diff_types - code_diff_types}，"
        f"契约缺少 {code_diff_types - contract_diff_types}"
    )
    assert "rounding_tolerance" in contract_diff_types

    ticket_state = contract["components"]["schemas"]["TicketOut"]["properties"]["state"]
    contract_states = _enum_values(contract, ticket_state)
    assert contract_states == set(TICKET_STATES), (
        f"契约多出 {contract_states - set(TICKET_STATES)}，"
        f"契约缺少 {set(TICKET_STATES) - contract_states}"
    )
