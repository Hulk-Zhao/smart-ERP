"""C15 PG 队列存取（``erp.job_queue``）。

语义与 01-技术架构 §1A.7.3 一致：

- 认领：``FOR UPDATE SKIP LOCKED``，单条 UPDATE 原子完成 running + 租约 + attempts+1
  + ``lease_gen``+1；``run_at IS NULL`` 表示「立即可领取」；
- 成功：``succeeded``（终态）；``complete`` / ``fail`` / ``heartbeat`` 都必须带回
  ``lease_gen``，与库里不一致时抛 :class:`LeaseLostError`（显式失败，不静默）；
- 失败：未到 ``max_attempts`` → 回 ``queued`` 并按退避设置 ``run_at``
  （``backoff=0`` 时写 NULL = 立即可领取，避免回拨把任务藏进未来）；
  到上限 → ``dead``（终态，不再自动重试，等人工重放）；
- 回收：``running`` 且 ``lease_expires_at`` 超过 ``grace_seconds`` 宽限仍未续租
  才视为 owner 已死（``exclude_owner`` 可排除调用方自己持有的行），
  未超限回 ``queued``（``backoff=0`` 同样写 NULL），超限转 ``dead``；
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
# reclaim 的宽限默认值。依据是租约的续约节奏而不是拍脑袋：
# 在跑任务每 ``lease_seconds / 3`` 续租一次（默认 60/3 = 20s），所以「上一次续约后
# 一个续租间隔内」租约随时可能看起来该续了；而本机 Docker Desktop 的时钟同步
# 已实测会让 ``clock_timestamp()`` 前跳 1s 级，前跳会把「还没续上的活租约」误判成过期。
# 宽限取一个续租间隔：过期超过 lease/3 才回收，挡住「漏一拍 + 秒级前跳」。
# 更大的前跳由 ``exclude_owner``（本次 sweep 不回收自己）与后续 ``lease_gen``
# 代次围栏兜底，不靠继续加宽 grace。
# 代价：真死掉的 worker 的任务最坏多等 ``grace + 一次回收间隔``。
# 默认租约 60s、心跳 10s（compose 下发 30s）：从最后一次续约算起约 60+20+10~30s
# 才被回收；不加宽限时是 60+10~30s，即这次改动让「真死」多等一个续租间隔。
DEFAULT_RECLAIM_GRACE_SECONDS = DEFAULT_LEASE_SECONDS / 3


@dataclass(frozen=True, slots=True)
class Job:
    id: int
    queue: str
    kind: str
    payload: dict[str, Any]
    attempts: int
    max_attempts: int
    # 认领代次：每次 claim +1。落账（complete/fail/heartbeat）必须带回来，
    # 不匹配说明手里的租约已被回收/重领，结果不能写进账本。
    lease_gen: int


class LeaseLostError(RuntimeError):
    """落账代次不匹配：调用方手里的租约已失效，操作被显式拒绝。"""

    def __init__(
        self,
        job_id: int,
        *,
        action: str,
        expected_owner: str,
        expected_lease_gen: int,
        actual_state: str | None,
        actual_owner: str | None,
        actual_lease_gen: int | None,
    ) -> None:
        self.job_id = job_id
        self.action = action
        self.expected_owner = expected_owner
        self.expected_lease_gen = expected_lease_gen
        self.actual_state = actual_state
        self.actual_owner = actual_owner
        self.actual_lease_gen = actual_lease_gen
        if actual_state is None:
            actual = "行已不存在"
        else:
            actual = (
                f"state={actual_state} lease_owner={actual_owner} "
                f"lease_gen={actual_lease_gen}"
            )
        super().__init__(
            f"任务 {job_id} 的租约已失效（{action}）："
            f"预期 state=running lease_owner={expected_owner} "
            f"lease_gen={expected_lease_gen}，实际 {actual}"
        )


_CLAIM_SQL = text(
    """
    UPDATE erp.job_queue AS j
       SET state = 'running',
           lease_owner = :owner,
           lease_expires_at = now() + make_interval(secs => :lease_seconds),
           attempts = j.attempts + 1,
           lease_gen = j.lease_gen + 1
     WHERE j.id = (
             SELECT id
               FROM erp.job_queue
              WHERE state = 'queued'
                AND queue = ANY(:queues)
                AND (run_at IS NULL OR run_at <= now())
                AND attempts < max_attempts
              ORDER BY priority DESC, run_at NULLS FIRST, id
              LIMIT 1
              FOR UPDATE SKIP LOCKED
           )
    RETURNING j.id, j.queue, j.kind, j.payload, j.attempts, j.max_attempts,
              j.lease_gen
    """
)

_SETTLE_FAILED_SQL = text(
    """
    UPDATE erp.job_queue
       SET state = CASE WHEN attempts >= max_attempts THEN 'dead' ELSE 'queued' END,
           run_at = CASE
                      WHEN attempts >= max_attempts THEN run_at
                      WHEN :immediate THEN NULL
                      ELSE now() + make_interval(secs => :backoff_seconds)
                    END,
           last_error = :error,
           lease_owner = NULL,
           lease_expires_at = NULL
     WHERE id = :job_id
       AND state = 'running'
       AND lease_owner = :owner
       AND lease_gen = :lease_gen
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
                        ":max_attempts, "
                        "CASE WHEN :immediate THEN NULL "
                        "ELSE now() + make_interval(secs => :delay) END) "
                        "RETURNING id"
                    ),
                    {
                        "queue": queue,
                        "kind": kind,
                        "payload": json.dumps(payload or {}),
                        "priority": priority,
                        "max_attempts": max_attempts,
                        "delay": delay_seconds,
                        "immediate": delay_seconds <= 0,
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
            lease_gen=int(row["lease_gen"]),
        )

    async def _lease_lost(
        self,
        session: AsyncSession,
        job_id: int,
        *,
        action: str,
        owner: str,
        lease_gen: int,
    ) -> LeaseLostError:
        """落账没命中时读回真实值，抛出带「预期/实际」的显式失败。"""

        row = (
            await session.execute(
                text(
                    "SELECT state, lease_owner, lease_gen FROM erp.job_queue "
                    "WHERE id = :job_id"
                ),
                {"job_id": job_id},
            )
        ).mappings().one_or_none()
        if row is None:
            return LeaseLostError(
                job_id,
                action=action,
                expected_owner=owner,
                expected_lease_gen=lease_gen,
                actual_state=None,
                actual_owner=None,
                actual_lease_gen=None,
            )
        return LeaseLostError(
            job_id,
            action=action,
            expected_owner=owner,
            expected_lease_gen=lease_gen,
            actual_state=str(row["state"]),
            actual_owner=row["lease_owner"],
            actual_lease_gen=int(row["lease_gen"]),
        )

    async def heartbeat_lease(
        self,
        job_id: int,
        *,
        owner: str,
        lease_gen: int,
        lease_seconds: float = DEFAULT_LEASE_SECONDS,
    ) -> None:
        """续租；代次不匹配时抛 :class:`LeaseLostError`，绝不静默返回。"""

        async with self._sf() as session, session.begin():
            result = await session.execute(
                text(
                    "UPDATE erp.job_queue "
                    "SET lease_expires_at = now() + make_interval(secs => :lease_seconds) "
                    "WHERE id = :job_id AND state = 'running' "
                    "AND lease_owner = :owner AND lease_gen = :lease_gen"
                ),
                {
                    "job_id": job_id,
                    "owner": owner,
                    "lease_gen": lease_gen,
                    "lease_seconds": lease_seconds,
                },
            )
            if result.rowcount == 0:
                error = await self._lease_lost(
                    session,
                    job_id,
                    action="heartbeat",
                    owner=owner,
                    lease_gen=lease_gen,
                )
                raise error

    async def complete(self, job_id: int, *, owner: str, lease_gen: int) -> None:
        """成功落账；代次不匹配时抛 :class:`LeaseLostError`。"""

        async with self._sf() as session, session.begin():
            result = await session.execute(
                text(
                    "UPDATE erp.job_queue "
                    "SET state = 'succeeded', lease_owner = NULL, lease_expires_at = NULL "
                    "WHERE id = :job_id AND state = 'running' "
                    "AND lease_owner = :owner AND lease_gen = :lease_gen"
                ),
                {"job_id": job_id, "owner": owner, "lease_gen": lease_gen},
            )
            if result.rowcount == 0:
                error = await self._lease_lost(
                    session,
                    job_id,
                    action="complete",
                    owner=owner,
                    lease_gen=lease_gen,
                )
                raise error

    async def fail(
        self,
        job_id: int,
        *,
        owner: str,
        lease_gen: int,
        error: str,
        backoff_seconds: float = DEFAULT_RETRY_BACKOFF_SECONDS,
        permanent: bool = False,
    ) -> str:
        """失败落账；返回终态 ``queued``（可重试）或 ``dead``（终态）。

        ``permanent=True`` 用于「未知 kind / 反序列化失败」这类重试也不会好的错误，
        直接 dead，避免永远转圈。代次不匹配时抛 :class:`LeaseLostError`。
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
                            "AND lease_owner = :owner AND lease_gen = :lease_gen "
                            "RETURNING state"
                        ),
                        {
                            "job_id": job_id,
                            "owner": owner,
                            "lease_gen": lease_gen,
                            "error": error,
                        },
                    )
                ).scalar_one_or_none()
            else:
                row = (
                    await session.execute(
                        _SETTLE_FAILED_SQL,
                        {
                            "job_id": job_id,
                            "owner": owner,
                            "lease_gen": lease_gen,
                            "error": error,
                            "backoff_seconds": backoff_seconds,
                            "immediate": backoff_seconds <= 0,
                        },
                    )
                ).scalar_one_or_none()
            if row is None:
                lost = await self._lease_lost(
                    session,
                    job_id,
                    action="fail",
                    owner=owner,
                    lease_gen=lease_gen,
                )
                raise lost
            return str(row)

    async def reclaim_stale(
        self,
        *,
        backoff_seconds: float = 0.0,
        limit: int = DEFAULT_RECLAIM_BATCH,
        exclude_owner: str | None = None,
        grace_seconds: float = 0.0,
    ) -> int:
        """回收租约过期的 running：未超限回 queued，超限转 dead。返回条数。

        ``exclude_owner``：跳过该 owner 持有的行。worker 传入自己的 ``worker_id``，
        这样「自己还在跑、但库时钟前跳把租约看成已过期」时不会自己把自己判死。
        ``grace_seconds``：过期宽限，只有 ``lease_expires_at < now() - grace``
        才算过期。默认 0 是严格语义；常驻进程应传自己的续租间隔
        （``lease_seconds / 3``，见 :data:`DEFAULT_RECLAIM_GRACE_SECONDS`）。
        """

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
                                      WHEN :immediate THEN NULL
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
                                AND lease_expires_at < now() - make_interval(secs => :grace)
                                AND (
                                      CAST(:exclude_owner AS text) IS NULL
                                      OR lease_owner IS DISTINCT FROM CAST(:exclude_owner AS text)
                                    )
                              ORDER BY lease_expires_at
                              LIMIT :limit
                              FOR UPDATE SKIP LOCKED
                           )
                    """
                ),
                {
                    "backoff": backoff_seconds,
                    "limit": limit,
                    "grace": grace_seconds,
                    "exclude_owner": exclude_owner,
                    "immediate": backoff_seconds <= 0,
                },
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
