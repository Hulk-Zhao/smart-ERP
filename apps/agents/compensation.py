"""独立补偿队列：``compensate_seq`` 倒序消费（§3.1.2 / §3.1.5）。

「倒序」不是代码习惯，而是数据结构：

1. ``compensate_seq`` 必须等于原步骤 ``step_no``（DB 触发器强制，填错直接报错）；
2. ``UNIQUE(ticket_id, compensate_seq)`` 让重复入队幂等；
3. ``compensation_task_one_running_idx`` 保证同一工单同一时刻只有一个补偿在跑；
4. 出队 SQL 固定 ``ORDER BY compensate_seq DESC ... FOR UPDATE SKIP LOCKED``，
   队列只按数据顺序吐出任务，调用方无法「先删后建的顺序」乱来。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from apps.agents.models import COMPENSATION_STATES

_ENQUEUE_SQL = text(
    """
    INSERT INTO erp.compensation_task
        (id, ticket_id, original_step, compensate_seq, action_key, state)
    SELECT gen_random_uuid(), s.ticket_id, s.id, s.step_no, s.action_key, 'queued'
      FROM erp.ticket_step s
     WHERE s.ticket_id = :ticket_id
       AND s.state = 'succeeded'
    ON CONFLICT (ticket_id, compensate_seq) DO NOTHING
    """
)

_LIST_SQL = text(
    """
    SELECT id, ticket_id, original_step, compensate_seq, action_key,
           state, attempts, next_retry_at, last_error
      FROM erp.compensation_task
     WHERE ticket_id = :ticket_id
     ORDER BY compensate_seq DESC
    """
)

_CLAIM_SQL = text(
    """
    UPDATE erp.compensation_task
       SET state = 'running', attempts = attempts + 1
     WHERE id = (
           SELECT id
             FROM erp.compensation_task
            WHERE ticket_id = :ticket_id
              AND state = 'queued'
              AND (next_retry_at IS NULL OR next_retry_at <= now())
            ORDER BY compensate_seq DESC
            LIMIT 1
            FOR UPDATE SKIP LOCKED
     )
    RETURNING id, ticket_id, original_step, compensate_seq, action_key, state, attempts
    """
)

_DONE_SQL = text(
    """
    UPDATE erp.compensation_task
       SET state = 'done', last_error = NULL
     WHERE id = :task_id AND state = 'running'
    """
)

_FAILED_SQL = text(
    """
    UPDATE erp.compensation_task
       SET state = CASE WHEN attempts >= :max_attempts THEN 'failed' ELSE 'queued' END,
           last_error = :error,
           next_retry_at = CASE
               WHEN attempts >= :max_attempts THEN NULL
               ELSE now() + make_interval(secs => :retry_in)
           END
     WHERE id = :task_id
    """
)

_COUNTS_SQL = text(
    """
    SELECT state, count(*) AS n
      FROM erp.compensation_task
     WHERE ticket_id = :ticket_id
     GROUP BY state
    """
)


@dataclass(frozen=True, slots=True)
class CompensationTaskView:
    id: UUID
    ticket_id: UUID
    original_step: UUID
    compensate_seq: int
    action_key: str
    state: str
    attempts: int


@dataclass(frozen=True, slots=True)
class QueueCounts:
    queued: int = 0
    running: int = 0
    done: int = 0
    failed: int = 0

    @property
    def total(self) -> int:
        return self.queued + self.running + self.done + self.failed


def _view(row: Mapping[str, object]) -> CompensationTaskView:
    return CompensationTaskView(
        id=row["id"],  # type: ignore[arg-type]
        ticket_id=row["ticket_id"],  # type: ignore[arg-type]
        original_step=row["original_step"],  # type: ignore[arg-type]
        compensate_seq=int(row["compensate_seq"]),  # type: ignore[arg-type]
        action_key=str(row["action_key"]),
        state=str(row["state"]),
        attempts=int(row["attempts"]),  # type: ignore[arg-type]
    )


class CompensationQueue:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sf = session_factory

    async def enqueue(self, ticket_id: UUID) -> list[CompensationTaskView]:
        """把已成功步骤批量入队（幂等），返回倒序任务列表。"""

        async with self._sf() as session, session.begin():
            await session.execute(_ENQUEUE_SQL, {"ticket_id": ticket_id})
        return await self.list_tasks(ticket_id)

    async def list_tasks(self, ticket_id: UUID) -> list[CompensationTaskView]:
        async with self._sf() as session:
            rows = (await session.execute(_LIST_SQL, {"ticket_id": ticket_id})).mappings()
            return [_view(row) for row in rows]

    async def claim_next(self, ticket_id: UUID) -> CompensationTaskView | None:
        async with self._sf() as session, session.begin():
            row = (
                await session.execute(_CLAIM_SQL, {"ticket_id": ticket_id})
            ).mappings().one_or_none()
        return _view(row) if row is not None else None

    async def mark_done(self, task_id: UUID) -> None:
        async with self._sf() as session, session.begin():
            await session.execute(_DONE_SQL, {"task_id": task_id})

    async def mark_failed(
        self,
        task_id: UUID,
        error: str,
        *,
        max_attempts: int = 3,
        retry_in_seconds: float = 8.0,
    ) -> None:
        async with self._sf() as session, session.begin():
            await session.execute(
                _FAILED_SQL,
                {
                    "task_id": task_id,
                    "error": error[:2000],
                    "max_attempts": max_attempts,
                    "retry_in": float(retry_in_seconds),
                },
            )

    async def counts(self, ticket_id: UUID) -> QueueCounts:
        async with self._sf() as session:
            rows = (
                await session.execute(_COUNTS_SQL, {"ticket_id": ticket_id})
            ).mappings()
            data = {row["state"]: int(row["n"]) for row in rows}
        for state in COMPENSATION_STATES:
            data.setdefault(state, 0)
        return QueueCounts(
            queued=data["queued"],
            running=data["running"],
            done=data["done"],
            failed=data["failed"],
        )
