"""告警规则文件校验：结构、指标存在性、采集点同批合入、触发演示。

``promtool`` 未安装（不属于本项目依赖），因此这里用 PyYAML 做语法校验，
并对规则做结构断言；补偿/时钟/unresolved/审计链 8 条规则在 2026-11-11 接线后
进入生效集合，它们依赖的指标都有生产写入点，表达式在真实 ``/metrics`` 文本上各跑一遍。

口径（README §6.1.2）：写入点存在 ≠ 可观测；规则不评估 = 规则不存在。
因此本文件同时锁三件事：规则生效、指标有写入点、写指标的进程真的 serve /metrics。
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


def test_every_active_rule_has_wake_up_rationale(rules: list[dict]) -> None:
    # 11 条：2 条对账 + 1 条抓取掉线 + unresolved + overdue×3 + future_retry
    #        + clock×2 + 审计链 P0。新增规则必须先有采集点才能进生效集合。
    assert len(rules) == 11, [rule["alert"] for rule in rules]
    for rule in rules:
        assert rule["alert"]
        assert rule["expr"].strip()
        assert rule["labels"]["severity"] in {"P0", "P1", "P2"}
        annotations = rule["annotations"]
        assert annotations.get("summary")
        # 每条都必须回答「为什么值得半夜叫醒人」。
        assert annotations.get("why_paged", "").strip()
        assert annotations.get("runbook", "").strip()


def test_audit_chain_rule_is_active_p0_wired_to_scheduler() -> None:
    """审计链断链是最高优先级：gauge 由 scheduler 写入，由同一进程 serve。"""

    text = RULE_FILE.read_text(encoding="utf-8")
    assert "\n      - alert: SmartErpAuditChainBroken" in text
    assert "expr: max(smart_erp_audit_chain_broken) > 0" in text
    # P0 不允许拖长等待时间；1 分钟足够排除采集抖动。
    assert "for: 1m" in text
    assert "severity: P0" in text
    assert "compliance: audit-chain" in text
    # 写指标的进程必须自己暴露 /metrics，否则该序列永远 absent。
    scheduler_src = (
        Path(__file__).resolve().parents[2] / "core" / "runtime" / "scheduler.py"
    ).read_text(encoding="utf-8")
    assert "MetricsServer" in scheduler_src
    assert "ERP_METRICS_PORT" in scheduler_src


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
    assert names == {
        "SmartErpReconBatchFailureRateHigh",
        "SmartErpReconBatchP95Slow",
        "SmartErpScrapeTargetDown",
        "SmartErpUnresolvedTicketsGrowing",
        "SmartErpCompensationOverdueP2",
        "SmartErpCompensationOverdueP1Volume",
        "SmartErpCompensationOverdueP1Duration",
        "SmartErpCompensationFutureRetry",
        "SmartErpClockSkewP2",
        "SmartErpClockSkewP1",
        "SmartErpAuditChainBroken",
    }, names
    # 2026-11-11 实测后：写失败率与对账请求失败率都不具备可评估性，已从注释态改为显式删除。
    # 不允许再以「注释」形式留空壳（「恒不触发的规则也是不存在的规则」）。
    assert _commented_alert_names() == []


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


#: 已接线规则的语义锁定：(severity, for, 归一化后的 expr)。口径与 README §6.1.3 一致。
_ACTIVE_WIRED_RULES = {
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
    "SmartErpAuditChainBroken": ("P0", "1m", "max(smart_erp_audit_chain_broken) > 0"),
}

#: 指标名 → (生产写入点相对路径, 必须出现的调用片段)。
#: 这条锁住「规则引用的指标真的有进程在写」；写入点消失时规则会静默，必须红。
WRITER_LOCATIONS = {
    "smart_erp_recon_batches_total": ("apps/api/service.py", "observe_recon_result("),
    "smart_erp_recon_batch_duration_seconds": (
        "apps/api/service.py",
        "observe_recon_result(",
    ),
    "smart_erp_unresolved_tickets": (
        "apps/agents/compensation_worker.py",
        "set_unresolved(",
    ),
    "smart_erp_compensation_overdue": (
        "apps/agents/compensation_worker.py",
        "set_compensation_overdue(",
    ),
    "smart_erp_compensation_future_retry": (
        "apps/agents/compensation_worker.py",
        "set_compensation_future_retry(",
    ),
    "smart_erp_db_clock_skew_seconds": (
        "apps/agents/compensation_worker.py",
        "observe_clock_skew(",
    ),
    "smart_erp_audit_chain_broken": ("apps/audit/monitor.py", "set_audit_chain_broken("),
}

#: 有生产写入点的指标族。``up`` 是 Prometheus 内置序列，不在此表。
WIRED_METRICS = set(WRITER_LOCATIONS)

#: 注释态规则的锚点。2026-11-11 起注释集合必须为空：停用只有「删除 + tombstone」一种形态。
_COMMENTED_ALERT_RE = re.compile(r"^\s*#\s*-\s*alert:\s*(?P<name>[A-Za-z0-9_]+)\s*$", re.M)


def _commented_alert_names() -> list[str]:
    return [m.group("name") for m in _COMMENTED_ALERT_RE.finditer(RULE_FILE.read_text(encoding="utf-8"))]


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


def test_wired_rules_are_active_with_locked_semantics(rules: list[dict]) -> None:
    """接线后的规则必须真的在生效集合里，且 severity / for / 表达式没被悄悄改掉。"""

    by_name = {rule["alert"]: rule for rule in rules}
    for name, (severity, wait, expr) in _ACTIVE_WIRED_RULES.items():
        rule = by_name[name]
        assert rule["labels"]["severity"] == severity, name
        assert rule["for"] == wait, name
        assert " ".join(rule["expr"].split()) == expr, name


def test_every_wired_metric_has_a_production_writer() -> None:
    """每个被生效规则引用的指标，都必须能在生产代码里找到写入调用。

    写入点被删/被搬走时，规则会变成永远 inactive——这正是「规则不评估 = 规则不存在」。
    """

    repo = Path(__file__).resolve().parents[2]
    for metric, (relative, snippet) in WRITER_LOCATIONS.items():
        source = (repo / relative).read_text(encoding="utf-8")
        assert snippet in source, f"{metric} 的生产写入点 {relative}::{snippet} 已不存在"


def test_removed_rules_leave_a_reasoned_tombstone() -> None:
    """删除也是决策：每条被删的规则都必须留下实测数字与重新引入条件。"""

    text = RULE_FILE.read_text(encoding="utf-8")
    for name, numbers in (
        ("SmartErpWriteFailureRateHigh", ("pending_write", "absent")),
        ("SmartErpReconRunFailureRateHigh", ("E-CONN-UNAVAILABLE", "absent")),
    ):
        assert f"# - alert: {name}" not in text, f"{name} 不应再以注释形式存在"
        assert name in text, f"{name} 的删除理由必须留痕"
        for token in numbers:
            assert token in text, (name, token)
    assert text.count("重新引入条件") >= 2


def test_active_rules_only_reference_wired_metrics(rules: list[dict]) -> None:
    """生效规则引用的每个指标都必须有生产采集点；无采集点的只能注释。"""

    for rule in rules:
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
