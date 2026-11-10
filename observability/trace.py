"""统一 trace / span 约定与最小埋点封装（标准库实现，无外网依赖）。

约定见 ``observability/README.md``：

- 一次工单执行 = 一个 ``trace_id``（W3C 32 位小写十六进制）；
- span 名称是冻结枚举 :data:`KNOWN_SPANS`，不得自造；
- 属性必填项：``ticket_id`` / ``agent`` / ``step_no`` / ``level`` / ``system``；
- 导出只写本地 JSON Lines 文件，不接任何 SaaS APM（L3 数据不出域）。

本模块只依赖标准库；不导入 ``apps.*``，避免观测层反向依赖业务实现。
"""

from __future__ import annotations

import contextvars
import re
import secrets
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any
from uuid import UUID

# ---------------------------------------------------------------- 冻结约定

#: 允许出现在 trace 里的 span 名称（与 tech 01 §3.7.1 的追踪树一一对应）。
KNOWN_SPANS: frozenset[str] = frozenset(
    {
        "ticket.step",
        "agent.decision",
        "gateway.preview",
        "gateway.write",
        "gateway.verify",
        "gateway.compensate",
        "llm.chat",
        "audit.write",
    }
)

#: 每个 span 都自动携带的上下文字段；缺失即视为埋点不规范。
REQUIRED_ATTRIBUTES: tuple[str, ...] = ("ticket_id", "agent")

#: ``ticket.step`` span 额外必填的字段。
STEP_ATTRIBUTES: tuple[str, ...] = ("step_no", "level", "system")

TRACEPARENT_RE = re.compile(
    r"^00-(?P<trace>[0-9a-f]{32})-(?P<span>[0-9a-f]{16})-(?P<flags>[0-9a-f]{2})$"
)


class TraceConventionError(ValueError):
    """埋点不符合约定（未知 span 名、缺必填属性、非法 traceparent）。"""


def new_trace_id() -> str:
    """32 位小写十六进制 trace id（W3C 口径）。"""

    return secrets.token_hex(16)


def new_span_id() -> str:
    """16 位小写十六进制 span id（W3C 口径）。"""

    return secrets.token_hex(8)


def format_traceparent(trace_id: str, span_id: str, *, sampled: bool = True) -> str:
    """生成 ``traceparent`` 头值；同时校验两个 id 的格式与全零禁用规则。"""

    _validate_trace_id(trace_id)
    _validate_span_id(span_id)
    return f"00-{trace_id}-{span_id}-{'01' if sampled else '00'}"


def parse_traceparent(value: str) -> tuple[str, str, bool] | None:
    """解析 ``traceparent``；非法或全零 id 返回 ``None``（不抛错，便于中间件降级）。"""

    match = TRACEPARENT_RE.match(value.strip().lower())
    if match is None:
        return None
    trace_id, span_id = match.group("trace"), match.group("span")
    if trace_id == "0" * 32 or span_id == "0" * 16:
        return None
    return trace_id, span_id, match.group("flags") == "01"


def _validate_trace_id(value: str) -> None:
    if len(value) != 32 or any(char not in "0123456789abcdef" for char in value):
        raise TraceConventionError(f"trace_id 必须是 32 位小写十六进制，实际 {value!r}")
    if value == "0" * 32:
        raise TraceConventionError("trace_id 不得为全零")


def _validate_span_id(value: str) -> None:
    if len(value) != 16 or any(char not in "0123456789abcdef" for char in value):
        raise TraceConventionError(f"span_id 必须是 16 位小写十六进制，实际 {value!r}")
    if value == "0" * 16:
        raise TraceConventionError("span_id 不得为全零")


# ---------------------------------------------------------------- 归一化

_SENSITIVE_KEY_PARTS = ("credential", "password", "secret", "authorization")


def _is_sensitive(key: str) -> bool:
    lowered = key.lower()
    return any(part in lowered for part in _SENSITIVE_KEY_PARTS)


