"""Prometheus 指标映射（标准库实现，不引入 prometheus_client）。

输出格式遵循 Prometheus text exposition format 0.0.4：

- ``# HELP`` / ``# TYPE`` + 样本行；
- 直方图输出 ``_bucket{le=...}``（累计）、``_sum``、``_count``；
- ``/metrics`` 端点为纯 ASGI 应用，由接入方 ``install_metrics(app, registry)`` 挂载；
  默认读 ``ERP_METRICS_ENABLED``（缺省关闭），且只允许内网抓取。

标签克制是本模块的硬约束：**工单号 / 幂等键 / 外部单号 / trace_id 一律禁止做标签**，
遇到即抛 :class:`MetricLabelError`。高基数只进 trace，不进时序库。

失败分类 F1–F8 取自 PRD §5.0 的冻结口径：
F1 数据缺失 / F2 解析失败 / F3 匹配歧义 / F4 主数据未绑定 /
F5 接口失败 / F6 校验不一致 / F7 并发冲突 / F8 外部已生效。
"""

from __future__ import annotations

import math
import os
import re
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

# ---------------------------------------------------------------- 冻结口径

FAILURE_CLASSES: tuple[str, ...] = tuple(f"F{index}" for index in range(1, 9))
TICKET_STATUSES: tuple[str, ...] = ("succeeded", "failed", "rejected")
WRITE_STAGES: tuple[str, ...] = ("preview", "write", "verify", "compensate")
RECON_RESULTS: tuple[str, ...] = ("matched", "pending_manual", "unreconcilable")

#: ``BatchStatus.name`` → 既有 ``result`` 词表。仅此三种，未知即报错，避免悄悄多出新序列。
BATCH_STATUS_TO_RESULT: dict[str, str] = {
    "RECONCILED": "matched",
    "NEEDS_REVIEW": "pending_manual",
    "NO_CONCLUSION": "unreconcilable",
}

#: 对账行级结果（封闭词表）。
#: ``matched_*`` 才算自动匹配；``unaccounted`` 是合成值（分母 − 已归类行数），
#: ``unknown`` 兜住引擎新增状态，二者长期非 0 都说明口径有缺口。
RECON_LINE_OUTCOMES: tuple[str, ...] = (
    "matched_exact",
    "matched_fuzzy",
    "matched_aggregated",
    "needs_review",
    "ambiguous",
    "candidate",
    "unmatched",
    "duplicate",
    "unparseable",
    "unaccounted",
    "unknown",
)

#: 计入「自动匹配」的行级结果；其余一律计入人工介入。
RECON_AUTO_OUTCOMES: frozenset[str] = frozenset(
    {"matched_exact", "matched_fuzzy", "matched_aggregated"}
)

#: 降级（无法对账）原因，取 ``MissingKind.name`` 的小写形式；自由文本永不进标签。
RECON_DEGRADATION_REASONS: tuple[str, ...] = (
    "scope",
    "supplier",
    "item",
    "base_unit",
    "unit_conversion",
    "line_data",
    "other",
)

#: 对账失败所在阶段（封闭词表）。失败与「批次结果」分开计数：量变少不等于失败。
RECON_FAILURE_STAGES: tuple[str, ...] = ("input", "engine", "persist")

#: 无法归类的异常用这个 code；取 ``ErrorCode.value``，自由文本永不进标签。
RECON_FAILURE_UNCLASSIFIED = "unclassified"

#: 批次自动匹配率桶（0–1）。2 位小数的分母限制下，桶只分辨「差 / 可用 / 接近全自动」。
RECON_RATIO_BUCKETS: tuple[float, ...] = (0.5, 0.7, 0.8, 0.9, 0.95, 0.99, 1.0)

_LINE_STATUS_TO_OUTCOME: dict[str, str] = {
    "NEEDS_REVIEW": "needs_review",
    "AMBIGUOUS": "ambiguous",
    "CANDIDATE_ONLY": "candidate",
    "UNMATCHED_OURS": "unmatched",
    "UNMATCHED_SUPPLIER": "unmatched",
    "DUPLICATE": "duplicate",
    "UNPARSEABLE": "unparseable",
}

