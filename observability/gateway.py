"""连接器埋点代理：把预演 / 写入 / 校验 / 补偿放进同一条 trace。

用法（真实链路不变，``WriteGateway`` 仍是唯一写出口）::

    trace = tracer.start_trace(ticket_id="T-1", agent="AG-1")
    gateway = WriteGateway(
        TracedConnector(connector, tracer, trace),
        approvals=...,
    )
    with tracer.step(trace, step_no=1, action_key=..., level=..., system=...):
        result = await gateway.execute(...)

代理只包一层 span，不改变任何返回值 / 异常语义：异常原样抛出，span 标记 ``error``。
"""

from __future__ import annotations

from contextlib import AbstractContextManager
from typing import Any, Mapping

from observability.trace import Span, Trace, Tracer


class TracedConnector:
    """``Connector`` 协议代理；读 / 健康检查直接透传，写路径记录 span。"""

    def __init__(self, connector: Any, tracer: Tracer, trace: Trace) -> None:
        self._connector = connector
        self._tracer = tracer
        self._trace = trace

    # -- 透传 ---------------------------------------------------------
    @property
    def meta(self) -> Any:
        return self._connector.meta

    @property
    def system(self) -> str:
        return str(getattr(self._connector.meta, "system", "unknown"))

    def describe(self) -> Any:
        return self._connector.describe()

    async def read(self, entity: str, query: Any) -> Any:
        return await self._connector.read(entity, query)

    async def health(self) -> Any:
        return await self._connector.health()

    # -- 埋点 ---------------------------------------------------------
    async def preview(self, action: Any, payload: Mapping[str, Any]) -> Any:
        with self._tracer.span(
            self._trace,
            "gateway.preview",
            connector=self.system,
            action_key=getattr(action, "key", "?"),
            field_count=len(payload),
        ) as span:
            result = await self._connector.preview(action, payload)
            span.set(verify_kind=getattr(result.verify_spec, "kind", None))
            return result

    async def write(
        self,
        action: Any,
        payload: Mapping[str, Any],
        *,
        ticket_id: str,
        idem_key: str,
    ) -> Any:
        with self._tracer.span(
            self._trace,
            "gateway.write",
            connector=self.system,
            action_key=getattr(action, "key", "?"),
            level=getattr(getattr(action, "level", None), "name", None),
            idem_key=idem_key,
            field_count=len(payload),
        ) as span:
            result = await self._connector.write(
                action, payload, ticket_id=ticket_id, idem_key=idem_key
            )
            span.set(
                external_id=getattr(result, "external_id", None),
                http_status=_http_status(result),
            )
            return result

    async def verify(self, spec: Any, payload: Mapping[str, Any]) -> Any:
        with self._tracer.span(
            self._trace,
            "gateway.verify",
            connector=self.system,
            verify_kind=getattr(spec, "kind", None),
        ) as span:
            result = await self._connector.verify(spec, payload)
            span.set(verify_result=getattr(result, "status", None))
            return result

    async def compensate(self, comp: Any, *, idem_key: str) -> Any:
        with self._tracer.span(
            self._trace,
            "gateway.compensate",
            connector=self.system,
            action_key=getattr(comp, "action_key", "?"),
            original_idem_key=getattr(comp, "original_idem_key", None),
            idem_key=idem_key,
        ) as span:
            result = await self._connector.compensate(comp, idem_key=idem_key)
            span.set(compensate_status=getattr(result, "status", None))
            return result


def _http_status(result: Any) -> int | None:
    """连接器返回体里若显式给出状态码就记录；没有就保持 ``None``。"""

    response = getattr(result, "response", None)
    if isinstance(response, Mapping):
        status = response.get("http_status", response.get("status_code"))
        if isinstance(status, int) and not isinstance(status, bool):
            return status
    return None


def audit_write_span(
    tracer: Tracer,
    trace: Trace,
    *,
    image_id: str,
    hash_prev: str,
) -> AbstractContextManager[Span]:
    """审计影像写入的 span 上下文；调用方 ``with`` 使用即可。"""

    return tracer.span(trace, "audit.write", image_id=image_id, hash_prev=hash_prev)
