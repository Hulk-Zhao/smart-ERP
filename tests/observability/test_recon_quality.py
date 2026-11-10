"""对账质量指标：行级口径、封闭词表、批次聚合与「不准率≠准确率」。

约束来源：T709 —— 对外可讲的质量指标只有自动匹配率 / 人工介入率 / 降级率。
仓库内没有人工标注 ground truth，所以任何 accuracy 都不可计算、不得出现在指标名里。

用本地枚举与数据类做替身，**不 import ``apps.recon``**：这既证明折算逻辑是鸭子类型，
也保证观测层不反向依赖业务流。
"""

from __future__ import annotations

import ast
import inspect
from dataclasses import dataclass
from enum import Enum

import pytest

from observability import metrics as metrics_module
from observability.metrics import (
    BATCH_STATUS_TO_RESULT,
    RECON_AUTO_OUTCOMES,
    RECON_LINE_OUTCOMES,
    RECON_RATIO_BUCKETS,
    RECON_RESULTS,
    Counter,
    MetricError,
    MetricLabelError,
    MetricsRegistry,
    classify_recon_line,
    render_metrics,
)


class LineStatus(str, Enum):
    """与 ``apps.recon.models.LineStatus`` 同名的替身；只依赖 ``.name``。"""

    MATCHED = "已匹配"
    NEEDS_REVIEW = "待人工"
    AMBIGUOUS = "匹配歧义"
    CANDIDATE_ONLY = "候选待人工"
    UNMATCHED_OURS = "我方未匹配"
    UNMATCHED_SUPPLIER = "供应商未匹配"
    DUPLICATE = "重复计费"
    UNPARSEABLE = "不可解析"


class MatchType(str, Enum):
    EXACT = "exact"
    FUZZY = "fuzzy"
    AGGREGATED = "aggregated"
    CANDIDATE = "candidate"
    UNMATCHED = "unmatched"


class BatchStatus(str, Enum):
    RECONCILED = "已对账"
    NEEDS_REVIEW = "待人工复核"
    NO_CONCLUSION = "无法对账"


class MissingKind(str, Enum):
    SCOPE = "范围缺失"
    LINE_DATA = "行数据缺失"


@dataclass
class Line:
    status: object
    match_type: object = MatchType.UNMATCHED
    supplier_ref: str | None = None


@dataclass
class Metrics:
    supplier_lines: int


@dataclass
class MissingItem:
    kind: object


@dataclass
class Result:
    batch_status: object
    lines: tuple[Line, ...] = ()
    metrics: Metrics | None = None
    missing_items: tuple[MissingItem, ...] = ()
    status_reason: str = ""
    conclusive: bool = True


def _matched(count: int, match_type: MatchType = MatchType.EXACT) -> list[Line]:
    return [
        Line(LineStatus.MATCHED, match_type, f"supplier:PO#{index}")
        for index in range(count)
    ]


def _unmatched(count: int) -> list[Line]:
    return [
        Line(LineStatus.UNMATCHED_SUPPLIER, MatchType.UNMATCHED, f"supplier:PO#{index}")
        for index in range(count)
    ]


# ---------------------------------------------------------------- 词表封闭


def test_vocabulary_is_closed_and_has_no_accuracy_metric() -> None:
    assert len(set(RECON_LINE_OUTCOMES)) == len(RECON_LINE_OUTCOMES)
    assert RECON_AUTO_OUTCOMES <= set(RECON_LINE_OUTCOMES)
    assert set(BATCH_STATUS_TO_RESULT.values()) == set(RECON_RESULTS)
    assert RECON_RATIO_BUCKETS == tuple(sorted(RECON_RATIO_BUCKETS))

    names = MetricsRegistry().metric_names
    assert not any("accuracy" in name for name in names)


def test_unknown_line_status_falls_back_to_one_declared_bucket() -> None:
    class Weird(str, Enum):
        SOMETHING_NEW = "新增状态"

    assert classify_recon_line(Line(Weird.SOMETHING_NEW, MatchType.EXACT, "s")) == "unknown"
    assert "unknown" in RECON_LINE_OUTCOMES


