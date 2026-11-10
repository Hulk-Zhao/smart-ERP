from __future__ import annotations

import json
from decimal import Decimal

from apps.masterdata import (
    SAMPLE_CAVEAT,
    SIMULATION_NOTE,
    dirt_tiers_payload,
    simulate_dirt_tiers,
)


def test_three_dirt_tiers_in_order():
    tiers = simulate_dirt_tiers()
    assert [tier.name for tier in tiers] == ["轻脏", "中脏", "重脏"]
    assert [tier.defect_rate for tier in tiers] == [
        Decimal("0.02"),
        Decimal("0.08"),
        Decimal("0.2"),
    ]


def test_downgrade_ratio_grows_with_dirt():
    light, medium, heavy = simulate_dirt_tiers()
    for tier in (light, medium, heavy):
        assert tier.lower_pct != tier.upper_pct
        assert tier.ratio_uniform <= tier.ratio_skewed
        assert 0 < tier.ratio_uniform <= 1
        assert 0 < tier.ratio_skewed <= 1
        assert tier.total_lines == 1000
    assert light.ratio_skewed < medium.ratio_uniform
    assert medium.ratio_skewed < heavy.ratio_uniform


def test_light_tier_stays_small_and_heavy_tier_is_high():
    light, _, heavy = simulate_dirt_tiers()
    assert light.ratio_uniform <= 0.08
    assert light.ratio_skewed <= 0.15
    assert heavy.ratio_uniform >= 0.35
    assert heavy.ratio_skewed >= 0.5


def test_dirty_subject_counts_follow_defect_rate():
    light, medium, heavy = simulate_dirt_tiers()
    assert light.dirty_suppliers < medium.dirty_suppliers < heavy.dirty_suppliers
    assert light.dirty_items < medium.dirty_items < heavy.dirty_items
    assert light.dirty_customers <= medium.dirty_customers <= heavy.dirty_customers


def test_simulation_is_deterministic():
    assert simulate_dirt_tiers() == simulate_dirt_tiers()


def test_caveat_marks_synthetic_worst_case():
    assert "dirty.json" in SAMPLE_CAVEAT
    assert "不是真实客户样本" in SAMPLE_CAVEAT
    assert "1000" in SIMULATION_NOTE


def test_payload_is_json_serializable():
    payload = dirt_tiers_payload()
    assert payload["caveat"] == SAMPLE_CAVEAT
    assert payload["note"] == SIMULATION_NOTE
    assert len(payload["tiers"]) == 3
    tier = payload["tiers"][0]
    assert tier["tier"] == "轻脏"
    assert tier["range_pct"] == f"{tier['ratio_uniform_pct']}–{tier['ratio_skewed_pct']}"
    json.dumps(payload, ensure_ascii=False)
