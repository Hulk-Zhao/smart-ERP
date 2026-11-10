"""告警规则文件校验：结构、指标存在性、P0 审计断链的可触发演示。

``promtool`` 未安装（不属于本项目依赖），因此这里用 PyYAML 做语法校验，
并对规则做结构断言；P0 那条用最小求值器直接在真实 ``/metrics`` 文本上跑一遍。
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
        "SmartErpCompensationBacklogHigh",
        "SmartErpReconBatchFailureRateHigh",
        "SmartErpAuditChainBroken",
    ):
        assert required in names


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
