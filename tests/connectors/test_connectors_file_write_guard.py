"""文件通道的只读守卫：没有 ActionSpec，任何写路径都必须被拒绝。"""

from __future__ import annotations

import pytest

from apps.connectors.errors import ConnectorError, ErrorKind
from apps.connectors.gateway import build_default_registry
from apps.platform.policy.levels import VerifySpec
from tests.connectors.file_support import FILE_FIXTURES, make_file_stack, run
from tests.connectors.support import PO_ACTION


def test_gateway_registers_file_channel_only_with_inbox(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ERP_FILE_INBOX", raising=False)
    assert "file" not in build_default_registry().aliases

    monkeypatch.setenv("ERP_FILE_INBOX", str(FILE_FIXTURES))
    registry = build_default_registry()

    assert "file" in registry.aliases
    cards = {card["alias"]: card for card in registry.describe()}
    assert cards["file"]["system"] == "file"
    assert sorted(cards["file"]["objects"]) == ["delivery_note", "supplier_statement"]
    assert cards["file"]["actions"] == []
    # 文件通道不新增任何可写动作，U8 的动作集合不受影响。
    assert registry.actions
    assert all(not key.startswith("file.") for key in registry.allowlist)


def test_write_preview_verify_compensate_are_refused() -> None:
    stack = make_file_stack()

    with pytest.raises(ConnectorError) as write_error:
        run(
            stack.connector.write(
                PO_ACTION,
                {"doc_no": "X"},
                ticket_id="T-1",
                idem_key="idem-1",
            )
        )
    assert write_error.value.kind is ErrorKind.FORBIDDEN
    assert write_error.value.error_class.value == "permanent"

    with pytest.raises(ConnectorError) as preview_error:
        run(stack.connector.preview(PO_ACTION, {"doc_no": "X"}))
    assert preview_error.value.kind is ErrorKind.FORBIDDEN

    with pytest.raises(ConnectorError) as verify_error:
        run(
            stack.connector.verify(
                VerifySpec(kind="read_by_business_key", fields=("doc_no",)),
                {"doc_no": "X"},
            )
        )
    assert verify_error.value.kind is ErrorKind.FORBIDDEN

    with pytest.raises(ConnectorError) as compensate_error:
        run(stack.connector.compensate({}, idem_key="idem-1"))
    assert compensate_error.value.kind is ErrorKind.FORBIDDEN
