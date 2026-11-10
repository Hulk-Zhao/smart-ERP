"""文件通道连接器（对账单/送货单 CSV，**只读导入**）。

写回不在本连接器内：文件通道没有可信的预览/校验/补偿，硬做等于绕开
``WriteGateway`` 的四路取严。因此 ``describe().actions`` 为空，
``write/preview/verify/compensate`` 一律抛 ``FORBIDDEN``，而不是返回假的成功。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any

from apps.connectors.breaker import BreakerState, CircuitBreaker
from apps.connectors.canonical.mapping import MappingContext, MappingOutcome
from apps.connectors.canonical.models import Document
from apps.connectors.errors import ConnectorError, ErrorKind
from apps.connectors.file.mapping import map_rows
from apps.connectors.file.spec import (
    CONNECTOR_VERSION,
    DEFAULT_READ_LIMIT,
    ENTITY_KINDS,
    OPTIONAL_COLUMNS,
    REQUIRED_COLUMNS,
    SYSTEM_NAME,
)
from apps.connectors.file.transport import FileReadPayload, FileTransport
from apps.connectors.health import HealthTracker
from apps.platform.connectors.sdk import (
    BaseConnector,
    CapabilitySet,
    CompensateResult,
    ConnectorMeta,
    DegradedReason,
    Health,
    PreviewResult,
    ReadQuery,
    ReadResult,
    VerifyResult,
    WriteResult,
)
from apps.platform.policy.levels import ActionSpec, RateLimit, VerifySpec

_READ_ONLY_REASON = (
    "文件通道只做只读导入：没有 ActionSpec，写回必须经过 WriteGateway 的四路取严"
)


class FileConnector(BaseConnector):
    def __init__(
        self,
        transport: FileTransport,
        *,
        ctx: MappingContext | None = None,
        rate_limit: RateLimit | None = None,
    ) -> None:
        super().__init__(transport)
        self.transport = transport
        self.ctx = ctx or MappingContext(connector=SYSTEM_NAME, authority_system="supplier")
        self._rate_limit = rate_limit or DEFAULT_READ_LIMIT
        self._fingerprint = _fingerprint()
        self.meta = ConnectorMeta(
            system=SYSTEM_NAME,
            version=CONNECTOR_VERSION,
            access_level="L1",
            security_level="A",
            supports_obo=False,
            version_fingerprint=self._fingerprint,
        )
        self._health = HealthTracker(
            system=SYSTEM_NAME, version_fingerprint=self._fingerprint
        )

    @property
    def breaker(self) -> CircuitBreaker:
        return self.transport.breaker

    # ---- 六能力：能力声明 -------------------------------------------------
    def describe(self) -> CapabilitySet:
        return CapabilitySet(
            objects=tuple(ENTITY_KINDS),
            actions=(),  # 只读：不声明任何写动作
            authority_map={entity: "supplier" for entity in ENTITY_KINDS},
            rate_limit=self._rate_limit,
        )

    # ---- 六能力：读（限流/重试/幂等/超时/熔断/错误分类在 transport 内） ----
    async def read(self, entity: str, query: ReadQuery) -> ReadResult:
        if entity not in ENTITY_KINDS:
            return ReadResult(
                items=(),
                completeness="unavailable",
                degraded=(
                    DegradedReason(
                        source=SYSTEM_NAME, reason=f"unsupported_entity:{entity}"
                    ),
                ),
            )

        try:
            payload = await self.transport.read(entity, _params(query))
        except ConnectorError as err:
            self._health.note_failure()
            return ReadResult(
                items=(),
                completeness="unavailable",
                degraded=(
                    DegradedReason(
                        source=SYSTEM_NAME,
                        reason=f"read_failed:{err.kind.value}",
                    ),
                ),
            )
        except Exception as exc:  # noqa: BLE001 - 未分类异常也要体现为不可用，不能伪装成空集
            self._health.note_failure()
            return ReadResult(
                items=(),
                completeness="unavailable",
                degraded=(
                    DegradedReason(source=SYSTEM_NAME, reason=f"read_failed:{exc!r}"),
                ),
            )

        outcomes, degraded = self._outcomes(entity, payload, query)
        self._health.note_success(synced=True)

        items = tuple(
            outcome.record.model_dump()
            for outcome in outcomes
            if outcome.record is not None
        )
        blocked = sum(1 for outcome in outcomes if outcome.record is None)

        if not items:
            if not degraded:
                # 读到空（只有表头）是合法结果，不是「读不到」。
                return ReadResult(items=(), completeness="complete")
            # 有行级错误/映射阻断且没有可用单据：读不到可用数据，必须带退化原因。
            return ReadResult(
                items=(), completeness="unavailable", degraded=tuple(degraded)
            )

        completeness = "partial" if degraded or blocked else "complete"
        return ReadResult(
            items=items, completeness=completeness, degraded=tuple(degraded)
        )

    async def read_typed(
        self, entity: str, query: ReadQuery
    ) -> list[MappingOutcome[Document]]:
        """带缺失清单的读取；传输失败直接抛 ``ConnectorError``（不吞错）。"""

        if entity not in ENTITY_KINDS:
            raise ConnectorError(
                ErrorKind.INVALID, f"文件通道不支持的实体: {entity!r}"
            )
        try:
            payload = await self.transport.read(entity, _params(query))
        except ConnectorError:
            self._health.note_failure()
            raise
        outcomes, _ = self._outcomes(entity, payload, query)
        self._health.note_success(synced=True)
        return outcomes

    # ---- 只读守卫：下列能力在文件通道不存在 -------------------------------
    async def write(
        self,
        action: ActionSpec,
        payload: Mapping[str, Any],
        *,
        ticket_id: str,
        idem_key: str,
    ) -> WriteResult:
        raise ConnectorError(ErrorKind.FORBIDDEN, _READ_ONLY_REASON)

    async def preview(
        self, action: ActionSpec, payload: Mapping[str, Any]
    ) -> PreviewResult:
        raise ConnectorError(ErrorKind.FORBIDDEN, _READ_ONLY_REASON)

    async def verify(
        self, spec: VerifySpec, payload: Mapping[str, Any]
    ) -> VerifyResult:
        raise ConnectorError(ErrorKind.FORBIDDEN, _READ_ONLY_REASON)

    async def compensate(self, comp: Any, *, idem_key: str) -> CompensateResult:
        raise ConnectorError(ErrorKind.FORBIDDEN, _READ_ONLY_REASON)

    # ---- 六能力：健康 -----------------------------------------------------
    async def health(self) -> Health:
        return self._health.snapshot(
            ok=self.transport.breaker.state is BreakerState.CLOSED,
            rate_limit_remaining=self.transport.rate_limit_remaining,
        )

    # ---- 内部 -------------------------------------------------------------
    def _outcomes(
        self, entity: str, payload: FileReadPayload, query: ReadQuery
    ) -> tuple[list[MappingOutcome[Document]], list[DegradedReason]]:
        filters = dict(query.filters)
        doc_no = _opt(filters.get("doc_no"))
        supplier_code = _opt(filters.get("supplier_code")) or _opt(
            filters.get("party_code")
        )
        date_from = _opt(filters.get("date_from"))
        date_to = _opt(filters.get("date_to"))
        period = _opt(filters.get("period"))

        rows = payload.rows
        if period is not None:
            rows = tuple(
                row
                for row in rows
                if _opt(row.values.get("period")) == period
            )

        degraded: list[DegradedReason] = []
        for error in payload.row_errors:
            degraded.append(
                DegradedReason(
                    source=SYSTEM_NAME,
                    reason=f"row:{error.file}:{error.line_no}:{error.detail}",
                )
            )
        if payload.ignored_columns:
            degraded.append(
                DegradedReason(
                    source=SYSTEM_NAME,
                    reason="ignored_columns:" + ",".join(payload.ignored_columns),
                )
            )

        outcomes = map_rows(rows, entity=entity, ctx=self.ctx)
        selected: list[MappingOutcome[Document]] = []
        for outcome in outcomes:
            record = outcome.record
            if record is None:
                # 阻断单据只在无单据号过滤时保留，便于上游看到「为什么没进来」。
                if doc_no is None:
                    selected.append(outcome)
                continue
            if doc_no is not None and record.doc_no != doc_no:
                continue
            if supplier_code is not None and record.party_code != supplier_code:
                continue
            if date_from is not None and record.doc_date < date_from:
                continue
            if date_to is not None and record.doc_date > date_to:
                continue
            selected.append(outcome)

        selected = selected[: max(1, int(query.limit))]
        for outcome in selected:
            for issue in outcome.issues:
                degraded.append(
                    DegradedReason(
                        source=SYSTEM_NAME,
                        reason=(
                            f"mapping:{issue.reason}:{issue.field}"
                            f"@{issue.doc_no or '-'}"
                        ),
                    )
                )
        return selected, degraded


def _opt(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _params(query: ReadQuery) -> dict[str, Any]:
    filters = dict(query.filters)
    return {
        "file": _opt(filters.get("file")),
        "filters": {
            "doc_no": _opt(filters.get("doc_no")),
            "supplier_code": _opt(filters.get("supplier_code"))
            or _opt(filters.get("party_code")),
            "date_from": _opt(filters.get("date_from")),
            "date_to": _opt(filters.get("date_to")),
            "period": _opt(filters.get("period")),
        },
        "limit": max(1, int(query.limit)),
    }


def _fingerprint() -> str:
    payload = {
        "system": SYSTEM_NAME,
        "version": CONNECTOR_VERSION,
        "entities": sorted(ENTITY_KINDS),
        "required": REQUIRED_COLUMNS,
        "optional": OPTIONAL_COLUMNS,
    }
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()
