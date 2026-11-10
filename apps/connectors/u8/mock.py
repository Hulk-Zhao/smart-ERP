"""本地 U8 mock（httpx.MockTransport）：假实现只在这里，不连真实 U8。

mock 刻意保留 U8 的坏毛病，用来验证 SDK 的韧性：

- 会返回 503 / 429（带或不带 ``Retry-After``）；
- 会返回缺字段的数据（缺供应商编码、缺基本单位、金额对不上）；
- 写入要求 ``Idempotency-Key`` 头，缺了直接 400；
- 草稿可以删除（补偿），重复删除返回 404。
"""

from __future__ import annotations

import json
from collections import defaultdict, deque
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx


@dataclass(frozen=True, slots=True)
class U8Call:
    method: str
    path: str
    params: Mapping[str, str]
    body: Mapping[str, Any] | None
    headers: Mapping[str, str]

    @property
    def idempotency_key(self) -> str | None:
        return self.headers.get("idempotency-key")


@dataclass
class MockU8Backend:
    suppliers: list[dict[str, Any]] = field(default_factory=list)
    items: list[dict[str, Any]] = field(default_factory=list)
    purchase_orders: list[dict[str, Any]] = field(default_factory=list)
    arrival_vouches: list[dict[str, Any]] = field(default_factory=list)

    calls: list[U8Call] = field(default_factory=list)
    drafts: dict[str, dict[str, dict[str, Any]]] = field(
        default_factory=lambda: defaultdict(dict)
    )
    _faults: dict[str, deque[tuple[int, str | None]]] = field(
        default_factory=lambda: defaultdict(deque)
    )
    _sequence: int = 0

    # ------------------------------------------------------------------ 测试工具
    def inject_failure(
        self,
        path_contains: str,
        status: int,
        *,
        times: int = 1,
        retry_after: str | None = None,
    ) -> None:
        """接下来对该路径的前 ``times`` 次请求返回指定状态码。"""

        for _ in range(times):
            self._faults[path_contains].append((status, retry_after))

    def clear_faults(self) -> None:
        self._faults.clear()

    def calls_to(self, path_contains: str) -> list[U8Call]:
        return [call for call in self.calls if path_contains in call.path]

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=httpx.MockTransport(self.handle),
            base_url="http://u8.mock",
        )

    @classmethod
    def from_fixtures(cls, directory: Path) -> MockU8Backend:
        def load(name: str) -> list[dict[str, Any]]:
            path = directory / name
            return json.loads(path.read_text(encoding="utf-8"))

        return cls(
            suppliers=load("suppliers.json"),
            items=load("items.json"),
            purchase_orders=load("purchase_orders.json"),
            arrival_vouches=load("arrival_vouches.json"),
        )

    # -------------------------------------------------------------------- 路由
    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        params = dict(request.url.params)
        body = _json_body(request)
        self.calls.append(
            U8Call(
                method=request.method,
                path=path,
                params=params,
                body=body,
                # httpx 会把头名规范成小写，这里显式记录小写键，断言更稳定。
                headers={key.lower(): value for key, value in request.headers.items()},
            )
        )

        fault = self._take_fault(path)
        if fault is not None:
            status, retry_after = fault
            headers = {"Retry-After": retry_after} if retry_after else {}
            return httpx.Response(status, json={"error": f"mock_{status}"}, headers=headers)

        if request.method == "GET":
            return self._handle_get(path, params)
        if request.method == "POST":
            return self._handle_post(path, body, request)
        if request.method == "DELETE":
            return self._handle_delete(path)
        return httpx.Response(405, json={"error": "method_not_allowed"})

    def _take_fault(self, path: str) -> tuple[int, str | None] | None:
        for fragment, queue in self._faults.items():
            if fragment in path and queue:
                return queue.popleft()
        return None

    def _handle_get(self, path: str, params: Mapping[str, str]) -> httpx.Response:
        if path == "/u8/api/suppliers":
            return _ok(_filter(self.suppliers, "cVenCode", params.get("code")))
        if path == "/u8/api/items":
            return _ok(_filter(self.items, "cInvCode", params.get("code")))
        if path == "/u8/api/purchase-orders":
            return _ok(_filter(self.purchase_orders, "cCode", params.get("code")))
        if path == "/u8/api/arrival-vouches":
            return _ok(_filter(self.arrival_vouches, "cCode", params.get("code")))
        for route in ("purchase-order-drafts", "arrival-drafts"):
            if path == f"/u8/api/{route}":
                source = params.get("source_doc_no")
                rows = [
                    record
                    for record in self.drafts[route].values()
                    if source is None or record.get("source_doc_no") == source
                ]
                return _ok(rows)
        return httpx.Response(404, json={"error": "not_found"})

    def _handle_post(
        self, path: str, body: Mapping[str, Any] | None, request: httpx.Request
    ) -> httpx.Response:
        route = None
        for candidate in ("purchase-order-drafts", "arrival-drafts"):
            if path == f"/u8/api/{candidate}":
                route = candidate
                break
        if route is None:
            return httpx.Response(404, json={"error": "not_found"})
        if not request.headers.get("Idempotency-Key"):
            return httpx.Response(400, json={"error": "missing_idempotency_key"})
        if body is None:
            return httpx.Response(400, json={"error": "invalid_json"})
        required = {"cVenCode", "dPODate", "details"}
        missing = required - set(body)
        if missing:
            return httpx.Response(422, json={"error": "missing_fields", "fields": sorted(missing)})
        self._sequence += 1
        prefix = "DPO" if route == "purchase-order-drafts" else "DRV"
        code = f"{prefix}-{self._sequence:04d}"
        record = {
            "cCode": code,
            "source_doc_no": body.get("source_doc_no"),
            "cVenCode": body["cVenCode"],
            "dPODate": body["dPODate"],
            "cMemo": body.get("cMemo"),
            "details": body["details"],
            "state": "draft",
        }
        self.drafts[route][code] = record
        return httpx.Response(201, json=record)

    def _handle_delete(self, path: str) -> httpx.Response:
        for route in ("purchase-order-drafts", "arrival-drafts"):
            prefix = f"/u8/api/{route}/"
            if path.startswith(prefix):
                code = path[len(prefix) :]
                if code in self.drafts[route]:
                    del self.drafts[route][code]
                    return httpx.Response(204)
                return httpx.Response(404, json={"error": "not_found"})
        return httpx.Response(404, json={"error": "not_found"})


def _ok(rows: Iterable[Mapping[str, Any]]) -> httpx.Response:
    return httpx.Response(200, json={"data": list(rows)})


def _filter(
    rows: list[dict[str, Any]], key: str, value: str | None
) -> list[dict[str, Any]]:
    if value is None:
        return list(rows)
    return [row for row in rows if str(row.get(key)) == value]


def _json_body(request: httpx.Request) -> Mapping[str, Any] | None:
    if not request.content:
        return None
    try:
        payload = json.loads(request.content)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None
