"""埋点约定：W3C id、span 层级、必填属性、异常/拒绝标记、导出与脱敏。"""

from __future__ import annotations

import json

import pytest

from observability import (
    JsonlExporter,
    MemoryExporter,
    MetricError,
    TraceConventionError,
    Tracer,
    exporter_from_env,
    format_traceparent,
    new_span_id,
    new_trace_id,
    parse_traceparent,
)


def test_w3c_ids_and_traceparent_roundtrip() -> None:
    trace_id, span_id = new_trace_id(), new_span_id()
    assert len(trace_id) == 32 and len(span_id) == 16
    assert trace_id == trace_id.lower()

    header = format_traceparent(trace_id, span_id, sampled=True)
    assert header.startswith(f"00-{trace_id}-{span_id}-01")
    assert parse_traceparent(header) == (trace_id, span_id, True)
    assert parse_traceparent(format_traceparent(trace_id, span_id, sampled=False)) == (
        trace_id,
        span_id,
        False,
    )

    assert parse_traceparent("bogus") is None
    assert parse_traceparent(f"00-{'0' * 32}-{span_id}-01") is None
    assert parse_traceparent(f"00-{trace_id}-{'0' * 16}-01") is None
    assert parse_traceparent(f"00-{trace_id.upper()}-{span_id}-01") == (trace_id, span_id, True)


def test_start_trace_requires_ticket_and_agent() -> None:
    tracer = Tracer()
    with pytest.raises(TraceConventionError):
        tracer.start_trace(ticket_id="", agent="AG-1")
    with pytest.raises(TraceConventionError):
        tracer.start_trace(ticket_id="T-1", agent=" ")


def test_step_parents_children_and_carries_required_attributes() -> None:
    tracer = Tracer()
    trace = tracer.start_trace(ticket_id="T-1", agent="AG-1")

    with tracer.step(trace, step_no=1, action_key="u8.po.draft", level=1, system="u8") as step:
        with tracer.decision(trace, kind="match", rule_id="rule.supplier", confidence=0.91):
            pass
        with tracer.span(trace, "gateway.preview", connector="u8", field_count=2):
            pass
        with tracer.span(trace, "audit.write", image_id="IMG-1", hash_prev="abc"):
            pass

    names = [span.name for span in trace.spans]
    assert names == ["ticket.step", "agent.decision", "gateway.preview", "audit.write"]

    attributes = step.attributes
    assert attributes["ticket_id"] == "T-1"
    assert attributes["agent"] == "AG-1"
    assert attributes["step_no"] == 1
    assert attributes["level"] == "W1"
    assert attributes["system"] == "u8"

    children = [span for span in trace.spans if span.name != "ticket.step"]
    assert {child.parent_id for child in children} == {step.span_id}
    assert trace.status == "ok"
    assert all(span.duration_ms is not None for span in trace.spans)


def test_unknown_span_name_and_bad_decision_are_rejected() -> None:
    tracer = Tracer()
    trace = tracer.start_trace(ticket_id="T-1", agent="AG-1")
    with pytest.raises(TraceConventionError), tracer.span(trace, "custom.made.up"):
        pass
    with pytest.raises(TraceConventionError), tracer.decision(trace, kind="guess"):
        pass
    with pytest.raises(TraceConventionError), tracer.decision(trace, kind="match", confidence=1.2):
        pass


def test_exception_marks_error_and_propagates() -> None:
    tracer = Tracer()
    trace = tracer.start_trace(ticket_id="T-1", agent="AG-1")

    with pytest.raises(RuntimeError), tracer.span(trace, "gateway.write", level="W1", idem_key="idem-1"):
        raise RuntimeError("模拟传输失败")

    span = trace.spans[0]
    assert span.status == "error"
    assert span.attributes["error"] == "模拟传输失败"
    assert trace.status == "error"


def test_rejected_status_is_distinguished_from_error() -> None:
    tracer = Tracer()
    trace = tracer.start_trace(ticket_id="T-1", agent="AG-1")
    with tracer.span(trace, "gateway.write", level="W3") as span:
        span.mark_rejected("E-POLICY-W3FORBIDDEN")

    assert trace.spans[0].status == "rejected"
    assert trace.status == "rejected"
    assert "E-POLICY-W3FORBIDDEN" in trace.spans[0].to_dict()["attributes"]["reject_code"]


def test_exporter_receives_flat_fields_and_redacts_secrets(tmp_path) -> None:
    memory = MemoryExporter()
    tracer = Tracer(exporter=memory)
    trace = tracer.start_trace(ticket_id="T-1", agent="AG-1")
    with tracer.step(trace, step_no=1, action_key="u8.po.draft", level="W1", system="u8"), tracer.span(
        trace,
        "gateway.write",
        credential_ref="vault://lease/secret",
        idem_key="idem-1",
    ):
        pass

    assert len(memory.spans) == 2
    write_row = next(row for row in memory.spans if row["name"] == "gateway.write")
    assert write_row["ticket_id"] == "T-1"
    assert write_row["agent"] == "AG-1"
    assert write_row["step_no"] == 1
    assert write_row["level"] == "W1"
    assert write_row["system"] == "u8"
    assert write_row["attributes"]["credential_ref"] == "[REDACTED]"

    path = tmp_path / "traces" / "trace.jsonl"
    file_tracer = Tracer(exporter=JsonlExporter(path))
    file_trace = file_tracer.start_trace(ticket_id="T-2", agent="AG-2")
    with file_tracer.span(file_trace, "gateway.preview", connector="u8", field_count=1):
        pass

    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    row = json.loads(lines[0])
    assert row["name"] == "gateway.preview"
    assert row["ticket_id"] == "T-2"


def test_exporter_from_env_defaults_off() -> None:
    assert exporter_from_env({}) is None
    assert exporter_from_env({"ERP_TRACE_ENABLED": "0"}) is None

    exporter = exporter_from_env(
        {"ERP_TRACE_ENABLED": "true", "ERP_TRACE_FILE": "logs/x.jsonl"}
    )
    assert isinstance(exporter, JsonlExporter)
    assert str(exporter.path).replace("\\", "/") == "logs/x.jsonl"


def test_exporter_from_env_accepts_mixed_case_and_spaces() -> None:
    exporter = exporter_from_env(
        {"ERP_TRACE_ENABLED": " On ", "ERP_TRACE_FILE": "logs/y.jsonl"}
    )
    assert isinstance(exporter, JsonlExporter)


@pytest.mark.parametrize("raw", ["enabled", "ture", "2"])
def test_exporter_from_env_rejects_invalid_switch(raw: str) -> None:
    # 与 ERP_METRICS_ENABLED 同一实现，trace 也不允许拼错静默关闭
    with pytest.raises(MetricError) as excinfo:
        exporter_from_env({"ERP_TRACE_ENABLED": raw})
    assert "ERP_TRACE_ENABLED" in str(excinfo.value)
