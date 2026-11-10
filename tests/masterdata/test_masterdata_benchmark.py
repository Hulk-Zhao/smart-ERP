"""性能基线与可复现性断言（只读）。

CI 只跑 1 万行：它是「30 秒预算」的固定上限，10 万行不进 CI（太慢），
10 万行的数字只作为人工基准记录在 ``PERFORMANCE.md``。
"""

from __future__ import annotations

import hashlib
import time

from apps.masterdata.benchmark import (
    build_snapshot,
    canonical_json,
    entity_counts,
)
from apps.masterdata.engine import inspect
from apps.masterdata.impact import build_downgrade_list

# CI 规模上限与预算：1 万行体检必须 30 秒内完成
CI_ROWS = 10_000
CI_BUDGET_SECONDS = 30.0


def test_entity_counts_sum_to_requested_total():
    for rows in (1000, 10_000, 100_000):
        counts = entity_counts(rows)
        assert counts.total == rows
        assert counts.suppliers > 0
        assert counts.customers > 0
        assert counts.items > 0
        assert counts.documents > 0


def test_snapshot_generation_is_deterministic():
    assert build_snapshot(1000) == build_snapshot(1000)


def test_same_input_produces_byte_identical_report():
    snapshot = build_snapshot(1000)
    first = canonical_json(inspect(snapshot).to_dict()).encode("utf-8")
    second = canonical_json(inspect(snapshot).to_dict()).encode("utf-8")
    assert first == second
    assert hashlib.sha256(first).hexdigest() == hashlib.sha256(second).hexdigest()


def test_downgrade_list_is_byte_identical_across_runs():
    snapshot = build_snapshot(1000)
    first = canonical_json(
        build_downgrade_list(inspect(snapshot), snapshot).to_dict()
    ).encode("utf-8")
    second = canonical_json(
        build_downgrade_list(inspect(snapshot), snapshot).to_dict()
    ).encode("utf-8")
    assert first == second


def test_ten_thousand_rows_within_budget_and_reproducible():
    """1 万行是 CI 的规模上限：既要 30 秒内跑完，又要两次输出逐字节相同。"""
    snapshot = build_snapshot(CI_ROWS)

    started = time.perf_counter()
    first = canonical_json(inspect(snapshot).to_dict()).encode("utf-8")
    elapsed = time.perf_counter() - started

    second = canonical_json(inspect(snapshot).to_dict()).encode("utf-8")
    assert first == second
    assert hashlib.sha256(first).hexdigest() == hashlib.sha256(second).hexdigest()
    assert elapsed <= CI_BUDGET_SECONDS, (
        f"1 万行体检耗时 {elapsed:.2f}s，超过 {CI_BUDGET_SECONDS:.0f}s 预算"
    )
