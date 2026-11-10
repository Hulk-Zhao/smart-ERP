"""S8 复验证据脚本：一条命令复跑四个场景，打印可核对的数字。

用法（先 ``alembic upgrade head``，再指向同一个库）：

    $env:ERP_TEST_DATABASE_URL="postgresql+asyncpg://…/smart_erp"
    python tests/agents/s8_evidence.py

输出四行 JSON + 一行总判定；任何一项不符预期退出码 1。
pytest 侧入口：``tests/agents/test_s8_evidence.py``（同一套 ``run_all``）。

场景与预期：
1. ``cli_5_jobs``：CLI worker 消费 5 条 → succeeded=5，心跳 jobs_done=5；
2. ``retry_and_dead``：14 条（10 echo + 3 三次失败 + 1 未知 kind）→ done=10、
   失败尝试 10、dead=4、重试 6，无永久 queued；
3. ``kill9_reclaim``：长任务被 kill -9 → 心跳停、探针失败 → 租约过期 → 可重新认领；
4. ``scheduler_single_point``：双进程同抢只有一个 leader，强杀后租约过期接管。
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.runtime.queue import JobQueueStore  # noqa: E402
from core.runtime.worker import HANDLERS, Worker  # noqa: E402
from tools.health import HealthConfig, check_once  # noqa: E402

REQUIRED_TABLES = ("erp.job_queue", "erp.worker_heartbeat", "erp.scheduler_lease")


def database_url() -> str:
    url = os.environ.get("ERP_TEST_DATABASE_URL") or os.environ.get("ERP_DATABASE_URL")
    if not url:
        sys.exit("请先设置 ERP_TEST_DATABASE_URL 或 ERP_DATABASE_URL")
    return url


def _subenv(url: str) -> dict[str, str]:
    env = os.environ.copy()
    env["ERP_DATABASE_URL"] = url
    env["ERP_LOG_LEVEL"] = "INFO"
    env["PYTHONPATH"] = str(ROOT)
    return env


async def _engine(url: str):
    return create_async_engine(url, poolclass=NullPool)


async def _execute(url: str, sql: str, params: dict | None = None) -> None:
    engine = await _engine(url)
    try:
        async with engine.begin() as conn:
            await conn.execute(text(sql), params or {})
    finally:
        await engine.dispose()


async def _fetch(url: str, sql: str, params: dict | None = None):
    engine = await _engine(url)
    try:
        async with engine.connect() as conn:
            return (await conn.execute(text(sql), params or {})).mappings().all()
    finally:
        await engine.dispose()


async def _truncate(url: str, tables: str) -> None:
    await _execute(url, f"TRUNCATE {tables}")


async def _enqueue(url: str, jobs: list[tuple[str, str, dict | None, dict]]) -> None:
    engine = await _engine(url)
    try:
        store = JobQueueStore(async_sessionmaker(engine, expire_on_commit=False))
        for queue, kind, payload, options in jobs:
            await store.enqueue(queue, kind, payload, **options)
    finally:
        await engine.dispose()


async def _queue_states(url: str) -> dict[str, int]:
    rows = await _fetch(
        url, "SELECT state, count(*) AS n FROM erp.job_queue GROUP BY state"
    )
    return {row["state"]: int(row["n"]) for row in rows}


async def _wait_job_running(url: str, timeout: float = 15.0) -> dict | None:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        rows = await _fetch(
            url, "SELECT id, state, lease_owner FROM erp.job_queue LIMIT 1"
        )
        if rows and rows[0]["state"] == "running":
            return dict(rows[0])
        await asyncio.sleep(0.2)
    return None


async def _wait_stale(url: str, timeout: float = 10.0) -> dict | None:
    """等到「租约过期 + 心跳年龄超过 2s」；返回当时的读数，超时返回 None。"""

    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        job = (
            await _fetch(
                url,
                "SELECT lease_expires_at < now() AS expired FROM erp.job_queue LIMIT 1",
            )
        )[0]
        beat = (
            await _fetch(
                url,
                "SELECT extract(epoch FROM now() - heartbeat_at) AS age "
                "FROM erp.worker_heartbeat WHERE worker_id = 's8-killme'",
            )
        )[0]
        if job["expired"] and float(beat["age"]) >= 2:
            return {"expired": True, "age": float(beat["age"])}
        await asyncio.sleep(0.2)
    return None


async def scenario_cli_5_jobs(url: str) -> dict:
    await _truncate(url, "erp.job_queue, erp.worker_heartbeat")
    await _enqueue(url, [("agent", "echo", None, {}) for _ in range(5)])
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "core.runtime.worker",
            "--queues",
            "agent",
            "--worker-id",
            "s8-cli",
            "--max-jobs",
            "5",
            "--poll-seconds",
            "0.1",
            "--heartbeat-seconds",
            "0.5",
            "--lease-seconds",
            "5",
        ],
        cwd=ROOT,
        env=_subenv(url),
        capture_output=True,
        text=True,
        timeout=120,
    )
    states = await _queue_states(url)
    beat = (
        await _fetch(
            url,
            "SELECT state, jobs_done, jobs_failed FROM erp.worker_heartbeat "
            "WHERE worker_id = 's8-cli'",
        )
    )[0]
    ok = (
        completed.returncode == 0
        and states == {"succeeded": 5}
        and beat["jobs_done"] == 5
        and beat["jobs_failed"] == 0
    )
    return {
        "scenario": "cli_5_jobs",
        "ok": ok,
        "exit_code": completed.returncode,
        "queue_states": states,
        "heartbeat": dict(beat),
    }


async def scenario_retry_and_dead(url: str) -> dict:
    async def boom(_job: object) -> None:
        raise RuntimeError("s8-evidence-fail")

    await _truncate(url, "erp.job_queue, erp.worker_heartbeat")
    kinds = [("agent", "echo", None, {}) for _ in range(10)]
    kinds += [("agent", "boom", None, {"max_attempts": 3}) for _ in range(3)]
    kinds.append(("agent", "no-such-kind", None, {}))
    await _enqueue(url, kinds)
    engine = await _engine(url)
    try:
        worker = Worker(
            session_factory=async_sessionmaker(engine, expire_on_commit=False),
            queues=["agent"],
            worker_id="s8-stats",
            concurrency=2,
            lease_seconds=10,
            heartbeat_seconds=0.2,
            poll_seconds=0.01,
            retry_backoff_seconds=0.01,
            handlers={**HANDLERS, "boom": boom},
        )
        stats = await worker.run(max_jobs=20)
        states = await _queue_states(url)
        retries = (
            await _fetch(
                url, "SELECT coalesce(sum(attempts - 1), 0) AS n FROM erp.job_queue"
            )
        )[0]["n"]
    finally:
        await engine.dispose()
    ok = (
        stats.done == 10
        and stats.failed == 10
        and states == {"succeeded": 10, "dead": 4}
        and int(retries) == 6
    )
    return {
        "scenario": "retry_and_dead",
        "ok": ok,
        "enqueued": 14,
        "done": stats.done,
        "failed_attempts": stats.failed,
        "queue_states": states,
        "retries": int(retries),
    }


def _hard_kill(proc: subprocess.Popen) -> None:
    proc.kill()  # Windows: TerminateProcess；POSIX: SIGKILL
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:  # pragma: no cover - 进程已不可杀
        pass


async def scenario_kill9_reclaim(url: str) -> dict:
    await _truncate(url, "erp.job_queue, erp.worker_heartbeat")
    await _enqueue(url, [("agent", "sleep", {"seconds": 60}, {})])
    log = tempfile.NamedTemporaryFile(delete=False, suffix=".log")
    proc = subprocess.Popen(
        [
            sys.executable,
            "-u",
            "-m",
            "core.runtime.worker",
            "--queues",
            "agent",
            "--worker-id",
            "s8-killme",
            "--lease-seconds",
            "3",
            "--heartbeat-seconds",
            "0.5",
            "--poll-seconds",
            "0.2",
        ],
        cwd=ROOT,
        env=_subenv(url),
        stdout=subprocess.DEVNULL,
        stderr=log,
    )
    try:
        running = await _wait_job_running(url)
        _hard_kill(proc)
        await asyncio.sleep(0.3)
        exited = proc.poll() is not None
        stale = await _wait_stale(url)
        job = (await _fetch(url, "SELECT id, state FROM erp.job_queue"))[0]
        beat = (
            await _fetch(
                url,
                "SELECT state, extract(epoch FROM now() - heartbeat_at) AS age "
                "FROM erp.worker_heartbeat WHERE worker_id = 's8-killme'",
            )
        )[0]
        health = await check_once(
            HealthConfig(target="worker", database_url=url, stale_seconds=2.0)
        )
        engine = await _engine(url)
        try:
            store = JobQueueStore(async_sessionmaker(engine, expire_on_commit=False))
            reclaimed = await store.reclaim_stale()
            again = await store.claim(["agent"], owner="s8-fresh", lease_seconds=30)
            if again is not None:
                await store.complete(again.id, owner="s8-fresh")
        finally:
            await engine.dispose()
    finally:
        if proc.poll() is None:
            _hard_kill(proc)
        log.close()
        os.unlink(log.name)
    ok = (
        running is not None
        and exited
        and stale is not None
        and not health.ok
        and reclaimed == 1
        and again is not None
        and again.id == job["id"]
    )
    return {
        "scenario": "kill9_reclaim",
        "ok": ok,
        "claimed_before_kill": running is not None,
        "process_exited": exited,
        "heartbeat_age_seconds": round(float(beat["age"]), 1),
        "health_ok_after_kill": health.ok,
        "lease_expired": bool(stale and stale["expired"]),
        "reclaimed": reclaimed,
        "reclaimed_same_job": again is not None and again.id == job["id"],
    }


async def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return ""


async def scenario_scheduler_single_point(url: str) -> dict:
    await _truncate(url, "erp.scheduler_lease")
    log_dir = Path(tempfile.mkdtemp(prefix="s8-sched-"))
    logs = {name: log_dir / f"{name}.log" for name in ("A", "B")}
    handles = {name: open(path, "w", encoding="utf-8") for name, path in logs.items()}
    procs: dict[str, subprocess.Popen] = {}
    try:
        for name in ("A", "B"):
            procs[name] = subprocess.Popen(
                [
                    sys.executable,
                    "-u",
                    "-m",
                    "core.runtime.scheduler",
                    "--owner",
                    f"s8-{name}",
                    "--lease-seconds",
                    "4",
                    "--tick-seconds",
                    "1",
                ],
                cwd=ROOT,
                env=_subenv(url),
                stdout=subprocess.DEVNULL,
                stderr=handles[name],
            )
        await asyncio.sleep(3)
        first_owner = (
            await _fetch(
                url, "SELECT owner FROM erp.scheduler_lease WHERE scope = 'scheduler'"
            )
        )[0]["owner"]
        leaders_initial = [
            name
            for name in ("A", "B")
            if "获得租约" in await _read(logs[name])
        ]
        loser = "B" if first_owner == "s8-A" else "A"
        owner_name = "A" if first_owner == "s8-A" else "B"
        _hard_kill(procs[owner_name])
        await asyncio.sleep(6)
        second_owner = (
            await _fetch(
                url, "SELECT owner FROM erp.scheduler_lease WHERE scope = 'scheduler'"
            )
        )[0]["owner"]
        takeover_logged = "获得租约" in await _read(logs[loser])
    finally:
        for proc in procs.values():
            if proc.poll() is None:
                _hard_kill(proc)
        for handle in handles.values():
            handle.close()
        await _execute(
            url,
            "UPDATE erp.scheduler_lease SET owner = NULL, expires_at = now() "
            "WHERE scope = 'scheduler'",
        )
        shutil.rmtree(log_dir, ignore_errors=True)
    ok = (
        first_owner in ("s8-A", "s8-B")
        and leaders_initial == [owner_name]
        and second_owner == f"s8-{loser}"
        and takeover_logged
    )
    return {
        "scenario": "scheduler_single_point",
        "ok": ok,
        "first_owner": first_owner,
        "leaders_at_start": leaders_initial,
        "killed": owner_name,
        "owner_after_expiry": second_owner,
        "takeover_logged": takeover_logged,
    }


async def _preflight(url: str) -> None:
    engine = await _engine(url)
    try:
        async with engine.connect() as conn:
            missing = [
                table
                for table in REQUIRED_TABLES
                if (await conn.execute(text("SELECT to_regclass(:n)"), {"n": table})).scalar()
                is None
            ]
    finally:
        await engine.dispose()
    if missing:
        raise SystemExit(f"缺表 {missing}，请先 alembic upgrade head")


async def run_all(url: str) -> list[dict]:
    await _preflight(url)
    results = [
        await scenario_cli_5_jobs(url),
        await scenario_retry_and_dead(url),
        await scenario_kill9_reclaim(url),
        await scenario_scheduler_single_point(url),
    ]
    return results


def main() -> None:
    url = database_url()
    results = asyncio.run(run_all(url))
    for item in results:
        print(json.dumps(item, ensure_ascii=False))
    passed = sum(1 for item in results if item["ok"])
    print(f"SUMMARY {passed}/{len(results)} passed")
    sys.exit(0 if passed == len(results) else 1)


if __name__ == "__main__":
    main()