_MATCH_TYPE_TO_OUTCOME: dict[str, str] = {
    "EXACT": "matched_exact",
    "FUZZY": "matched_fuzzy",
    "AGGREGATED": "matched_aggregated",
}

#: span 名 → 指标 stage 标签。
SPAN_TO_STAGE: dict[str, str] = {
    "gateway.preview": "preview",
    "gateway.write": "write",
    "gateway.verify": "verify",
    "gateway.compensate": "compensate",
}

#: 阶段耗时桶（秒）；工单级写操作通常在毫秒到秒级。
STAGE_BUCKETS: tuple[float, ...] = (
    0.005,
    0.01,
    0.025,
    0.05,
    0.1,
    0.25,
    0.5,
    1.0,
    2.5,
    5.0,
    10.0,
)

#: 对账批次耗时桶（秒）；月末批次可能到分钟级。
RECON_BUCKETS: tuple[float, ...] = (1.0, 5.0, 10.0, 30.0, 60.0, 120.0, 300.0, 600.0)

#: 禁止作为标签的键：高基数、自由文本或敏感。
FORBIDDEN_LABELS: frozenset[str] = frozenset(
    {
        "ticket_id",
        "idem_key",
        "external_id",
        "trace_id",
        "span_id",
        "doc_no",
        "batch_id",
        "supplier_code",
        "org_id",
        "period",
        "status_reason",
    }
)

#: ``/metrics`` 默认关闭（与 trace 默认关闭同一原则）；只有显式打开才挂载。
METRICS_ENABLED_VAR = "ERP_METRICS_ENABLED"
METRICS_PATH_VAR = "ERP_METRICS_PATH"
DEFAULT_METRICS_PATH = "/metrics"
_TRUTHY = frozenset({"1", "true", "yes", "on"})

_METRIC_NAME_RE = re.compile(r"^[a-zA-Z_:][a-zA-Z0-9_:]*$")
_LABEL_NAME_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")
_HELP_ESCAPE = {"\\": r"\\", "\n": r"\n"}
_LABEL_ESCAPE = {"\\": r"\\", "\n": r"\n", '"': r"\""}


class MetricError(ValueError):
    """指标名/标签/取值不合法。"""


class MetricLabelError(MetricError):
    """试图使用高基数或未声明的标签。"""


def _escape_help(value: str) -> str:
    return "".join(_HELP_ESCAPE.get(char, char) for char in value)


def _escape_label(value: str) -> str:
    return "".join(_LABEL_ESCAPE.get(char, char) for char in value)


def _format_value(value: float) -> str:
    number = float(value)
    if not math.isfinite(number):
        raise MetricError(f"指标值必须是有限数，实际 {value!r}")
    if number.is_integer():
        return str(int(number))
    return f"{number:.12g}"


def _finite(value: Any, what: str) -> float:
    """转成有限浮点；允许负值（时钟偏移这类双向信号用）。"""

    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise MetricError(f"{what} 必须是数字，实际 {value!r}") from exc
    if not math.isfinite(number):
        raise MetricError(f"{what} 必须是有限数，实际 {value!r}")
    return number


def _validate_metric_name(name: str) -> None:
    if not _METRIC_NAME_RE.match(name):
        raise MetricError(f"非法指标名 {name!r}")


def _validate_label_names(names: Sequence[str]) -> None:
    for name in names:
        if not _LABEL_NAME_RE.match(name):
            raise MetricError(f"非法标签名 {name!r}")
        if name == "le":
            raise MetricLabelError("le 为直方图保留标签，不得声明")
        if name in FORBIDDEN_LABELS or name.endswith("_id"):
            raise MetricLabelError(f"禁止把高基数字段 {name!r} 作为指标标签")


