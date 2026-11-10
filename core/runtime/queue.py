"""C15 PG 队列存取（``erp.job_queue``）。

语义与 01-技术架构 §1A.7.3 一致：

- 认领：``FOR UPDATE SKIP LOCKED``，单条 UPDATE 原子完成 running + 租约 + attempts+1；
- 成功：``succeeded``（终态）；
- 失败：未到 ``max_attempts`` → 回 ``queued`` 并按退避设置 ``run_at``；
  到上限 → ``dead``（终态，不再自动重试，等人工重放）；
- 回收：``running`` 且 ``lease_expires_at < now()`` 视为 owner 已死，
  未超限回 ``queued``，超限转 ``dead``；
- 单跑由状态机（``state='running'`` + 租约）保证，不靠唯一索引硬撞。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

# 四个决定「会不会误判死亡、会不会重复执行」的默认值，CLI/文档与此保持一致。
# 认领时把租约写进 lease_expires_at；只有它过期，别的 worker 才能回收。
DEFAULT_LEASE_SECONDS = 60.0
# 单条任务最多尝试次数；到上限直接 dead，等人工重放，不无限转圈。
DEFAULT_MAX_ATTEMPTS = 3
# 失败后回队列的退避时间；可由 worker 的 --retry-backoff-seconds 覆盖。
DEFAULT_RETRY_BACKOFF_SECONDS = 5.0
# 单次 reclaim_stale 最多回收条数，避免一次事务锁太多行。
DEFAULT_RECLAIM_BATCH = 100


@dataclass(frozen=True, slots=True)
class Job:
    id: int
    queue: str
    kind: str
    payload: dict[str, Any]
    attempts: int
    max_attempts: int


_CLAIM_SQL = text(
    """
    UPDATE erp.job_queue AS j
       SET state = 'running',
           lease_owner = :owner,
           lease_expires_at = now() + make_interval(secs => :lease_seconds),
           attempts = j.attempts + 1
     WHERE j.id = (
             SELECT id
               FROM erp.job_queue
              WHERE state = 'queued'
                AND queue = ANY(:queues)
                AND run_at <= now()
                AND attempts < max_attempts
              ORDER BY priority DESC, run_at, id
              LIMIT 1
              FOR UPDATE SKIP LOCKED
           )
    RETURNING j.id, j.queue, j.kind, j.payload, j.attempts, j.max_attempts
    """
)

_SETTLE_FAILED_SQL = text(
    """
    UPDATE erp.job_queue
       SET state = CASE WHEN attempts >= max_attempts THEN 'dead' ELSE 'queued' END,
           run_at = CASE
                      WHEN attempts >= max_attempts THEN run_at
                      ELSE now() + make_interval(secs => :backoff_seconds)
                    END,
           last_error = :error,
           lease_owner = NULL,
           lease_expires_at = NULL
     WHERE id = :job_id
       AND state = 'running'
       AND lease_owner = :owner
    RETURNING state
    """
)


class JobQueueStore:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sf = session_factory

    async def enqueue(
        self,
        queue: str,
        kind: str,
        payload: Mapping[str, Any] | None = None,
        *,
        priority: int = 0,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        delay_seconds: float = 0.0,
    ) -> int:
        async with self._sf() as session, session.begin():
            job_id = (
                await session.execute(
                    text(
                        "INSERT INTO erp.job_queue "
                        "(queue, kind, payload, priority, max_attempts, run_at) "
                        "VALUES (:queue, :kind, CAST(:payload AS jsonb), :priority, "
                        ":max_attempts, now() + make_interval(secs => :delay)) "
                        "RETURNING id"
                    ),
                    {
                        "queue": queue,
                        "kind": kind,
                        "payload": json.dumps(payload or {}),
                        "priority": priority,
                        "max_attempts": max_attempts,
                        "delay": delay_seconds,
                    },
                )
            ).scalar_one()
        return int(job_id)

    async def claim(
        self,
        queues: Sequence[str],
        *,
        owner: str,
        lease_seconds: float = DEFAULT_LEASE_SECONDS,
    ) -> Job | None:
        if not queues:
            return None
        async with self._sf() as session, session.begin():
            row = (
                await session.execute(
                    _CLAIM_SQL,
                    {
                        "owner": owner,
                        "lease_seconds": lease_seconds,
                        "queues": list(queues),
                    },
                )
            ).mappings().one_or_none()
        if row is None:
            return None
        return Job(
            id=int(row["id"]),
            queue=row["queue"],
            kind=row["kind"],
            payload=row["payload"] or {},
            attempts=int(row["attempts"]),
            max_attempts=int(row["max_attempts"]),
        )

    async def heartbeat_lease(
        self, job_id: int, *, owner: str, lease_seconds: float = DEFAULT_LEASE_SECONDS
    ) -> bool:
        async with self._sf() as session, session.begin():
            result = await session.execute(
                text(
                    "UPDATE erp.job_queue "
                    "SET lease_expires_at = now() + make_interval(secs => :lease_seconds) "
                    "WHERE id = :job_id AND state = 'running' AND lease_owner = :owner"
                ),
                {"job_id": job_id, "owner": owner, "lease_seconds": lease_seconds},
            )
            return result.rowcount > 0

    async def complete(self, job_id: int, *, owner: str) -> bool:
        async with self._sf() as session, session.begin():
            result = await session.execute(
                text(
                    "UPDATE erp.job_queue "
                    "SET state = 'succeeded', lease_owner = NULL, lease_expires_at = NULL "
                    "WHERE id = :job_id AND state = 'running' AND lease_owner = :owner"
                ),
                {"job_id": job_id, "owner": owner},
            )
            return result.rowcount > 0

    async def fail(
        self,
        job_id: int,
        *,
        owner: str,
        error: str,
        backoff_seconds: float = DEFAULT_RETRY_BACKOFF_SECONDS,
        permanent: bool = False,
    ) -> str:
        """失败落账；返回终态 ``queued``（可重试）或 ``dead``（终态）。

        ``permanent=True`` 用于「未知 kind / 反序列化失败」这类重试也不会好的错误，
        直接 dead，避免永远转圈。
        """

        async with self._sf() as session, session.begin():
            if permanent:
                row = (
                    await session.execute(
                        text(
                            "UPDATE erp.job_queue "
                            "SET state = 'dead', last_error = :error, "
                            "lease_owner = NULL, lease_expires_at = NULL "
                            "WHERE id = :job_id AND state = 'running' "
                            "AND lease_owner = :owner RETURNING state"
                        ),
                        {"job_id": job_id, "owner": owner, "error": error},
                    )
                ).scalar_one_or_none()
            else:
                row = (
                    await session.execute(
                        _SETTLE_FAILED_SQL,
                        {
                            "job_id": job_id,
                            "owner": owner,
                            "error": error,
                            "backoff_seconds": backoff_seconds,
                        },
                    )
                ).scalar_one_or_none()
        if row is None:
            raise RuntimeError(f"任务 {job_id} 不在 running/{owner} 名下，无法落失败态")
        return str(row)

    async def reclaim_stale(
        self, *, backoff_seconds: float = 0.0, limit: int = DEFAULT_RECLAIM_BATCH
    ) -> int:
        """回收租约过期的 running：未超限回 queued，超限转 dead。返回条数。"""

        async with self._sf() as session, session.begin():
            result = await session.execute(
                text(
                    """
                    UPDATE erp.job_queue
                       SET state = CASE
                                     WHEN attempts >= max_attempts THEN 'dead'
                                     ELSE 'queued'
                                   END,
                           run_at = CASE
                                      WHEN attempts >= max_attempts THEN run_at
                                      ELSE now() + make_interval(secs => :backoff)
                                    END,
                           last_error = CASE
                                          WHEN attempts >= max_attempts
                                            THEN coalesce(last_error, '') || ' [lease expired -> dead]'
                                          ELSE coalesce(last_error, '') || ' [lease expired]'
                                        END,
                           lease_owner = NULL,
                           lease_expires_at = NULL
                     WHERE id IN (
                             SELECT id
                               FROM erp.job_queue
                              WHERE state = 'running'
                                AND lease_expires_at < now()
                              ORDER BY lease_expires_at
                              LIMIT :limit
                              FOR UPDATE SKIP LOCKED
                           )
                    """
                ),
                {"backoff": backoff_seconds, "limit": limit},
            )
            return int(result.rowcount or 0)

    async def stats(self, queues: Sequence[str] | None = None) -> dict[str, int]:
        sql = "SELECT state, count(*) AS n FROM erp.job_queue"
        params: dict[str, Any] = {}
        if queues:
            sql += " WHERE queue = ANY(:queues)"
            params["queues"] = list(queues)
        sql += " GROUP BY state"
        async with self._sf() as session:
            rows = (await session.execute(text(sql), params)).all()
        return {str(state): int(n) for state, n in rows}