def test_observability_does_not_import_the_recon_app() -> None:
    tree = ast.parse(inspect.getsource(metrics_module))
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.append(node.module)
    assert [name for name in imported if name.startswith("apps")] == []


# ---------------------------------------------------------------- 行级折算


def test_supplier_lines_only_and_unaccounted_self_check() -> None:
    registry = MetricsRegistry()
    lines = [
        *_matched(2, MatchType.EXACT),
        *_matched(1, MatchType.FUZZY),
        *_matched(2, MatchType.AGGREGATED),
        Line(LineStatus.NEEDS_REVIEW, MatchType.AGGREGATED, "supplier:PO#5"),
        Line(LineStatus.AMBIGUOUS, MatchType.UNMATCHED, "supplier:PO#6"),
        Line(LineStatus.CANDIDATE_ONLY, MatchType.CANDIDATE, "supplier:PO#7"),
        Line(LineStatus.DUPLICATE, MatchType.EXACT, "supplier:PO#8"),
        Line(LineStatus.UNPARSEABLE, MatchType.UNMATCHED, "supplier:PO#9"),
        # 我方单边未匹配：supplier_ref 为空，不进分母
        Line(LineStatus.UNMATCHED_OURS, MatchType.UNMATCHED, None),
    ]
    # 引擎声明 11 行，实际只给了 10 行供应商侧数据 → 1 行 unaccounted
    registry.observe_recon_result(Result(BatchStatus.RECONCILED, tuple(lines), Metrics(11)))

    assert registry.recon_supplier_lines_total.value(outcome="matched_exact") == 2
    assert registry.recon_supplier_lines_total.value(outcome="matched_fuzzy") == 1
    assert registry.recon_supplier_lines_total.value(outcome="matched_aggregated") == 2
    assert registry.recon_supplier_lines_total.value(outcome="needs_review") == 1
    assert registry.recon_supplier_lines_total.value(outcome="ambiguous") == 1
    assert registry.recon_supplier_lines_total.value(outcome="candidate") == 1
    assert registry.recon_supplier_lines_total.value(outcome="duplicate") == 1
    assert registry.recon_supplier_lines_total.value(outcome="unparseable") == 1
    assert registry.recon_supplier_lines_total.value(outcome="unmatched") == 0
    assert registry.recon_supplier_lines_total.value(outcome="unaccounted") == 1
    assert registry.recon_batches_total.value(result="matched") == 1


def test_invariant_counted_plus_unaccounted_equals_supplier_lines() -> None:
    registry = MetricsRegistry()
    lines = [*_matched(7), *_unmatched(3)]

    registry.observe_recon_result(Result(BatchStatus.RECONCILED, tuple(lines), Metrics(10)))

    outcomes = {
        name: int(registry.recon_supplier_lines_total.value(outcome=name))
        for name in RECON_LINE_OUTCOMES
    }
    assert sum(outcomes.values()) == 10
    assert outcomes["unaccounted"] == 0
    assert outcomes["unmatched"] == 3


# ---------------------------------------------------------------- 批次聚合


def test_batch_status_maps_to_frozen_result_vocabulary() -> None:
    registry = MetricsRegistry()
    registry.observe_recon_result(Result(BatchStatus.RECONCILED, tuple(_matched(2)), Metrics(2)))
    registry.observe_recon_result(
        Result(BatchStatus.NEEDS_REVIEW, (Line(LineStatus.NEEDS_REVIEW, MatchType.AGGREGATED, "s"),), Metrics(1))
    )

    assert registry.recon_batches_total.value(result="matched") == 1
    assert registry.recon_batches_total.value(result="pending_manual") == 1
    assert registry.recon_batches_total.value(result="unreconcilable") == 0

    with pytest.raises(MetricError):
        registry.observe_recon_result(Result("已对账"))