def _non_negative(value: float, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise MetricError(f"{name} 必须是数值，实际 {value!r}")
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise MetricError(f"{name} 必须是非负有限数，实际 {value!r}")
    return number


@dataclass
class _Family:
    name: str
    help: str
    label_names: tuple[str, ...]
    kind: str
    values: dict[tuple[str, ...], float] = field(default_factory=dict)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def _key(self, labels: Mapping[str, Any]) -> tuple[str, ...]:
        unknown = set(labels) - set(self.label_names)
        missing = set(self.label_names) - set(labels)
        if unknown:
            raise MetricLabelError(
                f"{self.name} 不允许标签 {sorted(unknown)}；允许 {list(self.label_names)}"
            )
        if missing:
            raise MetricLabelError(f"{self.name} 缺少标签 {sorted(missing)}")
        key: list[str] = []
        for name in self.label_names:
            value = labels[name]
            if not isinstance(value, (str, int, float, bool)):
                raise MetricError(f"标签 {name} 必须是标量，实际 {type(value).__name__}")
            key.append(str(value))
        return tuple(key)

    def _render_labels(self, key: tuple[str, ...], *, extra: str | None = None) -> str:
        pairs = [
            f'{name}="{_escape_label(value)}"'
            for name, value in zip(self.label_names, key)
        ]
        if extra is not None:
            pairs.append(extra)
        return "{" + ",".join(pairs) + "}" if pairs else ""

    def render(self) -> list[str]:
        lines = [f"# HELP {self.name} {_escape_help(self.help)}", f"# TYPE {self.name} {self.kind}"]
        with self.lock:
            items = sorted(self.values.items())
        for key, value in items:
            lines.append(f"{self.name}{self._render_labels(key)} {_format_value(value)}")
        return lines


class Counter:
    """单调递增计数器。"""

    def __init__(self, name: str, help_text: str, label_names: Sequence[str] = ()) -> None:
        _validate_metric_name(name)
        _validate_label_names(label_names)
        self._family = _Family(name, help_text, tuple(label_names), "counter")

    @property
    def name(self) -> str:
        return self._family.name

    def inc(self, amount: float = 1.0, **labels: Any) -> None:
        delta = _non_negative(amount, "amount")
        if delta == 0:
            return
        key = self._family._key(labels)
        with self._family.lock:
            self._family.values[key] = self._family.values.get(key, 0.0) + delta

    def value(self, **labels: Any) -> float:
        key = self._family._key(labels)
        with self._family.lock:
            return self._family.values.get(key, 0.0)

    def render(self) -> list[str]:
        return self._family.render()


class Gauge:
    """可增可减的瞬时值。

    ``allow_negative=True`` 用于时钟偏移这类双向信号；其余 gauge 仍拒绝负值，
    因为「工单数 −1」几乎总是计数逻辑写错，而不是合法状态。
    """

    def __init__(
        self,
        name: str,
        help_text: str,
        label_names: Sequence[str] = (),
        *,
        allow_negative: bool = False,
    ) -> None:
        _validate_metric_name(name)
        _validate_label_names(label_names)
        self._family = _Family(name, help_text, tuple(label_names), "gauge")
        self.allow_negative = allow_negative

    @property
    def name(self) -> str:
        return self._family.name

    def set(self, value: float, **labels: Any) -> None:
        number = (
            _finite(value, "value")
            if self.allow_negative
            else _non_negative(value, "value")
        )
        key = self._family._key(labels)
        with self._family.lock:
            self._family.values[key] = number

    def value(self, **labels: Any) -> float | None:
        key = self._family._key(labels)
        with self._family.lock:
            return self._family.values.get(key)

    def render(self) -> list[str]:
        return self._family.render()


@dataclass
class _HistogramState:
    bucket_counts: list[int]
    total: float = 0.0
    count: int = 0


class Histogram:
    """固定桶直方图；``observe`` 入参单位为秒。"""

    def __init__(
        self,
        name: str,
        help_text: str,
        label_names: Sequence[str] = (),
        *,
        buckets: Sequence[float] = STAGE_BUCKETS,
    ) -> None:
        _validate_metric_name(name)
        _validate_label_names(label_names)
        values = tuple(float(bucket) for bucket in buckets)
        if not values or any(bucket <= 0 for bucket in values):
            raise MetricError("直方图桶必须是正数")
        if list(values) != sorted(values):
            raise MetricError("直方图桶必须递增")
        self.name = name
        self.help = help_text
        self.label_names = tuple(label_names)
        self.buckets = values
        self._values: dict[tuple[str, ...], _HistogramState] = {}
        self._lock = threading.Lock()
        self._labels = _Family(name, help_text, self.label_names, "histogram")

    def observe(self, seconds: float, **labels: Any) -> None:
        value = _non_negative(seconds, "seconds")
        key = self._labels._key(labels)
        with self._lock:
            state = self._values.setdefault(
                key, _HistogramState([0] * len(self.buckets))
            )
            for index, bucket in enumerate(self.buckets):
                if value <= bucket:
                    state.bucket_counts[index] += 1
            state.total += value
            state.count += 1

    def snapshot(self, **labels: Any) -> tuple[dict[float, int], float, int]:
        """读取某组标签的累计桶、总和、次数；测试与排障用。"""

        key = self._labels._key(labels)
        with self._lock:
            state = self._values.get(key)
            if state is None:
                return {bucket: 0 for bucket in self.buckets}, 0.0, 0
            return dict(zip(self.buckets, state.bucket_counts)), state.total, state.count

    def render(self) -> list[str]:
        lines = [
            f"# HELP {self.name} {_escape_help(self.help)}",
            f"# TYPE {self.name} histogram",
        ]
        with self._lock:
            items = sorted(self._values.items())
            snapshot = [
                (key, list(state.bucket_counts), state.total, state.count)
                for key, state in items
            ]
        infinity_label = 'le="+Inf"'
        for key, counts, total, count in snapshot:
            for bucket, bucket_count in zip(self.buckets, counts):
                le = _format_value(bucket)
                bucket_label = 'le="' + le + '"'
                lines.append(
                    f"{self.name}_bucket{self._labels._render_labels(key, extra=bucket_label)} "
                    f"{bucket_count}"
                )
            lines.append(
                f"{self.name}_bucket{self._labels._render_labels(key, extra=infinity_label)} "
                f"{count}"
            )
            lines.append(
                f"{self.name}_sum{self._labels._render_labels(key)} {_format_value(total)}"
            )
            lines.append(
                f"{self.name}_count{self._labels._render_labels(key)} {_format_value(count)}"
            )
        return lines


class MetricsRegistry:
    """业务指标集合；所有写入方法都做取值校验，标签集固定。"""

    def __init__(self) -> None:
        self.tickets_total = Counter(
            "smart_erp_tickets_total",
            "工单执行次数，按终态与 Agent 计数（成功/失败/拒绝）",
            ("status", "agent"),
        )
        self.ticket_failures_total = Counter(
            "smart_erp_ticket_failures_total",
            "失败工单次数，按 F1-F8 失败分类与 Agent 计数",
            ("failure_class", "agent"),
        )
        self.ticket_failures_unclassified_total = Counter(
            "smart_erp_ticket_failures_unclassified_total",
            "失败但未给出 F1-F8 分类的工单次数（口径缺口，需清零）",
            ("agent",),
        )
        self.write_stage_total = Counter(
            "smart_erp_write_stage_total",
            "WriteGateway 各阶段执行次数",
            ("stage",),
        )
        self.write_stage_failures_total = Counter(
            "smart_erp_write_stage_failures_total",
            "WriteGateway 各阶段失败次数（error 或 rejected）",
            ("stage",),
        )
        self.write_stage_duration_seconds = Histogram(
            "smart_erp_write_stage_duration_seconds",
            "WriteGateway 各阶段耗时（秒）",
            ("stage",),
            buckets=STAGE_BUCKETS,
        )
        self.recon_batches_total = Counter(
            "smart_erp_recon_batches_total",
            "对账批次结果计数：已对账/待人工/无法对账",
            ("result",),
        )
        self.recon_batch_duration_seconds = Histogram(
            "smart_erp_recon_batch_duration_seconds",
            "对账批次耗时（秒）",
            buckets=RECON_BUCKETS,
        )
        self.recon_supplier_lines_total = Counter(
            "smart_erp_recon_supplier_lines_total",
            "供应商对账行数，按行级结果计数（自动匹配率与人工介入率的分子分母）",
            ("outcome",),
        )
        self.recon_batch_auto_match_ratio = Histogram(
            "smart_erp_recon_batch_auto_match_ratio",
            "单批次自动匹配率（0-1，按行级状态重算；不是人工标注准确率）",
            buckets=RECON_RATIO_BUCKETS,
        )
        self.recon_degraded_batches_total = Counter(
            "smart_erp_recon_degraded_batches_total",
            "降级（无法对账）批次计数，按缺失项种类归因",
            ("reason",),
        )
        self.recon_runs_failed_total = Counter(
            "smart_erp_recon_runs_failed_total",
            "对账请求失败次数，按失败阶段与问题码计数（含未归类异常）",
            ("stage", "code"),
        )
        self.unresolved_tickets = Gauge(
            "smart_erp_unresolved_tickets",
            "当前未解决工单数（unresolved）",
        )
        self.compensation_backlog = Gauge(
            "smart_erp_compensation_backlog",
            "当前待处理补偿任务积压数",
        )
        self.compensation_overdue = Gauge(
            "smart_erp_compensation_overdue",
            "补偿任务已到期却未被领取的条数（抓 claim 停摆，含 next_retry_at 为 NULL 的无闸门行）",
        )
        self.compensation_future_retry = Gauge(
            "smart_erp_compensation_future_retry",
            "next_retry_at 落在未来 18s 之外的 queued 补偿任务数（只可能由墙钟回拨或错算造成）",
        )
        self.db_clock_skew_seconds = Gauge(
            "smart_erp_db_clock_skew_seconds",
            "应用单调时钟与墙钟的差值（秒）：Δwall − Δtick，负值表示墙钟回拨",
            allow_negative=True,
        )
        self.db_clock_skew_backwards_seconds_max = Gauge(
            "smart_erp_db_clock_skew_backwards_seconds_max",
            "进程启动以来观测到的最大墙钟回拨幅度（秒）",
        )
        self.audit_chain_broken = Gauge(
            "smart_erp_audit_chain_broken",
            "审计链断链标志：0 正常，1 检测到断链（P0，不计入业务成功率）",
        )
        self._metrics: tuple[Any, ...] = (
            self.tickets_total,
            self.ticket_failures_total,
            self.ticket_failures_unclassified_total,
            self.write_stage_total,
            self.write_stage_failures_total,
            self.write_stage_duration_seconds,
            self.recon_batches_total,
            self.recon_batch_duration_seconds,
            self.recon_supplier_lines_total,
            self.recon_batch_auto_match_ratio,
            self.recon_degraded_batches_total,
            self.recon_runs_failed_total,
            self.unresolved_tickets,
            self.compensation_backlog,
            self.compensation_overdue,
            self.compensation_future_retry,
            self.db_clock_skew_seconds,
            self.db_clock_skew_backwards_seconds_max,
            self.audit_chain_broken,
        )

    @property
    def metric_names(self) -> tuple[str, ...]:
        return tuple(metric.name for metric in self._metrics)

    # -- 写入 ---------------------------------------------------------
    def record_ticket(
        self,
        *,
        status: str,
        agent: str,
        failure_class: str | None = None,
    ) -> None:
        if status not in TICKET_STATUSES:
            raise MetricError(f"status 必须是 {TICKET_STATUSES}，实际 {status!r}")
        if not str(agent).strip():
            raise MetricError("agent 不能为空")
        self.tickets_total.inc(status=status, agent=agent)
        if status != "failed":
            return
        if failure_class is None:
            self.ticket_failures_unclassified_total.inc(agent=agent)
            return
        if failure_class not in FAILURE_CLASSES:
            raise MetricError(
                f"failure_class 必须是 F1-F8，实际 {failure_class!r}"
            )
        self.ticket_failures_total.inc(failure_class=failure_class, agent=agent)

    def record_stage(self, stage: str, duration_seconds: float, *, ok: bool = True) -> None:
        if stage not in WRITE_STAGES:
            raise MetricError(f"stage 必须是 {WRITE_STAGES}，实际 {stage!r}")
        seconds = _non_negative(duration_seconds, "duration_seconds")
        self.write_stage_total.inc(stage=stage)
        self.write_stage_duration_seconds.observe(seconds, stage=stage)
        if not ok:
            self.write_stage_failures_total.inc(stage=stage)

    def record_recon_batch(self, result: str, duration_seconds: float) -> None:
        """只记批次结果与耗时；有完整 ``ReconResult`` 时应改用 ``observe_recon_result``。"""

        if result not in RECON_RESULTS:
            raise MetricError(f"result 必须是 {RECON_RESULTS}，实际 {result!r}")
        self.recon_batches_total.inc(result=result)
        self.recon_batch_duration_seconds.observe(
            _non_negative(duration_seconds, "duration_seconds")
        )

    def observe_recon_result(
        self, result: Any, *, duration_seconds: float | None = None
    ) -> None:
        """把一个批次结果折算成对账质量指标（鸭子类型，不 import ``apps.recon``）。

        口径（README §6.1.1 为唯一权威）：

        - 只统计**供应商侧**行（``supplier_ref`` 非空）；我方单边未匹配不进分母，
          否则「我方有单、供应商没来」会把匹配率压成假低；
        - 分母以 ``result.metrics.supplier_lines`` 为准；缺失时退回已归类行数；
        - ``unaccounted`` = 分母 − 已归类行数（≥0）。长期非 0 说明引擎少给了行；
        - 自动匹配率按**行级状态**重算，不用 ``metrics.matched_rate``：后者把 merged
          （状态为「待人工」）也算进 matched，与本层口径不一致；
        - 每批次只调用一次。不要再叠加 ``record_recon_batch``，否则批次计数翻倍。
        """

        batch_status = _enum_name(getattr(result, "batch_status", None))
        label = BATCH_STATUS_TO_RESULT.get(batch_status)
        if label is None:
            raise MetricError(
                f"batch_status 必须是 {sorted(BATCH_STATUS_TO_RESULT)}，"
                f"实际 {batch_status!r}"
            )
        if duration_seconds is not None:
            self.recon_batch_duration_seconds.observe(
                _non_negative(duration_seconds, "duration_seconds")
            )

        counts = {name: 0 for name in RECON_LINE_OUTCOMES}
        for line in getattr(result, "lines", None) or ():
            if getattr(line, "supplier_ref", None) is None:
                continue
            counts[classify_recon_line(line)] += 1

        declared = getattr(getattr(result, "metrics", None), "supplier_lines", None)
        supplier_lines = (
            int(declared)
            if isinstance(declared, int) and not isinstance(declared, bool)
            else sum(counts.values())
        )
        counted = sum(counts.values())
        if supplier_lines > counted:
            counts["unaccounted"] += supplier_lines - counted

        for outcome, amount in counts.items():
            if amount:
                self.recon_supplier_lines_total.inc(amount, outcome=outcome)

        self.recon_batches_total.inc(result=label)

        total = sum(counts.values())
        if total:
            auto = sum(counts[outcome] for outcome in RECON_AUTO_OUTCOMES)
            self.recon_batch_auto_match_ratio.observe(auto / total)

        if label == "unreconcilable":
            self.recon_degraded_batches_total.inc(reason=_degradation_reason(result))

    def record_recon_failure(self, *, stage: str, code: str) -> None:
        """记一次**失败**的对账请求（异常/中断），与批次结果计数分开。

        ``stage`` 取 ``RECON_FAILURE_STAGES``；``code`` 取 ``ErrorCode.value``，
        无法归类时用 ``RECON_FAILURE_UNCLASSIFIED``。二者都是封闭/受控词表，
        不接受自由文本（problem+json 的 detail 永远不进标签）。
        """

        if stage not in RECON_FAILURE_STAGES:
            raise MetricError(f"stage 必须是 {RECON_FAILURE_STAGES}，实际 {stage!r}")
        if not code or not isinstance(code, str) or len(code) > 64:
            raise MetricLabelError(f"code 必须是 ≤64 字符的非空字符串，实际 {code!r}")
        self.recon_runs_failed_total.inc(stage=stage, code=code)

    def set_unresolved(self, count: int) -> None:
        self.unresolved_tickets.set(_count_value(count, "unresolved"))

    def set_compensation_backlog(self, count: int) -> None:
        self.compensation_backlog.set(_count_value(count, "compensation_backlog"))

    def set_compensation_overdue(self, count: int) -> None:
        """到期未领取条数；SQL 口径见 README §6.1.3，NULL 分支必须计入。"""

        self.compensation_overdue.set(_count_value(count, "compensation_overdue"))

    def set_compensation_future_retry(self, count: int) -> None:
        self.compensation_future_retry.set(
            _count_value(count, "compensation_future_retry")
        )

    def observe_clock_skew(
        self, *, monotonic_delta_seconds: float, wall_delta_seconds: float
    ) -> float:
        """一次成对采样：单调时钟前进 ``tick``、墙钟前进 ``wall``，返回本次 skew。

        单调时钟不会倒退，所以 ``tick > wall`` 就是墙钟回拨——这是**根因信号**，
        在任务还没卡住之前就能报。``skew = wall − tick``，负值表示回拨；回拨幅度
        同时并入进程内最大值。调用方只需在采样点把两个增量传进来。
        """

        tick = _non_negative(monotonic_delta_seconds, "monotonic_delta_seconds")
        wall = _finite(wall_delta_seconds, "wall_delta_seconds")
        skew = wall - tick
        self.db_clock_skew_seconds.set(skew)
        backwards = -skew if skew < 0 else 0.0
        current = self.db_clock_skew_backwards_seconds_max.value() or 0.0
        if backwards > current:
            self.db_clock_skew_backwards_seconds_max.set(backwards)
        return skew

    def set_audit_chain_broken(self, broken: bool) -> None:
        self.audit_chain_broken.set(1 if broken else 0)

    # -- span 映射 -----------------------------------------------------
    def observe_trace(self, trace: Any) -> None:
        """把一条已结束的 trace 折算成指标；每 trace 只应调用一次。"""

        statuses = {span.status for span in trace.spans}
        if "rejected" in statuses:
            status = "rejected"
        elif "error" in statuses:
            status = "failed"
        else:
            status = "succeeded"
        self.record_ticket(
            status=status,
            agent=trace.agent,
            failure_class=_failure_class_of(trace) if status == "failed" else None,
        )
        for span in trace.spans:
            stage = SPAN_TO_STAGE.get(span.name)
            if stage is None:
                continue
            self.record_stage(
                stage,
                (span.duration_ms or 0.0) / 1000.0,
                ok=span.status == "ok",
            )

    # -- 输出 ---------------------------------------------------------
    def render(self) -> str:
        lines: list[str] = []
        for metric in self._metrics:
            lines.extend(metric.render())
        return "\n".join(lines) + "\n"


def _count_value(value: int, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise MetricError(f"{name} 必须是非负整数，实际 {value!r}")
    return value


def _enum_name(value: Any) -> str:
    """取枚举成员名（``LineStatus.MATCHED`` → ``MATCHED``），非枚举按原值转字符串。

    只用 ``.name`` 而不比对 ``.value`` 的中文文案：中文是展示口径，改名不该打穿指标。
    """

    return str(getattr(value, "name", value))


def classify_recon_line(line: Any) -> str:
    """把一条 ``MatchResultLine`` 归入封闭词表；状态优先于匹配类型。

    ``UNPARSEABLE`` 行虽然 ``match_type`` 是 ``UNMATCHED``，仍单列——它是最需要被看见的
    降级来源。未识别的状态归 ``unknown``，绝不新造标签值。
    """

    status = _enum_name(getattr(line, "status", None))
    if status == "MATCHED":
        return _MATCH_TYPE_TO_OUTCOME.get(_enum_name(getattr(line, "match_type", None)), "unknown")
    return _LINE_STATUS_TO_OUTCOME.get(status, "unknown")


def _degradation_reason(result: Any) -> str:
    """从缺失项里取一个封闭的原因标签；没有可用缺失项时为 ``other``。"""

    for item in getattr(result, "missing_items", None) or ():
        reason = _enum_name(getattr(item, "kind", None)).lower()
        if reason in RECON_DEGRADATION_REASONS:
            return reason
    return "other"


def _failure_class_of(trace: Any) -> str | None:
    for span in reversed(trace.spans):
        for key in ("failure_class", "fail_code"):
            value = span.attributes.get(key)
            if value:
                return str(value)
    return None


# ---------------------------------------------------------------- ASGI 端点


class MetricsEndpoint:
    """纯 ASGI ``/metrics`` 端点；只响应 GET/HEAD。"""

    content_type = "text/plain; version=0.0.4; charset=utf-8"

    def __init__(self, registry: MetricsRegistry) -> None:
        self._registry = registry

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            return
        if scope.get("method") not in {"GET", "HEAD"}:
            await _send_response(send, 405, b"method not allowed\n", "text/plain; charset=utf-8")
            return
        body = self._registry.render().encode("utf-8")
        await _send_response(send, 200, body, self.content_type, head=scope.get("method") == "HEAD")


async def _send_response(
    send: Any,
    status: int,
    body: bytes,
    content_type: str,
    *,
    head: bool = False,
) -> None:
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", content_type.encode("ascii")),
                (b"content-length", str(len(body)).encode("ascii")),
            ],
        }
    )
    await send({"type": "http.response.body", "body": b"" if head else body})


