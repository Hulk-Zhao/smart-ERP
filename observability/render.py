"""trace 文本树与「决策 → 预演 → 写入 → 校验 → 补偿」链路还原。

只读内存里的 span，不依赖观测后端；排障时把 :func:`render_trace` 或
:func:`render_decision_chain` 的输出贴进工单即可（NFR-O-03）。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from observability.trace import Span, Trace

#: 决策链的固定五段，顺序即验收口径。
CHAIN_PHASES: tuple[str, ...] = (
    "decision",
    "preview",
    "write",
    "verify",
    "compensation",
)

PHASE_LABELS: dict[str, str] = {
    "decision": "决策",
    "preview": "预演",
    "write": "写入",
    "verify": "校验",
    "compensation": "补偿",
}

SPAN_TO_PHASE: dict[str, str] = {
    "agent.decision": "decision",
    "gateway.preview": "preview",
    "gateway.write": "write",
    "gateway.verify": "verify",
    "gateway.compensate": "compensation",
}


def _spans_of(source: Trace | Sequence[Span]) -> tuple[Span, ...]:
    if isinstance(source, Trace):
        return source.spans
    return tuple(source)


def _tree_prefix(flags: Sequence[bool]) -> str:
    """``flags`` 表示每层是否为「最后一项」，末位是本节点自身。"""

    parts: list[str] = []
    for is_last in flags[:-1]:
        parts.append("   " if is_last else "│  ")
    parts.append("└─ " if flags[-1] else "├─ ")
    return "".join(parts)


def _status_text(span: Span) -> str:
    text = f"{span.status}, {span.duration_text}"
    if span.status == "rejected" and span.attributes.get("reject_code"):
        text += f", code={span.attributes['reject_code']}"
    if span.status == "error" and span.attributes.get("error"):
        text += f", {span.attributes['error']}"
    return text


def _span_summary(span: Span) -> str:
    attrs = span.attributes
    name = span.name
    parts: list[str] = []

    if name == "ticket.step":
        parts.append(f"#{attrs.get('step_no')}")
        parts.append(f"action={attrs.get('action_key')}")
        parts.append(f"level={attrs.get('level')}")
        parts.append(f"system={attrs.get('system')}")
    elif name == "agent.decision":
        parts.append(f"kind={attrs.get('decision_kind')}")
        if attrs.get("rule_id"):
            parts.append(f"rule={attrs['rule_id']}")
        if attrs.get("model"):
            parts.append(f"model={attrs['model']}")
        if attrs.get("confidence") is not None:
            parts.append(f"confidence={attrs['confidence']}")
    elif name == "gateway.preview":
        parts.append(f"connector={attrs.get('connector')}")
        parts.append(f"fields={attrs.get('field_count')}")
    elif name == "gateway.write":
        parts.append(f"level={attrs.get('level')}")
        parts.append(f"idem={attrs.get('idem_key')}")
        if attrs.get("external_id"):
            parts.append(f"external_id={attrs['external_id']}")
    elif name == "gateway.verify":
        parts.append(f"result={attrs.get('verify_result')}")
    elif name == "gateway.compensate":
        parts.append(f"action={attrs.get('action_key')}")
        parts.append(f"status={attrs.get('compensate_status')}")
    elif name == "llm.chat":
        parts.append(f"model={attrs.get('model')}")
        parts.append(f"prompt={attrs.get('prompt_version')}")
        if attrs.get("input_tokens") is not None:
            parts.append(f"in={attrs['input_tokens']}")
        if attrs.get("output_tokens") is not None:
            parts.append(f"out={attrs['output_tokens']}")
        if attrs.get("cost_micros") is not None:
            parts.append(f"cost_micros={attrs['cost_micros']}")
    elif name == "audit.write":
        parts.append(f"image={attrs.get('image_id')}")
        parts.append(f"hash_prev={str(attrs.get('hash_prev') or '')[:12]}")
    else:  # pragma: no cover - KNOWN_SPANS 已封闭，保留兜底
        parts.extend(f"{key}={value}" for key, value in attrs.items())

    summary = " ".join(str(part) for part in parts)
    return f"{name} {summary}".rstrip()


def render_trace(source: Trace | Sequence[Span], *, include_events: bool = False) -> str:
    """把一条 trace 渲染成可读文本树（按父子关系与开始时间排序）。"""

    spans = _spans_of(source)
    if not spans:
        return "trace (empty)"

    first = spans[0]
    trace_id = first.trace_id
    ticket_id = first.attributes.get("ticket_id")
    agent = first.attributes.get("agent")
    statuses = {span.status for span in spans}
    status = "error" if "error" in statuses else "rejected" if "rejected" in statuses else "ok"
    duration = sum(span.duration_ms or 0.0 for span in spans)

    lines = [
        (
            f"trace {trace_id} ticket={ticket_id} agent={agent} "
            f"spans={len(spans)} status={status} duration={duration:.1f}ms"
        )
    ]

    by_id = {span.span_id: span for span in spans}
    children: dict[str | None, list[Span]] = {}
    for span in spans:
        parent = span.parent_id if span.parent_id in by_id else None
        children.setdefault(parent, []).append(span)

    ordered = sorted(spans, key=lambda item: (item.started_at, item.sequence))
    order_index = {span.span_id: index for index, span in enumerate(ordered)}

    def walk(parent_id: str | None, flags: list[bool]) -> None:
        items = sorted(
            children.get(parent_id, []), key=lambda item: order_index[item.span_id]
        )
        for index, span in enumerate(items):
            is_last = index == len(items) - 1
            prefix = _tree_prefix([*flags, is_last])
            lines.append(f"{prefix}{_span_summary(span)} ({_status_text(span)})")
            if include_events:
                for event in span.events:
                    lines.append(f"{prefix}   · {event['name']} {event['attributes']}")
            walk(span.span_id, [*flags, is_last])

    walk(None, [])
    return "\n".join(lines)


# ---------------------------------------------------------------- 决策链


@dataclass(frozen=True, slots=True)
class StepChain:
    """一个工单步骤下的五段链路（缺失段为 ``None``，不补假数据）。"""

    step: Span
    spans: tuple[Span, ...]

    @property
    def step_no(self) -> int:
        return int(self.step.attributes.get("step_no") or 0)

    @property
    def action_key(self) -> str:
        return str(self.step.attributes.get("action_key") or "")

    def phase(self, phase: str) -> Span | None:
        matched = [
            span
            for span in self.spans
            if SPAN_TO_PHASE.get(span.name) == phase
        ]
        return matched[-1] if matched else None

    @property
    def phases(self) -> tuple[str, ...]:
        return tuple(phase for phase in CHAIN_PHASES if self.phase(phase) is not None)

    @property
    def missing(self) -> tuple[str, ...]:
        return tuple(phase for phase in CHAIN_PHASES if self.phase(phase) is None)


@dataclass(frozen=True, slots=True)
class DecisionChain:
    """一条工单的决策链；同一 trace 内可能有多步。"""

    trace_id: str
    ticket_id: str | None
    agent: str | None
    steps: tuple[StepChain, ...]
    orphan_compensations: tuple[Span, ...] = ()

    @property
    def phases(self) -> tuple[str, ...]:
        covered = {
            phase
            for step in self.steps
            for phase in step.phases
        }
        if self.orphan_compensations:
            covered.add("compensation")
        return tuple(phase for phase in CHAIN_PHASES if phase in covered)

    @property
    def missing(self) -> tuple[str, ...]:
        return tuple(phase for phase in CHAIN_PHASES if phase not in self.phases)

    @property
    def complete(self) -> bool:
        return not self.missing and bool(self.steps)


def build_decision_chain(source: Trace | Sequence[Span]) -> DecisionChain:
    """按父子关系把 span 归到步骤，还原五段链路。"""

    spans = _spans_of(source)
    if not spans:
        return DecisionChain(trace_id="", ticket_id=None, agent=None, steps=())

    by_id = {span.span_id: span for span in spans}

    def nearest_step(span: Span) -> Span | None:
        current = by_id.get(span.parent_id or "")
        seen: set[str] = set()
        while current is not None and current.span_id not in seen:
            seen.add(current.span_id)
            if current.name == "ticket.step":
                return current
            current = by_id.get(current.parent_id or "")
        return None

    step_spans = sorted(
        (span for span in spans if span.name == "ticket.step"),
        key=lambda item: (int(item.attributes.get("step_no") or 0), item.sequence),
    )
    assigned: dict[str, list[Span]] = {span.span_id: [] for span in step_spans}
    orphans: list[Span] = []

    for span in spans:
        if span.name == "ticket.step":
            continue
        owner = nearest_step(span)
        if owner is None and span.name == "gateway.compensate":
            action_key = span.attributes.get("action_key")
            candidates = [
                step
                for step in step_spans
                if step.attributes.get("action_key") == action_key
            ]
            owner = candidates[-1] if len(candidates) == 1 else None
        if owner is not None:
            assigned[owner.span_id].append(span)
        elif span.name == "gateway.compensate":
            orphans.append(span)

    first = spans[0]
    steps = tuple(
        StepChain(
            step=step,
            spans=tuple(
                sorted(assigned[step.span_id], key=lambda item: (item.started_at, item.sequence))
            ),
        )
        for step in step_spans
    )
    return DecisionChain(
        trace_id=first.trace_id,
        ticket_id=first.attributes.get("ticket_id"),
        agent=first.attributes.get("agent"),
        steps=steps,
        orphan_compensations=tuple(orphans),
    )


def render_decision_chain(chain: DecisionChain) -> str:
    """输出「决策 → 预演 → 写入 → 校验 → 补偿」的文本链与完整性结论。"""

    lines = [
        f"决策链 trace={chain.trace_id} ticket={chain.ticket_id} agent={chain.agent}",
    ]

    for index, step in enumerate(chain.steps):
        last_step = index == len(chain.steps) - 1
        branch = "└─" if last_step else "├─"
        child_indent = "   " if last_step else "│  "
        detail = _span_summary(step.step)
        lines.append(f"{branch} 步骤 {detail}")

        present = [(phase, step.phase(phase)) for phase in CHAIN_PHASES]
        present = [(phase, span) for phase, span in present if span is not None]
        for pos, (phase, span) in enumerate(present):
            leaf = "└─" if pos == len(present) - 1 else "├─"
            lines.append(
                f"{child_indent}{leaf} {PHASE_LABELS[phase]} {_span_summary(span)} "
                f"({_status_text(span)})"
            )
        if step.missing:
            missing = "、".join(PHASE_LABELS[phase] for phase in step.missing)
            lines.append(f"{child_indent}   （缺失：{missing}）")

    for span in chain.orphan_compensations:
        lines.append(f"└─ 工单级补偿 {_span_summary(span)} ({_status_text(span)})")

    covered = " → ".join(PHASE_LABELS[phase] for phase in chain.phases) or "（无）"
    verdict = "完整" if chain.complete else "不完整"
    lines.append(
        f"链路覆盖: {covered}（{len(chain.phases)}/{len(CHAIN_PHASES)}，{verdict}）"
    )
    return "\n".join(lines)
