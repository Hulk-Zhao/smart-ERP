"""scheduler 的 tick 配置口径：命令行 > ERP_SCHEDULER_TICK_SECONDS > 默认 5s。

B-COMPOSE-METRICS：compose 里写着 60，代码却只认 --tick-seconds，这个变量从未被读取。
这里把「真读」和「读不到就大声失败」都钉成用例，避免再退回注释约定。
"""

from __future__ import annotations

import asyncio
import os
import unittest
from unittest import mock

from core.runtime import scheduler as sched

ENV_KEY = "ERP_SCHEDULER_TICK_SECONDS"


def _parse_with_env(value: str | None, argv: list[str] | None = None):
    env = dict(os.environ)
    env.pop(ENV_KEY, None)
    if value is not None:
        env[ENV_KEY] = value
    with mock.patch.dict(os.environ, env, clear=True):
        return sched._parse_args(argv or [])


class SchedulerTickEnvTest(unittest.TestCase):
    def test_env_value_is_read(self) -> None:
        args = _parse_with_env("60")
        self.assertEqual(args.tick_seconds, 60.0)
        self.assertEqual(args.tick_source, "env")

    def test_cli_beats_env(self) -> None:
        args = _parse_with_env("60", ["--tick-seconds", "7"])
        self.assertEqual(args.tick_seconds, 7.0)
        self.assertEqual(args.tick_source, "cli")

    def test_blank_env_falls_back_to_default(self) -> None:
        args = _parse_with_env("   ")
        self.assertEqual(args.tick_seconds, sched.DEFAULT_TICK_SECONDS)
        self.assertEqual(args.tick_source, "default")

    def test_unset_env_falls_back_to_default(self) -> None:
        args = _parse_with_env(None)
        self.assertEqual(args.tick_seconds, sched.DEFAULT_TICK_SECONDS)
        self.assertEqual(args.tick_source, "default")

    def test_non_numeric_env_fails_fast_with_key_name(self) -> None:
        with self.assertRaises(SystemExit) as ctx:
            _parse_with_env("sixty")
        self.assertIn(ENV_KEY, str(ctx.exception))

    def test_non_positive_env_fails_fast_with_key_name(self) -> None:
        with self.assertRaises(SystemExit) as ctx:
            _parse_with_env("0")
        self.assertIn(ENV_KEY, str(ctx.exception))

    def test_startup_log_reports_configured_lease_and_effective(self) -> None:
        """启动日志必须自证：配置值 / 来源 / lease / 实际生效间隔。

        CI 的 tests/agents 会先跑 ``alembic upgrade head``（PG 夹具），Alembic 的
        ``fileConfig`` 默认 ``disable_existing_loggers=True``，会把导入期已存在的
        ``erp.scheduler`` logger 置为 disabled；``assertLogs`` 只改 level 不改
        disabled，所以这里显式恢复，跑完还原。
        """
        scheduler = sched.Scheduler(
            session_factory=None,
            owner="test",
            tick_seconds=60.0,
            tick_source="env",
            lease_seconds=30.0,
        )
        scheduler.stop()
        was_disabled = sched.logger.disabled
        sched.logger.disabled = False
        try:
            with (
                self.assertLogs("erp.scheduler", level="INFO") as logs,
                mock.patch.object(sched.Scheduler, "release", new=mock.AsyncMock()),
            ):
                asyncio.run(scheduler.run())
        finally:
            sched.logger.disabled = was_disabled
        joined = "\n".join(logs.output)
        self.assertIn("tick_seconds=60.0 source=env", joined)
        self.assertIn("lease=30.0s effective=15.0s", joined)


if __name__ == "__main__":
    unittest.main()
