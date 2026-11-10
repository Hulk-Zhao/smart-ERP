"""性能基线与可复现性断言（只读）。"""

from __future__ import annotations

import hashlib

from apps.masterdata.benchmark import (
    build_snapshot,
    canonical_json,
    entity_counts,
)
from apps.masterdata.engine import inspect
from apps.masterdata.impact import build_downgrade_list


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