def normalize(value: Any) -> Any:
    """把属性值转成可 JSON 序列化的形式；敏感键由 :func:`_sanitize` 处理。"""

    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Enum):
        return value.name
    if isinstance(value, (UUID, Decimal, datetime, date)):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): normalize(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [normalize(item) for item in value]
    return str(value)


def _sanitize(attributes: Mapping[str, Any]) -> dict[str, Any]:
    cleaned: dict[str, Any] = {}
    for key, value in attributes.items():
        cleaned[key] = "[REDACTED]" if _is_sensitive(key) else normalize(value)
    return cleaned


def _level_name(value: Any) -> str | None:
    """把 ``WriteLevel`` / 0–3 / ``"W1"`` 统一成 ``W0``–``W3``。"""

    if value is None:
        return None
    name = getattr(value, "name", None)
    if isinstance(name, str) and name.upper() in {"W0", "W1", "W2", "W3"}:
        return name.upper()
    if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 3:
        return f"W{value}"
    if isinstance(value, str) and value.strip().upper() in {"W0", "W1", "W2", "W3"}:
        return value.strip().upper()
    return str(value)


# ---------------------------------------------------------------- 数据结构


@dataclass
class Span:
    """一次可观测操作的记录；由 :class:`Tracer` 创建，``finish()`` 后只读。"""

    name: str
    trace_id: str
    span_id: str
    parent_id: str | None
    attributes: dict[str, Any]
    started_at: datetime
    started_monotonic: float
    sequence: int = 0
    ended_at: datetime | None = None
    duration_ms: float | None = None
    status: str = "ok"  # ok | error | rejected
    events: list[dict[str, Any]] = field(default_factory=list)

    # -- 写入 ---------------------------------------------------------
    def set(self, **attributes: Any) -> Span:
        """补充/覆盖属性；值在写入时归一化。"""

        self.attributes.update(_sanitize(attributes))
        return self

    def add_event(self, name: str, **attributes: Any) -> Span:
        self.events.append(
            {
                "name": name,
                "ts": datetime.now(tz=UTC).isoformat(),
                "attributes": _sanitize(attributes),
            }
        )
        return self

    def mark_error(self, error: BaseException | str) -> Span:
        self.status = "error"
        self.attributes["error"] = normalize(error)
        return self

    def mark_rejected(self, code: str) -> Span:
        """策略拒绝（W3 / 越权 / 审批）不是技术错误，单独标记便于统计。"""

        self.status = "rejected"
        self.attributes["reject_code"] = normalize(code)
        return self

    def finish(self, error: BaseException | None = None) -> None:
        if self.ended_at is not None:
            return
        if error is not None and self.status == "ok":
            self.mark_error(error)
        self.ended_at = datetime.now(tz=UTC)
        self.duration_ms = round((time.perf_counter() - self.started_monotonic) * 1000, 3)

    # -- 读取 ---------------------------------------------------------
    @property
    def duration_text(self) -> str:
        if self.duration_ms is None:
            return "-"
        if self.duration_ms < 1_000:
            return f"{self.duration_ms:.1f}ms"
        return f"{self.duration_ms / 1000:.2f}s"

    def to_dict(self) -> dict[str, Any]:
        """扁平导出：必填字段提到顶层，方便 grep 与按工单检索。"""

        attrs = self.attributes
        return {
            "name": self.name,
            "trace_id": self.trace_id,
            "span_id": self.span_id,
            "parent_id": self.parent_id,
            "ticket_id": attrs.get("ticket_id"),
            "agent": attrs.get("agent"),
            "step_no": attrs.get("step_no"),
            "level": attrs.get("level"),
            "system": attrs.get("system"),
            "action_key": attrs.get("action_key"),
            "status": self.status,
            "started_at": self.started_at.isoformat(),
            "ended_at": self.ended_at.isoformat() if self.ended_at else None,
            "duration_ms": self.duration_ms,
            "attributes": attrs,
            "events": list(self.events),
        }


class Trace:
    """一次工单执行的 span 集合；由 :meth:`Tracer.start_trace` 创建。"""

    def __init__(
        self,
        *,
        trace_id: str,
        ticket_id: str,
        agent: str,
        tracer: Tracer,
    ) -> None:
        self.trace_id = trace_id
        self.ticket_id = ticket_id
        self.agent = agent
        self._tracer = tracer
        self._spans: list[Span] = []
        self._lock = threading.Lock()

    def _add(self, span: Span) -> None:
        with self._lock:
            span.sequence = len(self._spans)
            self._spans.append(span)

    @property
    def spans(self) -> tuple[Span, ...]:
        with self._lock:
            return tuple(sorted(self._spans, key=lambda item: item.sequence))

    @property
    def root_spans(self) -> tuple[Span, ...]:
        ids = {span.span_id for span in self.spans}
        return tuple(span for span in self.spans if span.parent_id not in ids)

    @property
    def status(self) -> str:
        statuses = {span.status for span in self.spans}
        if "error" in statuses:
            return "error"
        if "rejected" in statuses:
            return "rejected"
        return "ok"

    @property
    def duration_ms(self) -> float:
        durations = [span.duration_ms for span in self.spans if span.duration_ms]
        return round(sum(durations), 3)

    def to_dict(self) -> dict[str, Any]:
        return {
            "trace_id": self.trace_id,
            "ticket_id": self.ticket_id,
            "agent": self.agent,
            "status": self.status,
            "duration_ms": self.duration_ms,
            "spans": [span.to_dict() for span in self.spans],
        }

    # -- 便捷入口 -----------------------------------------------------
    def span(self, name: str, **attributes: Any):
        return self._tracer.span(self, name, **attributes)

    def step(self, **kwargs: Any):
        return self._tracer.step(self, **kwargs)

    def decision(self, **kwargs: Any):
        return self._tracer.decision(self, **kwargs)


class Tracer:
    """进程内埋点入口。``exporter=None`` 时只保留内存记录，不落盘。"""

    def __init__(
        self,
        *,
        exporter: Any = None,
        clock: Callable[[], datetime] | None = None,
        monotonic: Callable[[], float] | None = None,
    ) -> None:
        self._exporter = exporter
        self._clock = clock or (lambda: datetime.now(tz=UTC))
        self._monotonic = monotonic or time.perf_counter
        self._current: contextvars.ContextVar[Span | None] = contextvars.ContextVar(
            f"smart_erp_current_span_{id(self)}", default=None
        )

    def start_trace(
        self,
        *,
        ticket_id: str,
        agent: str,
        trace_id: str | None = None,
    ) -> Trace:
        if not str(ticket_id).strip():
            raise TraceConventionError("trace 必须绑定 ticket_id（I-1）")
        if not str(agent).strip():
            raise TraceConventionError("trace 必须记录 agent 名称")
        resolved = trace_id or new_trace_id()
        _validate_trace_id(resolved)
        return Trace(trace_id=resolved, ticket_id=str(ticket_id), agent=str(agent), tracer=self)

    @contextmanager
    def span(
        self,
        trace: Trace,
        name: str,
        *,
        parent: Span | None = None,
        **attributes: Any,
    ) -> Iterator[Span]:
        """创建 span；未显式给 ``parent`` 时自动挂到当前 span 下。"""

        if name not in KNOWN_SPANS:
            raise TraceConventionError(
                f"未知 span 名 {name!r}；允许值：{sorted(KNOWN_SPANS)}"
            )

        inherited = {"ticket_id": trace.ticket_id, "agent": trace.agent}

        current = self._current.get()
        same_trace = current is not None and current.trace_id == trace.trace_id
        if same_trace:
            # 子 span 继承步骤上下文，保证任何一条日志都能按步骤号/等级/系统检索。
            for key in ("step_no", "level", "system", "action_key"):
                value = current.attributes.get(key)
                if value is not None:
                    inherited[key] = value

        inherited.update(attributes)
        inherited = _sanitize(inherited)

        if parent is not None:
            parent_id = parent.span_id
        elif same_trace:
            parent_id = current.span_id
        else:
            parent_id = None

        span = Span(
            name=name,
            trace_id=trace.trace_id,
            span_id=new_span_id(),
            parent_id=parent_id,
            attributes=inherited,
            started_at=self._clock(),
            started_monotonic=self._monotonic(),
        )
        trace._add(span)
        token = self._current.set(span)
        error: BaseException | None = None
        try:
            yield span
        except BaseException as exc:
            error = exc
            raise
        finally:
            self._current.reset(token)
            span.finish(error)
            if self._exporter is not None:
                self._exporter.export(span)

    @contextmanager
    def step(
        self,
        trace: Trace,
        *,
        step_no: int,
        action_key: str,
        level: Any,
        system: str,
        agent: str | None = None,
        parent: Span | None = None,
    ) -> Iterator[Span]:
        """工单步骤 span；自动带上 step_no / action_key / level / system。"""

        if int(step_no) < 1:
            raise TraceConventionError(f"step_no 从 1 开始，实际 {step_no!r}")
        if not str(action_key).strip():
            raise TraceConventionError("step 必须记录 action_key")
        if not str(system).strip():
            raise TraceConventionError("step 必须记录 system")
        with self.span(
            trace,
            "ticket.step",
            parent=parent,
            step_no=int(step_no),
            action_key=str(action_key),
            level=_level_name(level),
            system=str(system),
            **({"agent": agent} if agent else {}),
        ) as span:
            yield span

    @contextmanager
    def decision(
        self,
        trace: Trace,
        *,
        kind: str,
        rule_id: str | None = None,
        model: str | None = None,
        prompt_version: str | None = None,
        confidence: float | None = None,
        parent: Span | None = None,
        **extra: Any,
    ) -> Iterator[Span]:
        """Agent 决策 span；``kind`` 取 match/attribution/route/draft（tech 01 §3.7.2）。"""

        if kind not in {"match", "attribution", "route", "draft"}:
            raise TraceConventionError(
                f"decision_kind 必须是 match/attribution/route/draft，实际 {kind!r}"
            )
        if confidence is not None and not 0 <= float(confidence) <= 1:
            raise TraceConventionError(f"confidence 必须在 0..1，实际 {confidence!r}")
        with self.span(
            trace,
            "agent.decision",
            parent=parent,
            decision_kind=kind,
            rule_id=rule_id,
            model=model,
            prompt_version=prompt_version,
            confidence=confidence,
            **extra,
        ) as span:
            yield span
