"""S1 连接器测试的共用件：本地 U8 mock、假时钟、装配好的栈。"""

from __future__ import annotations

import asyncio
import json
import random
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from apps.connectors.u8.actions import PO_CREATE_DRAFT
from apps.connectors.u8.factory import AllowAllApprovals, U8Stack, build_u8_stack
from apps.connectors.u8.mock import MockU8Backend
from apps.platform.policy.levels import SceneSpec, WriteLevel

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "fixtures" / "connectors" / "u8"


def run[T](coro: Awaitable[T]) -> T:
    """仓库没有 pytest-asyncio，统一用 asyncio.run（与 tests/platform 一致）。"""

    return asyncio.run(coro)  # type: ignore[arg-type]


def load_fixture(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def make_backend() -> MockU8Backend:
    return MockU8Backend.from_fixtures(FIXTURES)


def make_stack(
    backend: MockU8Backend | None = None,
    *,
    experimental: bool = False,
    sleep: Callable[[float], Awaitable[None]] | None = None,
    timeout_s: float = 5.0,
    idem: Any = None,
) -> tuple[MockU8Backend, U8Stack]:
    backend = backend or make_backend()
    stack = build_u8_stack(
        client=backend.client(),
        experimental=experimental,
        approvals=AllowAllApprovals(),
        sleep=sleep or asyncio.sleep,
        rng=random.Random(20261110),
        timeout_s=timeout_s,
        idem=idem,
    )
    return backend, stack


class FakeClock:
    """可注入的单调时钟 + 睡眠：限流/熔断测试不真的等待。"""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def post_calls(backend: MockU8Backend) -> list[Any]:
    return [call for call in backend.calls if call.method == "POST"]


def po_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "source_doc_no": "PO202610310001",
        "supplier_code": "V001",
        "doc_date": "2026-11-02",
        "lines": [
            {
                "item_code": "M-1001",
                "quantity": "120",
                "unit": "件",
                "unit_price": "35.5",
                "tax_rate": "13",
            }
        ],
        "memo": "工单 T-001 生成",
    }
    payload.update(overrides)
    return payload


PO_SCENE = SceneSpec(code="AG-2", max_level=WriteLevel.W1)
PO_ACTION = PO_CREATE_DRAFT
