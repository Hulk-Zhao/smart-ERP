"""Prometheus 指标映射：文本格式、span→指标折算、标签克制、/metrics 端点。

约束来源：T702 —— 观测要能被监控系统消费，但高基数绝不能进时序库。
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from observability import (
    Counter,
    Gauge,
    Histogram,
    MetricError,
    MetricLabelError,
    MetricsRegistry,
    Tracer,
    install_metrics,
    metrics_enabled,
    render_metrics,
)

# ---------------------------------------------------------------- 标签克制


@pytest.mark.parametrize(
    "label",
    ["ticket_id", "idem_key", "external_id", "trace_id", "span_id", "doc_no", "batch_id"],
)
def test_forbidden_high_cardinality_labels_are_rejected(label: str) -> None:
    with pytest.raises(MetricLabelError):
        Counter("smart_erp_probe_total", "探针", (label,))


def test_any_id_suffix_is_rejected_even_if_not_in_blacklist() -> None:
    for label in ("supplier_id", "org_id", "some_new_id"):
        with pytest.raises(MetricLabelError):
            Gauge("smart_erp_probe", "探针", (label,))


def test_undeclared_or_missing_labels_are_rejected_at_write_time() -> None:
    counter = Counter("smart_erp_probe_total", "探针", ("status",))
    with pytest.raises(MetricLabelError):
        counter.inc(status="ok", ticket_id="T-1")
    with pytest.raises(MetricLabelError):
        counter.inc()


# ---------------------------------------------------------------- 文本格式


def test_counter_gauge_histogram_render_prometheus_text_format() -> None:
    counter = Counter("smart_erp_probe_total", "探针计数器", ("status",))
    counter.inc(status="ok")
    counter.inc(2, status="ok")
    counter.inc(status="failed")

    gauge = Gauge("smart_erp_probe_current", "探针瞬时值")
    gauge.set(7)

    histogram = Histogram(
        "smart_erp_probe_seconds", "探针耗时", ("stage",), buckets=(0.1, 0.5, 1.0)
    )
    histogram.observe(0.05, stage="write")
    histogram.observe(0.75, stage="write")

    lines = counter.render() + gauge.render() + histogram.render()

    assert "# HELP smart_erp_probe_total 探针计数器" in lines
    assert "# TYPE smart_erp_probe_total counter" in lines
    assert 'smart_erp_probe_total{status="ok"} 3' in lines
    assert 'smart_erp_probe_total{status="failed"} 1' in lines
    assert "# TYPE smart_erp_probe_current gauge" in lines
    assert "smart_erp_probe_current 7" in lines

    assert "# TYPE smart_erp_probe_seconds histogram" in lines
    assert 'smart_erp_probe_seconds_bucket{stage="write",le="0.1"} 1' in lines
    assert 'smart_erp_probe_seconds_bucket{stage="write",le="0.5"} 1' in lines
    assert 'smart_erp_probe_seconds_bucket{stage="write",le="1"} 2' in lines
    assert 'smart_erp_probe_seconds_bucket{stage="write",le="+Inf"} 2' in lines
    assert 'smart_erp_probe_seconds_sum{stage="write"} 0.8' in lines
    assert 'smart_erp_probe_seconds_count{stage="write"} 2' in lines


def test_histogram_buckets_are_cumulative() -> None:
    histogram = Histogram("smart_erp_probe_seconds", "探针", buckets=(0.1, 1.0))
    histogram.observe(0.05)
    buckets, total, count = histogram.snapshot()
    assert buckets == {0.1: 1, 1.0: 1}
    assert count == 1
    assert total == pytest.approx(0.05)


def test_invalid_values_are_rejected() -> None:
    counter = Counter("smart_erp_probe_total", "探针")
    with pytest.raises(MetricError):
        counter.inc(-1)
    gauge = Gauge("smart_erp_probe", "探针")
    with pytest.raises(MetricError):
        gauge.set(-0.5)
    histogram = Histogram("smart_erp_probe_seconds", "探针", buckets=(0.1, 0.5))
    with pytest.raises(MetricError):
        histogram.observe(-1)
    with pytest.raises(MetricError):
        Histogram("smart_erp_probe_seconds", "探针", buckets=(0.5, 0.1))


# ---------------------------------------------------------------- span → 指标


def test_successful_trace_maps_to_ticket_counter_and_stage_metrics() -> None:
    registry = MetricsRegistry()
    tracer = Tracer()
    trace = tracer.start_trace(ticket_id="T-1", agent="AG-1")

    with tracer.step(trace, step_no=1, action_key="u8.po.draft", level=1, system="u8"):
        with tracer.span(trace, "gateway.preview"):
            pass
        with tracer.span(trace, "gateway.write"):
            pass
        with tracer.span(trace, "gateway.verify"):
            pass

    registry.observe_trace(trace)

    assert registry.tickets_total.value(status="succeeded", agent="AG-1") == 1
    for stage in ("preview", "write", "verify"):
        assert registry.write_stage_total.value(stage=stage) == 1
        assert registry.write_stage_failures_total.value(stage=stage) == 0
        _, _, count = registry.write_stage_duration_seconds.snapshot(stage=stage)
        assert count == 1


def test_failed_trace_is_classified_by_fail_code() -> None:
    registry = MetricsRegistry()
    tracer = Tracer()
    trace = tracer.start_trace(ticket_id="T-2", agent="AG-1")

    with tracer.step(trace, step_no=1, action_key="u8.po.draft", level=1, system="u8"):
        with tracer.span(trace, "gateway.write", fail_code="F6") as span:
            span.mark_error("verify mismatch")

    registry.observe_trace(trace)

    assert registry.tickets_total.value(status="failed", agent="AG-1") == 1
    assert registry.ticket_failures_total.value(failure_class="F6", agent="AG-1") == 1
    assert registry.ticket_failures_unclassified_total.value(agent="AG-1") == 0
    assert registry.write_stage_failures_total.value(stage="write") == 1


def test_failed_trace_without_class_goes_to_unclassified() -> None:
    registry = MetricsRegistry()
    tracer = Tracer()
    trace = tracer.start_trace(ticket_id="T-3", agent="AG-2")

    with tracer.step(trace, step_no=1, action_key="u8.po.draft", level=1, system="u8"):
        with tracer.span(trace, "gateway.write") as span:
            span.mark_error("boom")

    registry.observe_trace(trace)

    assert registry.tickets_total.value(status="failed", agent="AG-2") == 1
    assert registry.ticket_failures_unclassified_total.value(agent="AG-2") == 1
    assert registry.ticket_failures_total.value(failure_class="F1", agent="AG-2") == 0


def test_rejected_trace_is_counted_separately_from_failed() -> None:
    registry = MetricsRegistry()
    tracer = Tracer()
    trace = tracer.start_trace(ticket_id="T-4", agent="AG-3")

    with tracer.step(trace, step_no=1, action_key="u8.po.pay", level=3, system="u8"):
        with tracer.span(trace, "gateway.preview") as span:
            span.mark_rejected("E-POLICY-W3FORBIDDEN")

    registry.observe_trace(trace)

    assert registry.tickets_total.value(status="rejected", agent="AG-3") == 1
    assert registry.tickets_total.value(status="failed", agent="AG-3") == 0
    assert registry.write_stage_failures_total.value(stage="preview") == 1


def test_record_ticket_validates_inputs() -> None:
    registry = MetricsRegistry()
    with pytest.raises(MetricError):
        registry.record_ticket(status="unknown", agent="AG-1")
    with pytest.raises(MetricError):
        registry.record_ticket(status="failed", agent="AG-1", failure_class="F9")
    with pytest.raises(MetricError):
        registry.record_ticket(status="succeeded", agent="  ")


def test_record_stage_and_recon_validate_inputs() -> None:
    registry = MetricsRegistry()
    with pytest.raises(MetricError):
        registry.record_stage("rollback", 0.1)
    with pytest.raises(MetricError):
        registry.record_recon_batch("done", 1.0)
    with pytest.raises(MetricError):
        registry.set_unresolved(-1)
    with pytest.raises(MetricError):
        registry.set_compensation_backlog(-1)


# ---------------------------------------------------------------- 业务维度


def test_business_metrics_render_expected_series() -> None:
    registry = MetricsRegistry()
    registry.record_recon_batch("matched", 12.0)
    registry.record_recon_batch("pending_manual", 30.0)
    registry.record_recon_batch("unreconcilable", 45.0)
    registry.set_unresolved(7)
    registry.set_compensation_backlog(3)
    registry.set_audit_chain_broken(True)

    text = render_metrics(registry)

    assert 'smart_erp_recon_batches_total{result="matched"} 1' in text
    assert 'smart_erp_recon_batches_total{result="pending_manual"} 1' in text
    assert 'smart_erp_recon_batches_total{result="unreconcilable"} 1' in text
    assert 'smart_erp_recon_batch_duration_seconds_count 3' in text
    assert "smart_erp_unresolved_tickets 7" in text
    assert "smart_erp_compensation_backlog 3" in text
    assert "smart_erp_audit_chain_broken 1" in text


def test_audit_chain_gauge_flips_back_to_zero() -> None:
    registry = MetricsRegistry()
    registry.set_audit_chain_broken(True)
    assert "smart_erp_audit_chain_broken 1" in render_metrics(registry)
    registry.set_audit_chain_broken(False)
    assert "smart_erp_audit_chain_broken 0" in render_metrics(registry)


def test_metric_names_are_stable_and_prefixed() -> None:
    names = MetricsRegistry().metric_names
    assert all(name.startswith("smart_erp_") for name in names)
    for required in (
        "smart_erp_tickets_total",
        "smart_erp_ticket_failures_total",
        "smart_erp_write_stage_duration_seconds",
        "smart_erp_recon_batches_total",
        "smart_erp_unresolved_tickets",
        "smart_erp_compensation_backlog",
        "smart_erp_audit_chain_broken",
    ):
        assert required in names


# ---------------------------------------------------------------- /metrics


def test_metrics_endpoint_serves_text_and_stays_out_of_openapi() -> None:
    app = FastAPI()
    registry = MetricsRegistry()
    registry.set_unresolved(2)
    assert install_metrics(app, registry, enabled=True) is True

    client = TestClient(app)
    response = client.get("/metrics")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain; version=0.0.4")
    assert "smart_erp_unresolved_tickets 2" in response.text
    assert "# TYPE smart_erp_tickets_total counter" in response.text

    assert "/metrics" not in app.openapi()["paths"]


def test_metrics_endpoint_rejects_non_read_methods() -> None:
    app = FastAPI()
    install_metrics(app, MetricsRegistry(), enabled=True)
    client = TestClient(app)

    assert client.post("/metrics").status_code == 405
    head = client.head("/metrics")
    assert head.status_code == 200
    assert head.content == b""


# ---------------------------------------------------------------- 暴露面开关


def test_metrics_default_off_unless_explicitly_enabled() -> None:
    app = FastAPI()
    # 环境里没有 ERP_METRICS_ENABLED 时默认不挂载；生产不会被随手翻出内部指标。
    assert install_metrics(app, MetricsRegistry(), env={}) is False
    assert TestClient(app).get("/metrics").status_code == 404
    assert "/metrics" not in app.openapi()["paths"]


def test_metrics_env_switch_controls_mounting() -> None:
    disabled_app = FastAPI()
    assert install_metrics(disabled_app, MetricsRegistry(), env={"ERP_METRICS_ENABLED": "0"}) is False
    assert TestClient(disabled_app).get("/metrics").status_code == 404

    enabled_app = FastAPI()
    assert (
        install_metrics(
            enabled_app, MetricsRegistry(), env={"ERP_METRICS_ENABLED": "true"}
        )
        is True
    )
    assert TestClient(enabled_app).get("/metrics").status_code == 200


def test_metrics_path_can_be_overridden_by_env() -> None:
    app = FastAPI()
    install_metrics(
        app,
        MetricsRegistry(),
        env={"ERP_METRICS_ENABLED": "on", "ERP_METRICS_PATH": "/internal/metrics"},
    )
    client = TestClient(app)
    assert client.get("/internal/metrics").status_code == 200
    assert client.get("/metrics").status_code == 404


@pytest.mark.parametrize("raw", ["1", "true", "TRUE", "On", " yes ", "on"])
def test_metrics_enabled_accepts_truthy_spellings(raw: str) -> None:
    assert metrics_enabled({"ERP_METRICS_ENABLED": raw}) is True


@pytest.mark.parametrize("raw", ["", "0", "false", "FALSE", "no", "off", " OFF "])
def test_metrics_enabled_treats_blank_and_falsy_as_off(raw: str) -> None:
    assert metrics_enabled({"ERP_METRICS_ENABLED": raw}) is False
    assert metrics_enabled({}) is False


@pytest.mark.parametrize("raw", ["enabled", "ture", "2", "yes please"])
def test_metrics_enabled_rejects_invalid_values(raw: str) -> None:
    # 拼错/误用不得静默关闭：那会变成「以为开了、其实没开」
    with pytest.raises(MetricError) as excinfo:
        metrics_enabled({"ERP_METRICS_ENABLED": raw})
    assert "ERP_METRICS_ENABLED" in str(excinfo.value)


def test_install_metrics_fails_fast_on_invalid_switch() -> None:
    app = FastAPI()
    with pytest.raises(MetricError):
        install_metrics(app, MetricsRegistry(), env={"ERP_METRICS_ENABLED": "enabled"})
    # 抛错前不得已经挂上路由
    assert TestClient(app).get("/metrics").status_code == 404

