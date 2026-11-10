"""U8 连接器（假实现）：HTTP 打本地 mock，不连真实 U8。

写路径刻意**不重写** ``BaseConnector._transport_write``：写入口永远是
``WriteGateway → BaseConnector.write() → transport.send()``，本类只提供
读/预演/校验/补偿/健康度。想绕过闸门直接调 transport 的接口在这里不存在。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any, TypeVar

from apps.connectors.breaker import BreakerState
from apps.connectors.canonical.mapping import MappingContext, MappingOutcome
from apps.connectors.canonical.models import Item
from apps.connectors.errors import ConnectorError, ErrorKind
from apps.connectors.health import HealthTracker
from apps.connectors.u8.actions import ACTIONS, ACTIONS_BY_KEY, PO_DRAFT_KEY
from apps.connectors.u8.mapping import (
    map_arrival_vouch,
    map_item,
    map_purchase_order,
    map_supplier,
)
from apps.connectors.u8.models import (
    U8ArrivalVouch,
    U8Item,
    U8PurchaseOrder,
    U8Supplier,
)
from apps.connectors.u8.transport import U8Transport
from apps.platform.connectors.sdk import (
    BaseConnector,
    CapabilitySet,
    CompensateResult,
    ConnectorMeta,
    DegradedReason,
    FieldDiffEntry,
    Health,
    PreviewResult,
    ReadQuery,
    ReadResult,
    VerifyResult,
    WriteResult,
)
from apps.platform.policy.levels import ActionSpec, VerifySpec, WriteLevel

T = TypeVar("T")

OBJECT_ROUTES = {
    "supplier": "suppliers",
    "item": "items",
    "purchase_order": "purchase-orders",
    "inbound_order": "arrival-vouches",
}

# 工单载荷字段 ↔ U8 草稿字段（预演 diff 与校验共用，避免两处口径）。
_DIFF_PAIRS = (
    ("supplier_code", "cVenCode"),
    ("doc_date", "dPODate"),
    ("memo", "cMemo"),
    ("line_count", "details"),
)


def _fingerprint() -> str:
    """版本指纹：能力集 + 动作字段的稳定哈希（FR-1206 漂移检测的基线）。"""

    payload = {
        "objects": sorted(OBJECT_ROUTES),
        "actions": sorted(
            (action.key, int(action.level), sorted(action.writable_fields)) for action in ACTIONS
        ),
        "version": "U16.5",
    }
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _line_count(value: Any) -> int | None:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return len(value)
    return None


class U8Connector(BaseConnector):
    def __init__(
        self,
        transport: U8Transport,
        *,
        ctx: MappingContext | None = None,
        experimental: bool = False,
    ) -> None:
        super().__init__(transport)
        self._transport = transport
        self.ctx = ctx or MappingContext(connector="u8", authority_system="erp")
        self.experimental = experimental
        fingerprint = _fingerprint()
        self.meta = ConnectorMeta(
            system="u8",
            version="U16.5",
            access_level="L1",
            security_level="B",
            supports_obo=False,
            version_fingerprint=fingerprint,
            experimental=experimental,
        )
        self._health = HealthTracker(system="u8", version_fingerprint=fingerprint)

    # ------------------------------------------------------------------ describe
    def describe(self) -> CapabilitySet:
        return CapabilitySet(
            objects=tuple(OBJECT_ROUTES),
            actions=ACTIONS,
            authority_map={
                "supplier": "erp",
                "item": "erp",
                "purchase_order": "erp",
                "inbound_order": "erp",
                "qty": "erp",
                "amount": "erp",
            },
        )

    @property
    def breaker(self):
        return self._transport.breaker

    def connector_cap(self) -> WriteLevel:
        """连接器上限：实验连接器一律 W0（FR-1201），其余交给动作/场景取严。"""

        return WriteLevel.W0 if self.experimental else WriteLevel.W2

    async def write(
        self,
        action: ActionSpec,
        payload: Mapping[str, Any],
        *,
        ticket_id: str,
        idem_key: str,
    ) -> WriteResult:
        """仍然走 ``BaseConnector.write``，只为实验连接器加一道零写入保险。

        W0 在闸门里是「只读/建议」的等级标记，但 S0 的 ``WriteGateway`` 在 W0 下
        仍会调用 write；实验连接器（FR-1201）不能借此把写入漏出去，所以这里显式拒绝。
        """

        if self.experimental:
            raise ConnectorError(
                ErrorKind.FORBIDDEN, "实验连接器禁止写入，需管理员确认后才可提升（FR-1201）"
            )
        return await super().write(action, payload, ticket_id=ticket_id, idem_key=idem_key)

    # ---------------------------------------------------------------------- read
    async def read(self, entity: str, query: ReadQuery) -> ReadResult:
        route = OBJECT_ROUTES.get(entity)
        if route is None:
            return ReadResult(
                items=(),
                completeness="unavailable",
                degraded=(
                    DegradedReason(source="u8", reason=f"unsupported_entity:{entity}"),
                ),
            )
        try:
            raw_rows = await self._transport.get(route, self._params(query))
        except Exception as exc:  # noqa: BLE001 - 读失败必须降级成 unavailable
            self._health.note_failure()
            return ReadResult(
                items=(),
                completeness="unavailable",
                degraded=(
                    DegradedReason(source="u8", reason=f"read_failed:{_describe_error(exc)}"),
                ),
            )

        outcomes = [self._map_row(entity, row) for row in raw_rows]
        self._health.note_success(synced=True)
        items = tuple(
            outcome.record.model_dump() for outcome in outcomes if outcome.record is not None
        )
        degraded = tuple(
            DegradedReason(
                source="u8",
                reason=f"mapping:{issue.reason}@{issue.doc_no or '-'}:{issue.field}",
            )
            for outcome in outcomes
            for issue in outcome.issues
        )
        blocked = sum(1 for outcome in outcomes if outcome.record is None)
        if blocked and not items:
            return ReadResult(
                items=(),
                completeness="unavailable",
                degraded=degraded
                or (DegradedReason(source="u8", reason="all_rows_unmappable"),),
            )
        completeness = "partial" if degraded or blocked else "complete"
        return ReadResult(items=items, completeness=completeness, degraded=degraded)

    def _params(self, query: ReadQuery) -> dict[str, Any]:
        filters = dict(query.filters)
        params: dict[str, Any] = {"limit": query.limit}
        for key in ("code", "doc_no", "date_from", "date_to"):
            if key in filters and filters[key] is not None:
                params["code" if key == "doc_no" else key] = filters[key]
        return params

    def _map_row(self, entity: str, row: Mapping[str, Any]) -> MappingOutcome[Any]:
        if entity == "supplier":
            return map_supplier(U8Supplier.model_validate(row), self.ctx)
        if entity == "item":
            return map_item(U8Item.model_validate(row), self.ctx)
        if entity == "purchase_order":
            return map_purchase_order(U8PurchaseOrder.model_validate(row), self.ctx)
        return map_arrival_vouch(U8ArrivalVouch.model_validate(row), self.ctx)

    async def read_typed(
        self,
        entity: str,
        *,
        code: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        limit: int = 100,
        items: Mapping[str, Item] | None = None,
    ) -> list[MappingOutcome[Any]]:
        """带缺失清单的读：S2/S5 需要知道「哪一行为什么不可用」，不只是结果集。"""

        route = OBJECT_ROUTES[entity]
        params = {"limit": limit}
        if code:
            params["code"] = code
        if date_from:
            params["date_from"] = date_from
        if date_to:
            params["date_to"] = date_to
        try:
            rows = await self._transport.get(route, params)
        except Exception:
            self._health.note_failure()
            raise
        outcomes: list[MappingOutcome[Any]] = []
        for row in rows:
            if entity == "supplier":
                outcomes.append(map_supplier(U8Supplier.model_validate(row), self.ctx))
            elif entity == "item":
                outcomes.append(map_item(U8Item.model_validate(row), self.ctx))
            elif entity == "purchase_order":
                outcomes.append(
                    map_purchase_order(U8PurchaseOrder.model_validate(row), self.ctx, items=items)
                )
            else:
                outcomes.append(
                    map_arrival_vouch(U8ArrivalVouch.model_validate(row), self.ctx, items=items)
                )
        self._health.note_success(synced=True)
        return outcomes

    # ------------------------------------------------------------------- preview
    async def preview(self, action: ActionSpec, payload: Mapping[str, Any]) -> PreviewResult:
        if action.key not in ACTIONS_BY_KEY:
            raise ValueError(f"动作 {action.key} 不属于 u8 连接器")
        source_doc_no = str(payload.get("source_doc_no") or "").strip()
        if not source_doc_no:
            # 没有业务键 → 拿不到前像 → 由网关判零写入，不在这里编一个假前像。
            return PreviewResult(
                diff=(),
                before=None,
                verify_spec=action.verify or VerifySpec(kind="read_by_business_key"),
            )
        existing = await self._transport.get_draft(action.key, source_doc_no)
        before: Mapping[str, Any] = (
            dict(existing)
            if existing is not None
            else {"exists": False, "source_doc_no": source_doc_no}
        )
        diff: list[FieldDiffEntry] = []
        for payload_key, draft_key in _DIFF_PAIRS:
            after = (
                _line_count(payload.get("lines"))
                if payload_key == "line_count"
                else payload.get(payload_key)
            )
            before_value = (
                _line_count(existing.get("details"))
                if (draft_key == "details" and existing is not None)
                else (existing.get(draft_key) if existing is not None else None)
            )
            if before_value != after:
                diff.append(
                    FieldDiffEntry(field=payload_key, before=before_value, after=after)
                )
        return PreviewResult(
            diff=tuple(diff),
            before=before,
            verify_spec=action.verify or VerifySpec(kind="read_by_business_key"),
        )

    # -------------------------------------------------------------------- verify
    async def verify(self, spec: VerifySpec, payload: Mapping[str, Any]) -> VerifyResult:
        action_key = _action_key_of_spec(spec)
        source_doc_no = str(payload.get("source_doc_no") or "").strip()
        if action_key is None or not source_doc_no:
            return VerifyResult(status="absent", observed=None)
        observed = await self._transport.get_draft(action_key, source_doc_no)
        if observed is None:
            return VerifyResult(status="absent", observed=None)

        expected_lines = _line_count(payload.get("lines"))
        checks = (
            observed.get("cVenCode") == payload.get("supplier_code"),
            observed.get("dPODate") == payload.get("doc_date"),
            _line_count(observed.get("details")) == expected_lines,
        )
        return VerifyResult(
            status="match" if all(checks) else "mismatch",
            observed=dict(observed),
        )

    # ---------------------------------------------------------------- compensate
    async def compensate(self, comp: Any, *, idem_key: str) -> CompensateResult:
        action_key = _comp_action_key(comp)
        external_id = _comp_external_id(comp)
        if action_key is None or external_id is None:
            return CompensateResult(
                status="failed",
                detail={"reason": "补偿缺少 action_key 或 external_id"},
            )
        deleted = await self._transport.delete_draft(action_key, external_id)
        if deleted:
            return CompensateResult(
                status="compensated", detail={"external_id": external_id, "idem_key": idem_key}
            )
        return CompensateResult(
            status="unchanged", detail={"external_id": external_id, "reason": "目标草稿已不存在"}
        )

    # -------------------------------------------------------------------- health
    async def health(self) -> Health:
        return self._health.snapshot(
            ok=self.breaker.state is BreakerState.CLOSED,
            rate_limit_remaining=self._transport.rate_limit_remaining,
        )


def _describe_error(exc: BaseException) -> str:
    kind = getattr(exc, "kind", None)
    return f"{getattr(kind, 'value', kind or type(exc).__name__)}:{exc}"


def _action_key_of_spec(spec: VerifySpec) -> str | None:
    if spec.kind == "read_po_draft_by_source":
        return PO_DRAFT_KEY
    if spec.kind == "read_arrival_draft_by_source":
        return "u8.arrival.create_draft"
    return None


def _comp_action_key(comp: Any) -> str | None:
    if isinstance(comp, Mapping):
        return comp.get("action_key")
    return getattr(comp, "action_key", None)


def _comp_external_id(comp: Any) -> str | None:
    candidates = (
        comp.get("external_id") if isinstance(comp, Mapping) else getattr(comp, "external_id", None),
        comp.get("after_ref") if isinstance(comp, Mapping) else getattr(comp, "after_ref", None),
        comp.get("draft_no") if isinstance(comp, Mapping) else getattr(comp, "draft_no", None),
    )
    for value in candidates:
        if value:
            return str(value)
    return None