def test_degraded_batch_is_attributed_by_missing_kind_not_free_text() -> None:
    registry = MetricsRegistry()
    result = Result(
        BatchStatus.NO_CONCLUSION,
        (),
        Metrics(0),
        missing_items=(MissingItem(MissingKind.SCOPE),),
        status_reason="供应商「华东某某」未绑定，无法确定范围",
        conclusive=False,
    )
    registry.observe_recon_result(result, duration_seconds=12.0)

    assert registry.recon_batches_total.value(result="unreconcilable") == 1
    assert registry.recon_degraded_batches_total.value(reason="scope") == 1

    text = render_metrics(registry)
    assert "华东某某" not in text
    assert "telemetry" not in text
    _, _, duration_count = registry.recon_batch_duration_seconds.snapshot()
    assert duration_count == 1  # 不会因 observe 再调 record 而翻倍

    registry.observe_recon_result(Result(BatchStatus.NO_CONCLUSION, (), Metrics(0)))
    assert registry.recon_degraded_batches_total.value(reason="other") == 1


def test_auto_match_ratio_bucket_boundaries() -> None:
    registry = MetricsRegistry()
    # 10 行里 9 行自动 → 0.9：Prometheus 桶是「小于等于」，所以只有 le>=0.9 的桶计入
    registry.observe_recon_result(
        Result(BatchStatus.RECONCILED, tuple(_matched(9) + _unmatched(1)), Metrics(10))
    )
    buckets, total, count = registry.recon_batch_auto_match_ratio.snapshot()
    assert count == 1
    assert total == pytest.approx(0.9)
    assert buckets == {0.5: 0, 0.7: 0, 0.8: 0, 0.9: 1, 0.95: 1, 0.99: 1, 1.0: 1}

    # 20 行里 19 行自动 → 0.95：le="0.9" 不再增加，le="0.95" 起计入
    registry.observe_recon_result(
        Result(BatchStatus.RECONCILED, tuple(_matched(19) + _unmatched(1)), Metrics(20))
    )
    buckets, _, count = registry.recon_batch_auto_match_ratio.snapshot()
    assert count == 2
    assert buckets[0.9] == 1
    assert buckets[0.95] == 2
    assert buckets[1.0] == 2

    text = render_metrics(registry)
    assert 'smart_erp_recon_batch_auto_match_ratio_bucket{le="0.9"} 1' in text


# ---------------------------------------------------------------- 标签红线


@pytest.mark.parametrize(
    "label", ["supplier_code", "org_id", "period", "status_reason", "batch_id"]
)
def test_quality_redlines_cannot_become_labels(label: str) -> None:
    with pytest.raises(MetricLabelError):
        Counter("smart_erp_probe_total", "探针", (label,))


# ---------------------------------------------------------------- 失败路径


def test_failed_runs_have_their_own_counter() -> None:
    registry = MetricsRegistry()

    registry.record_recon_failure(stage="input", code="E-DATA-MISSING")
    registry.record_recon_failure(stage="persist", code="E-IDEM-CONFLICT")

    assert (
        registry.recon_runs_failed_total.value(stage="input", code="E-DATA-MISSING") == 1
    )
    assert (
        registry.recon_runs_failed_total.value(stage="persist", code="E-IDEM-CONFLICT")
        == 1
    )
    # 失败与「批次结果」互不冒充：量变少和失败在告警上是两件事。
    assert sum(registry.recon_batches_total.value(result=item) for item in RECON_RESULTS) == 0

    text = render_metrics(registry)
    assert 'smart_erp_recon_runs_failed_total{stage="persist",code="E-IDEM-CONFLICT"} 1' in text


def test_failed_runs_reject_unknown_stage_and_empty_code() -> None:
    registry = MetricsRegistry()

    with pytest.raises(MetricError):
        registry.record_recon_failure(stage="database", code="E-DATA-MISSING")
    with pytest.raises(MetricLabelError):
        registry.record_recon_failure(stage="input", code="")

