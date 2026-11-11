"""孤儿 ``ticket_step.audit_image_id`` 巡检（PR4 / B-AUDIT-ORPHAN）。

问题定性：``ticket_step.audit_image_id`` 没有外键（见 0010 迁移的说明），
当它指向不存在的 ``erp.audit_image`` 行时，**审计链会显示某一步有外部证据，
但该证据行不存在**——属于审计证据完整性问题（无法自证），不是状态机错误。
容器装配一旦回退到进程内台账，这一步从出生起就悬空；本巡检是当前唯一会
发现它的地方。

一条 SQL 同时给出「孤儿数」与「样本」；SQL 本身与读路径共用
``apps/audit/image_link.py`` 的 ``ORPHAN_SQL`` / ``STEP_IMAGE_PREDICATE``，
这里不再复写一份，避免孤儿定义与影像列表分叉。

索引与代价：``NOT EXISTS`` 的探测走 ``erp.audit_image`` 的 ``id`` 前缀索引
（主键或 ``audit_image_id_tenant_uniq``）；``erp.ticket_step`` 的
``audit_image_id`` 上没有索引，因此外层是全表扫描。``ticket_step`` 一行对应
一次已执行成功的外部写入，与 ``audit_image`` 同一数量级（现有巡检按
「10 万行 ≈ 44 秒」估算，单次扫描为数十到数百毫秒）。

默认频率 900 秒（15 分钟），不是 60 秒：孤儿是持久的数据完整性缺陷，产生后
不会在秒级窗口自行修复，也不参与任何在线判定；15 分钟把全表扫描的占空比压到
约 0.1%，同时保证当天就能发现。启动后 5 秒先跑一次，让新容器的 ``docker logs``
立刻有今日结论。

双落点（今天都没有评估器/抓取端，所以日志必须是告警的可见载体）：

1. 指标 ``smart_erp_audit_orphan_steps``（gauge，无标签）进 ``/metrics``，
   供将来接告警；
2. ``WARNING`` 日志（``erp.audit.integrity``）含孤儿数与样本 id。``WARNING``
   级即使没有配置 logging handler，也会经 ``logging.lastResort`` 写 stderr，
   因此今天就能在 ``docker logs`` 里看到。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from typing import Any

from apps.audit.image_link import ORPHAN_SQL
from observability.metrics import MetricsRegistry

logger = logging.getLogger("erp.audit.integrity")

DEFAULT_INTERVAL_SECONDS = 900.0
DEFAULT_STARTUP_DELAY_SECONDS = 5.0
DEFAULT_SAMPLE_LIMIT = 20


@dataclass(frozen=True, slots=True)
class OrphanReport:
    orphan_count: int
    samples: tuple[dict[str, str], ...]
    elapsed_ms: float


@dataclass
class AuditIntegrityScanner:
    """周期跑孤儿引用巡检；``session_factory`` 缺省时首次使用时建引擎。"""

    session_factory: Any | None = None
    metrics: MetricsRegistry | None = None
    interval_seconds: float = DEFAULT_INTERVAL_SECONDS
    startup_delay_seconds: float = DEFAULT_STARTUP_DELAY_SECONDS
    sample_limit: int = DEFAULT_SAMPLE_LIMIT

    def _factory(self) -> Any:
        if self.session_factory is None:
            from apps.platform.db.session import build_engine, build_session_factory

            self.session_factory = build_session_factory(build_engine())
        return self.session_factory

    async def scan_once(self) -> OrphanReport:
        """跑一次巡检：写指标 + 打日志；异常向上抛，由调用方保持上次指标值。"""

        started = time.perf_counter()
        async with self._factory()() as session:
            row = (
                await session.execute(ORPHAN_SQL, {"sample_limit": self.sample_limit})
            ).one()
        count = int(row.orphan_count or 0)
        samples = tuple(json.loads(row.samples or "[]"))
        elapsed_ms = (time.perf_counter() - started) * 1000.0

        if self.metrics is not None:
            self.metrics.set_audit_orphan_steps(count)
        if count:
            logger.warning(
                "审计影像孤儿引用：orphan_steps=%d samples=%s "
                "（ticket_step.audit_image_id 指向不存在的 erp.audit_image 行："
                "审计链显示某步有外部证据，但证据行不存在）",
                count,
                json.dumps(samples, ensure_ascii=False),
            )
        else:
            logger.info(
                "审计影像孤儿引用巡检：orphan_steps=0 elapsed_ms=%.1f", elapsed_ms
            )
        return OrphanReport(orphan_count=count, samples=samples, elapsed_ms=elapsed_ms)

    async def run_forever(self) -> None:
        """常驻任务：先等一个短启动延迟，再按周期跑；失败不退出。"""

        await asyncio.sleep(self.startup_delay_seconds)
        while True:
            try:
                await self.scan_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning(
                    "审计影像孤儿引用巡检失败（保持上次指标值，下一周期重试）",
                    exc_info=True,
                )
            await asyncio.sleep(self.interval_seconds)


__all__ = [
    "DEFAULT_INTERVAL_SECONDS",
    "DEFAULT_SAMPLE_LIMIT",
    "DEFAULT_STARTUP_DELAY_SECONDS",
    "AuditIntegrityScanner",
    "OrphanReport",
]
