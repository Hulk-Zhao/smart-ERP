"""从代码生成 ``contracts/`` 冻结件。

Schema 与代码同源：先改 Pydantic 模型/常量，再跑

    python -m apps.platform.contracts.export_contracts --write

CI 用 ``--check`` 校验冻结件未漂移（§4.3 契约变更流程）。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from apps.platform.contracts.agent import AG1ReconcileInput, AG1ReconcileOutput
from apps.platform.contracts.errors import error_contract
from apps.platform.contracts.ticket import state_contract
from apps.platform.policy.levels import (
    CRITICAL_READONLY_FIELDS,
    FORBIDDEN_ACTION_VERBS,
    WriteLevel,
)

ROOT = Path(__file__).resolve().parents[3]


def build_contracts() -> dict[str, dict]:
    return {
        "contracts/agent/ag1.reconcile.input.v1.json": AG1ReconcileInput.model_json_schema(),
        "contracts/agent/ag1.reconcile.output.v1.json": AG1ReconcileOutput.model_json_schema(),
        "contracts/policy/write_levels.v1.json": {
            "contract": "write-levels",
            "version": "1.0",
            "levels": [
                {
                    "level": level.name,
                    "value": int(level),
                    "executable": level is not WriteLevel.W3,
                    "meaning": meaning,
                }
                for level, meaning in (
                    (WriteLevel.W0, "只读/建议"),
                    (WriteLevel.W1, "草稿类写入，可补偿"),
                    (WriteLevel.W2, "需审批的写入"),
                    (WriteLevel.W3, "资金/过账/认证，只识别只拒绝"),
                )
            ],
            "resolution": "min(scene_cap, connector_cap, action_level, breaker_cap); W3 为吸收元",
            "forbidden_action_verbs": sorted(FORBIDDEN_ACTION_VERBS),
            "critical_readonly_fields": sorted(CRITICAL_READONLY_FIELDS),
        },
        "contracts/ticket/state_machine.v1.json": state_contract(),
        "contracts/errors/codes.v1.json": error_contract(),
        "contracts/connector/action_spec.v1.json": {
            "contract": "action-spec",
            "version": "1.0",
            "fields": {
                "key": "string, '{connector}.{alias}.{action}'",
                "level": "W0 | W1 | W2",
                "writable_fields": "string[]",
                "forbidden_fields": "string[]",
                "compensation": "object | null（W1 及以上必填）",
                "verify": "object（必填）",
                "rate_limit": "object{qps, daily_quota, concurrency}",
                "idempotent": "boolean",
            },
            "registration_rejects": [
                "level == W3",
                "level >= W1 且 compensation == null",
                "verify == null",
                "writable_fields ∩ critical_readonly_fields 非空",
                "action 命中禁止动词表",
            ],
        },
    }


def _dump(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def write_all() -> None:
    for relative, payload in build_contracts().items():
        target = ROOT / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(_dump(payload), encoding="utf-8")


def check_all() -> list[str]:
    drift: list[str] = []
    for relative, payload in build_contracts().items():
        target = ROOT / relative
        if not target.exists():
            drift.append(f"缺失: {relative}")
            continue
        if target.read_text(encoding="utf-8") != _dump(payload):
            drift.append(f"漂移: {relative}")
    return drift


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if args == ["--write"]:
        write_all()
        print(f"已写出 {len(build_contracts())} 份契约")
        return 0
    if args == ["--check"]:
        drift = check_all()
        if drift:
            print("\n".join(drift))
            return 1
        print("契约与代码一致")
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
