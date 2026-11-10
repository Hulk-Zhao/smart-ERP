"""告警规则文件校验：结构、指标存在性、采集点同批合入、触发演示。

``promtool`` 未安装（不属于本项目依赖），因此这里用 PyYAML 做语法校验，
并对规则做结构断言；补偿/时钟 6 条规则只允许以注释形态存在（零采集点），
它们的表达式用最小求值器在真实 ``/metrics`` 文本上各跑一遍。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from observability import MetricsRegistry, render_metrics

RULE_FILE = Path(__file__).resolve().parents[2] / "observability" / "alerts.rules.yaml"

METRIC_REF_RE = re.compile(r"\b(smart_erp_[a-zA-Z0-9_]+)")
_SERIES_SUFFIXES = ("_bucket", "_sum", "_count")


@pytest.fixture(scope="module")
def rules_doc() -> dict:
    return yaml.safe_load(RULE_FILE.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def rules(rules_doc: dict) -> list[dict]:
    collected: list[dict] = []
    for group in rules_doc["groups"]:
        collected.extend(group["rules"])
    return collected


def test_rule_file_parses_and_has_required_groups(rules_doc: dict) -> None:
    assert isinstance(rules_doc, dict)
    assert rules_doc["groups"]
    for group in rules_doc["groups"]:
        assert group["name"]
        assert group["rules"]


def test_at_least_five_rules_with_wake_up_rationale(rules: list[dict]) -> None:
    assert len(rules) >= 5
    for rule in rules:
        assert rule["alert"]
        assert rule["expr"].strip()
        assert rule["labels"]["severity"] in {"P0", "P1", "P2"}
        annotations = rule["annotations"]
        assert annotations.get("summary")
        # 每条都必须回答「为什么值得半夜叫醒人」。
        assert annotations.get("why_paged", "").strip()
        assert annotations.get("runbook", "").strip()


def test_audit_chain_rule_is_p0_and_fast_firing(rules: list[dict]) -> None:
    audit = [rule for rule in rules if rule["alert"] == "SmartErpAuditChainBroken"]
    assert len(audit) == 1
    rule = audit[0]
    assert rule["labels"]["severity"] == "P0"
    assert rule["expr"].strip() == "max(smart_erp_audit_chain_broken) > 0"
    # P0 不允许拖长等待时间；1 分钟足够排除采集抖动。
    assert rule["for"] == "1m"
    assert rule["labels"]["compliance"] == "audit-chain"


def test_every_referenced_metric_exists_in_registry(rules: list[dict]) -> None:
    available = set(MetricsRegistry().metric_names)
    for rule in rules:
        for reference in METRIC_REF_RE.findall(rule["expr"]):
            base = reference
            for suffix in _SERIES_SUFFIXES:
                if base.endswith(suffix):
                    base = base[: -len(suffix)]
                    break
            assert base in available, f"{rule['alert']} 引用了未定义的指标 {reference}"


def test_required_alerts_are_covered(rules: list[dict]) -> None:
    names = {rule["alert"] for rule in rules}
    for required in (
        "SmartErpWriteFailureRateHigh",
        "SmartErpUnresolvedTicketsGrowing",
        "SmartErpReconBatchFailureRateHigh",
        "SmartErpAuditChainBroken",
    ):
        assert required in names
    # 补偿/时钟 6 条规则依赖零采集点指标，按 README §6.1.2 必须注释掉，不得出现在生效规则里。
    assert "SmartErpCompensationOverdueP2" not in names


# ---------------------------------------------------------------- P0 触发演示


def _gauge_value(text: str, metric: str) -> float:
    """从真实 ``/metrics`` 文本里取出无标签 gauge 的值；不存在的序列按 0 处理。"""

    total = 0.0
    for line in text.splitlines():
        if line.startswith("#") or not line.startswith(metric):
            continue
        name, _, value = line.rpartition(" ")
        if name.strip() == metric:
            total += float(value)
    return total


def _eval_max_greater_than(expr: str, text: str) -> tuple[bool, float]:
    """只求值形如 ``max(<metric>) > <threshold>`` 的最小求值器。"""

    match = re.fullmatch(
        r"max\((?P<metric>smart_erp_[a-zA-Z0-9_]+)\)\s*>\s*(?P<threshold>[0-9.]+)",
        expr.strip(),
    )
    assert match is not None, f"求值器不支持该表达式：{expr!r}"
    observed = _gauge_value(text, match.group("metric"))
    return observed > float(match.group("threshold")), observed


def test_audit_chain_p0_alert_does_not_fire_when_chain_is_healthy() -> None:
    registry = MetricsRegistry()
    registry.set_audit_chain_broken(False)
    firing, observed = _eval_max_greater_than(
        "max(smart_erp_audit_chain_broken) > 0", render_metrics(registry)
    )
    assert firing is False
    assert observed == 0.0


def test_audit_chain_p0_alert_fires_on_real_metrics_output(capsys: pytest.CaptureFixture[str]) -> None:
    registry = MetricsRegistry()
    registry.set_audit_chain_broken(True)
    text = render_metrics(registry)

    firing, observed = _eval_max_greater_than(
        "max(smart_erp_audit_chain_broken) > 0", text
    )

    with capsys.disabled():
        print("\n[P0 演示] /metrics 关键行：")
        for line in text.splitlines():
            if "audit_chain_broken" in line:
                print("  " + line)
        print(f"[P0 演示] max(...) = {observed:g} > 0 → firing={firing}")

    assert firing is True
    assert observed == 1.0


# ------------------------------------------------- 补偿 / 时钟规则（A、B、C）


#: 指标名 → (severity, for, 归一化后的 expr)。口径与 README §6.1.3 一一对应。
_COMPENSATION_RULES = {
    "SmartErpCompensationOverdueP2": ("P2", "5m", "max(smart_erp_compensation_overdue) > 0"),
    "SmartErpCompensationOverdueP1Volume": (
        "P1",
        "5m",
        "max(smart_erp_compensation_overdue) > 5",
    ),
    "SmartErpCompensationOverdueP1Duration": (
        "P1",
        "15m",
        "max(smart_erp_compensation_overdue) > 0",
    ),
    "SmartErpCompensationFutureRetry": (
        "P1",
        "1m",
        "max(smart_erp_compensation_future_retry) > 0",
    ),
    "SmartErpClockSkewP2": (
        "P2",
        "1m",
        "max(abs(smart_erp_db_clock_skew_seconds)) >= 5",
    ),
    "SmartErpClockSkewP1": (
        "P1",
        "1m",
        "max(abs(smart_erp_db_clock_skew_seconds)) >= 30",
    ),
}


#: 截至 2026-11-11 审计，唯一有生产采集点的指标族（见 README §6.1.3）。
WIRED_METRICS = {
    "smart_erp_recon_batches_total",
    "smart_erp_recon_batch_duration_seconds",
    "smart_erp_recon_supplier_lines_total",
    "smart_erp_recon_batch_auto_match_ratio",
    "smart_erp_recon_degraded_batches_total",
    "smart_erp_recon_runs_failed_total",
}

#: T702 遗留的「规则在、采集点无」三条。本 PR 不动其启停（取消 P0 需单独决策），
#: 但覆盖度按 0 记，且它们的采集点列在 README §6.1 里是「无」。
LEGACY_UNWIRED_ALERTS = {
    "SmartErpWriteFailureRateHigh",
    "SmartErpUnresolvedTicketsGrowing",
    "SmartErpAuditChainBroken",
}


def test_dead_backlog_alert_is_removed(rules: list[dict]) -> None:
    """零采集点的 P1 必须删掉；指标定义可以留着等接线，但不得再被规则引用。"""

    assert "SmartErpCompensationBacklogHigh" not in {rule["alert"] for rule in rules}
    text = RULE_FILE.read_text(encoding="utf-8")
    for line in text.splitlines():
        if (
            "smart_erp_compensation_backlog" in line
            or "SmartErpCompensationBacklogHigh" in line
        ):
            assert line.lstrip().startswith("#"), line


def test_pending_wiring_rules_are_commented_out() -> None:
    """同批合入口径：零采集点的规则只能以注释形态存在（README §6.1.2）。"""

    text = RULE_FILE.read_text(encoding="utf-8")
    for name, (severity, wait, expr) in _COMPENSATION_RULES.items():
        assert f"# - alert: {name}" in text, name
        assert f"#   expr: {expr}" in text, name
        assert f"#   for: {wait}" in text, name
        assert f"#     severity: {severity}" in text, name


def test_active_rules_beyond_legacy_only_reference_wired_metrics(rules: list[dict]) -> None:
    """新进规则引用的指标必须有生产采集点；遗留的 3 条显式豁免并单独记账。"""

    for rule in rules:
        if rule["alert"] in LEGACY_UNWIRED_ALERTS:
            continue
        for reference in METRIC_REF_RE.findall(rule["expr"]):
            base = reference
            for suffix in _SERIES_SUFFIXES:
                if base.endswith(suffix):
                    base = base[: -len(suffix)]
                    break
            assert base in WIRED_METRICS, f"{rule['alert']} 引用了无采集点指标 {base}"


def _eval_max_abs_at_least(expr: str, text: str) -> tuple[bool, float]:
    """求值形如 ``max(abs(<metric>)) >= <threshold>`` 的最小求值器。"""

    match = re.fullmatch(
        r"max\(abs\((?P<metric>smart_erp_[a-zA-Z0-9_]+)\)\)\s*>=\s*(?P<threshold>[0-9.]+)",
        expr.strip(),
    )
    assert match is not None, f"求值器不支持该表达式：{expr!r}"
    observed = abs(_gauge_value(text, match.group("metric")))
    return observed >= float(match.group("threshold")), observed


def test_compensation_overdue_alert_fires_on_real_metrics_output() -> None:
    registry = MetricsRegistry()
    registry.set_compensation_overdue(3)
    text = render_metrics(registry)

    # 3 条跨过「> 0」，5 分钟内该有 P2；但还不到批量阈值 5，不能升 P1。
    firing, observed = _eval_max_greater_than(
        "max(smart_erp_compensation_overdue) > 0", text
    )
    assert firing is True
    assert observed == 3.0
    firing, _ = _eval_max_greater_than(
        "max(smart_erp_compensation_overdue) > 5", text
    )
    assert firing is False

    registry.set_compensation_overdue(6)
    firing, observed = _eval_max_greater_than(
        "max(smart_erp_compensation_overdue) > 5", render_metrics(registry)
    )
    assert firing is True
    assert observed == 6.0


def test_future_retry_alert_fires_on_real_metrics_output() -> None:
    registry = MetricsRegistry()
    registry.set_compensation_future_retry(1)

    firing, observed = _eval_max_greater_than(
        "max(smart_erp_compensation_future_retry) > 0", render_metrics(registry)
    )

    assert firing is True
    assert observed == 1.0


def test_clock_skew_alerts_split_p2_and_p1_on_real_metrics_output() -> None:
    registry = MetricsRegistry()

    # 今晚实测回拨 0.84～1.43s：属于抖动，两条规则都不该响。
    registry.observe_clock_skew(monotonic_delta_seconds=10.0, wall_delta_seconds=8.57)
    text = render_metrics(registry)
    assert "smart_erp_db_clock_skew_seconds -1.43" in text
    firing, _ = _eval_max_abs_at_least(
        "max(abs(smart_erp_db_clock_skew_seconds)) >= 5", text
    )
    assert firing is False

    # 回拨 6s：跨过 P2（≥5），不到 P1（≥30）。
    registry.observe_clock_skew(monotonic_delta_seconds=10.0, wall_delta_seconds=4.0)
    text = render_metrics(registry)
    p2, observed = _eval_max_abs_at_least(
        "max(abs(smart_erp_db_clock_skew_seconds)) >= 5", text
    )
    p1, _ = _eval_max_abs_at_least(
        "max(abs(smart_erp_db_clock_skew_seconds)) >= 30", text
    )
    assert p2 is True
    assert observed == 6.0
    assert p1 is False

    # 回拨 31s：P1，并且进程内最大回拨被记录下来（只做排查，不单独告警）。
    registry.observe_clock_skew(monotonic_delta_seconds=10.0, wall_delta_seconds=-21.0)
    text = render_metrics(registry)
    p1, observed = _eval_max_abs_at_least(
        "max(abs(smart_erp_db_clock_skew_seconds)) >= 30", text
    )
    assert p1 is True
    assert observed == 31.0
    assert "smart_erp_db_clock_skew_backwards_seconds_max 31" in text
