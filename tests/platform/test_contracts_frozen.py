"""冻结契约件必须与代码同源（--check 的等价断言）。"""

from __future__ import annotations

import json

from apps.platform.contracts.export_contracts import ROOT, build_contracts


def test_frozen_contracts_exist_and_match_code() -> None:
    contracts = build_contracts()
    assert contracts, "至少应有一份契约"

    for relative, payload in contracts.items():
        path = ROOT / relative
        assert path.exists(), f"缺失契约文件: {relative}"
        assert json.loads(path.read_text(encoding="utf-8")) == payload, f"契约漂移: {relative}"


def test_contract_ids_are_versioned() -> None:
    for relative, payload in build_contracts().items():
        if relative.endswith(".json") and "contract" in payload:
            assert payload.get("version") == "1.0", relative
