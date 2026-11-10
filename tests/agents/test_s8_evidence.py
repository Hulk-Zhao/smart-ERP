"""S8 四个复验场景的 pytest 入口（与 ``python tests/agents/s8_evidence.py`` 同一套逻辑）。"""

from __future__ import annotations

import asyncio

from tests.agents.s8_evidence import run_all


def test_s8_evidence_scenarios(db: str) -> None:
    results = asyncio.run(run_all(db))
    assert all(item["ok"] for item in results), results

    by_name = {item["scenario"]: item for item in results}
    assert by_name["cli_5_jobs"]["queue_states"] == {"succeeded": 5}
    assert by_name["cli_5_jobs"]["heartbeat"]["jobs_done"] == 5

    retry = by_name["retry_and_dead"]
    assert retry["done"] == 10
    assert retry["failed_attempts"] == 10
    assert retry["queue_states"] == {"succeeded": 10, "dead": 4}
    assert retry["retries"] == 6

    kill9 = by_name["kill9_reclaim"]
    assert kill9["health_ok_after_kill"] is False
    assert kill9["reclaimed"] == 1
    assert kill9["reclaimed_same_job"] is True

    sched = by_name["scheduler_single_point"]
    assert sched["leaders_at_start"] == [sched["killed"]]
    assert sched["owner_after_expiry"] != sched["first_owner"]
    assert sched["takeover_logged"] is True
