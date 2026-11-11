"""U8 私有传输层（HTTP 打本地 mock；不连接真实 U8）。

本模块是**唯一**真正发 U8 请求的地方，也只被连接器以两种方式使用：

- ``get()`` —— 读接口；
- ``send()`` —— ``BaseConnector._transport_write()`` 传进来的写请求，内部带幂等键。

所有请求都过 ``ResilientCaller``（熔断 → 限流 → 超时 → 重试 → 分类），
所以连接器里没有第二套重试/限流代码。
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

import httpx

from apps.connectors.canonical.mapping import MappingContext, MappingOutcome
from apps.connectors.errors import (
    ConnectorError,
    ErrorKind,
    classify_exception,
    classify_status_code,
)
from apps.connectors.idempotency import IdempotencyStore, IdemStatus, request_hash
from apps.connectors.runtime import ResilientCaller
from apps.connectors.u8.actions import (
    ARRIVAL_DRAFT_KEY,
    DELETE_ACTIONS,
    PO_DRAFT_KEY,
    READ_LIMIT_KEY,
)
from apps.connectors.u8.mapping import build_arrival_draft_body, build_po_draft_body

# 动作 → 草稿接口路径；未声明的动作在发送前就被拒绝。
DRAFT_ROUTES: dict[str, str] = {
    PO_DRAFT_KEY: "purchase-order-drafts",
    ARRIVAL_DRAFT_KEY: "arrival-drafts",
}

BodyBuilder = Callable[[Mapping[str, Any], MappingContext], MappingOutcome[dict[str, Any]]]


@dataclass(frozen=True, slots=True)
class U8TransportResponse:
    """与 ``TransportResponse`` 同构（不导入该模块，避免触碰 S0 的导入白名单）。"""

    external_id: str | None
    body: Mapping[str, Any]
    before_ref: str | None = None
    after_ref: str | None = None


class U8Transport:
    def __init__(
        self,
        *,
        client: httpx.AsyncClient,
        caller: ResilientCaller,
        idem: IdempotencyStore,
        ctx: MappingContext,
        builders: Mapping[str, BodyBuilder] | None = None,
    ) -> None:
        self._client = client
        self._caller = caller
        self._idem = idem
        self._ctx = ctx
        self._builders: Mapping[str, BodyBuilder] = builders or {
            PO_DRAFT_KEY: build_po_draft_body,
            ARRIVAL_DRAFT_KEY: build_arrival_draft_body,
        }

    @property
    def breaker(self):
        return self._caller.breaker

    @property
    def rate_limit_remaining(self) -> float:
        return self._caller.limiter.remaining_ratio(READ_LIMIT_KEY)

    # ---------------------------------------------------------------------- 读
    async def get(self, route: str, params: Mapping[str, Any]) -> list[dict[str, Any]]:
        payload = await self._caller.call(
            READ_LIMIT_KEY, lambda: self._get_json(f"/u8/api/{route}", params), phase="read"
        )
        data = payload.get("data")
        if not isinstance(data, list):
            raise ConnectorError(
                ErrorKind.SCHEMA_CHANGED,
                f"U8 {route} 返回结构不含 data 数组（版本漂移）",
            )
        return [row for row in data if isinstance(row, dict)]

    async def get_draft(self, action_key: str, source_doc_no: str) -> Mapping[str, Any] | None:
        route = self._route(action_key)
        rows = await self.get(route, {"source_doc_no": source_doc_no})
        return rows[0] if rows else None

    async def get_draft_by_code(self, action_key: str, c_code: str) -> Mapping[str, Any] | None:
        """按 U8 单号（cCode）查草稿；冲销前像与冲销校验共用。"""

        route = self._route(action_key)
        rows = await self.get(route, {})
        for row in rows:
            if str(row.get("cCode") or "") == c_code:
                return row
        return None

    async def delete_draft(self, action_key: str, external_id: str) -> bool:
        route = self._route(action_key)

        async def operation() -> bool:
            response = await self._client.delete(f"/u8/api/{route}/{external_id}")
            if response.status_code == 404:
                return False
            if not response.is_success:
                self._raise_status(response)
            return True

        return await self._caller.call(action_key, operation, phase="write")

    def _route(self, action_key: str) -> str:
        route = DRAFT_ROUTES.get(action_key) or DELETE_ACTIONS.get(action_key)
        if route is None:
            raise ConnectorError(ErrorKind.INVALID, f"动作 {action_key} 没有声明草稿路由")
        return route

    # ---------------------------------------------------------------------- 写
    async def send(self, request: Any) -> U8TransportResponse:
        """由 ``BaseConnector._transport_write()`` 调用；``request`` 只按属性读取。"""

        action_key = str(request.action_key)
        if action_key in DELETE_ACTIONS:
            return await self._send_delete(action_key, request)

        route = self._route(action_key)
        builder = self._builders.get(action_key)
        if builder is None:
            raise ConnectorError(ErrorKind.INVALID, f"动作 {action_key} 没有报文映射")

        outcome = builder(dict(request.payload), self._ctx)
        if outcome.blocked or outcome.record is None:
            raise ConnectorError(
                ErrorKind.INVALID,
                f"动作 {action_key} 的载荷无法映射为 U8 草稿（缺就报缺，不猜）",
                detail={"issues": [issue.model_dump() for issue in outcome.issues]},
            )
        body = outcome.record
        fingerprint = request_hash({"action": action_key, "body": body})

        decision = await self._idem.begin(
            str(request.idem_key), fingerprint, action_key=action_key
        )
        if decision.status is IdemStatus.REPLAY and decision.response is not None:
            stored = dict(decision.response)
            return U8TransportResponse(
                external_id=stored.get("external_id"),
                body=stored.get("body", {}),
                before_ref=stored.get("before_ref"),
                after_ref=stored.get("after_ref"),
            )

        try:
            created = await self._caller.call(
                action_key,
                lambda: self._post_json(f"/u8/api/{route}", body, request),
                phase="write",
            )
        except ConnectorError:
            await self._idem.fail(str(request.idem_key))
            raise

        external_id = created.get("cCode")
        if not external_id:
            # 写回成功但拿不到单号 → 结果未知，必须走 verify，不能当成功。
            await self._idem.fail(str(request.idem_key))
            raise ConnectorError(
                ErrorKind.UNKNOWN_OUTCOME, "U8 写回未返回单号，结果未知（需 verify）"
            )

        stored = {
            "external_id": str(external_id),
            "body": created,
            "before_ref": str(request.payload.get("source_doc_no") or ""),
            "after_ref": str(external_id),
        }
        await self._idem.complete(str(request.idem_key), stored)
        return U8TransportResponse(
            external_id=stored["external_id"],
            body=created,
            before_ref=stored["before_ref"] or None,
            after_ref=stored["after_ref"],
        )

    async def _send_delete(self, action_key: str, request: Any) -> U8TransportResponse:
        """反向动作：按 cCode 删除草稿；已不存在视为幂等命中，不当失败。"""

        c_code = str(request.payload.get("cCode") or "").strip()
        if not c_code:
            raise ConnectorError(ErrorKind.INVALID, f"动作 {action_key} 缺 cCode，无法冲销")
        fingerprint = request_hash({"action": action_key, "cCode": c_code})

        decision = await self._idem.begin(
            str(request.idem_key), fingerprint, action_key=action_key
        )
        if decision.status is IdemStatus.REPLAY and decision.response is not None:
            stored = dict(decision.response)
            return U8TransportResponse(
                external_id=stored.get("external_id"),
                body=stored.get("body", {}),
                before_ref=stored.get("before_ref"),
                after_ref=stored.get("after_ref"),
            )

        try:
            deleted = await self.delete_draft(action_key, c_code)
        except ConnectorError:
            await self._idem.fail(str(request.idem_key))
            raise

        stored = {
            "external_id": c_code,
            "body": {"cCode": c_code, "deleted": deleted},
            "before_ref": c_code,
            "after_ref": None,
        }
        await self._idem.complete(str(request.idem_key), stored)
        return U8TransportResponse(
            external_id=c_code,
            body=stored["body"],
            before_ref=c_code,
            after_ref=None,
        )

    # -------------------------------------------------------------------- HTTP
    async def _get_json(self, path: str, params: Mapping[str, Any]) -> dict[str, Any]:
        try:
            response = await self._client.get(path, params=dict(params))
        except Exception as exc:  # 统一收敛成 ConnectorError
            raise classify_exception(exc, phase="read") from exc
        self._raise_status(response)
        return self._decode(response)

    async def _post_json(
        self, path: str, body: Mapping[str, Any], request: Any
    ) -> dict[str, Any]:
        try:
            response = await self._client.post(
                path,
                json=dict(body),
                headers={
                    "Idempotency-Key": str(request.idem_key),
                    "X-Ticket-Id": str(request.ticket_id),
                },
            )
        except Exception as exc:
            raise classify_exception(exc, phase="write") from exc
        self._raise_status(response)
        return self._decode(response)

    @staticmethod
    def _decode(response: httpx.Response) -> dict[str, Any]:
        try:
            payload = response.json()
        except (json.JSONDecodeError, ValueError) as exc:
            raise ConnectorError(
                ErrorKind.SCHEMA_CHANGED, f"U8 响应不是合法 JSON: {exc}", status=response.status_code
            ) from exc
        if not isinstance(payload, dict):
            raise ConnectorError(
                ErrorKind.SCHEMA_CHANGED, "U8 响应顶层不是对象（版本漂移）", status=response.status_code
            )
        return payload

    @staticmethod
    def _raise_status(response: httpx.Response) -> None:
        if response.is_success:
            return
        retry_after: float | None = None
        raw_retry = response.headers.get("Retry-After")
        if raw_retry:
            try:
                retry_after = float(raw_retry)
            except ValueError:
                retry_after = None
        kind = classify_status_code(response.status_code)
        raise ConnectorError(
            kind,
            f"U8 返回 {response.status_code}: {response.text[:200]}",
            status=response.status_code,
            retry_after_s=retry_after,
        )