def metrics_enabled(env: Mapping[str, str] | None = None) -> bool:
    """``/metrics`` 是否开启；未配置即关闭，避免生产默认暴露内部指标。"""

    source = os.environ if env is None else env
    return str(source.get(METRICS_ENABLED_VAR, "")).strip().lower() in _TRUTHY


def install_metrics(
    app: Any,
    registry: MetricsRegistry,
    *,
    path: str | None = None,
    enabled: bool | None = None,
    env: Mapping[str, str] | None = None,
) -> bool:
    """按显式配置把 ``/metrics`` 挂到 FastAPI/Starlette 应用。

    默认**不挂载**：``enabled=None`` 时读 ``ERP_METRICS_ENABLED``（缺省关闭），
    与 trace 的 ``ERP_TRACE_ENABLED`` 同一原则。返回是否真的挂载，便于启动日志留痕。

    ``/metrics`` 只允许内网访问：挂载到业务 app 就会继承业务端口的暴露面，
    因此生产应由反向代理拒绝该路径，或改用独立内网监听（见 README §6.0）。
    """

    source = dict(os.environ) if env is None else dict(env)
    if enabled is None:
        enabled = metrics_enabled(source)
    if not enabled:
        return False

    resolved = path or source.get(METRICS_PATH_VAR) or DEFAULT_METRICS_PATH
    if any(char.isspace() for char in resolved):
        # 尾随空格会注册出一个永远匹配不到的路由：运维改 nginx deny 也跟着改，
        # 而端点静默 404——属于「配了但不生效」，必须在启动期炸掉。
        raise MetricError(f"metrics 路径不得含空白字符，实际 {resolved!r}")
    if not resolved.startswith("/"):
        raise MetricError(f"metrics 路径必须以 / 开头，实际 {resolved!r}")

    from starlette.routing import Route

    app.router.routes.append(
        Route(resolved, MetricsEndpoint(registry), methods=["GET", "HEAD"], include_in_schema=False)
    )
    return True


def render_metrics(registry: MetricsRegistry) -> str:
    return registry.render()
