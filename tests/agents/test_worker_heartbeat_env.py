"""worker 心跳配置口径：命令行 > ERP_HEARTBEAT_INTERVAL_SECONDS > 默认 10s。

B-HEARTBEAT-ENV：compose 一直下发 30，但 worker 只认 --heartbeat-seconds，这个值从未生效。
比接线更重要的是两道闸：
- 解析：空值回落、非数字/≤0 启动即失败并带键名；
- 语义：心跳间隔超过租约的一半直接拒绝构造（30 合法，300 起不来）。
"""

from __future__ import annotations

import os
import unittest
from unittest import mock

from core.runtime import worker as worker_mod

ENV_KEY = "ERP_HEARTBEAT_INTERVAL_SECONDS"


def _parse_with_env(value: str | None, argv: list[str] | None = None):
    env = dict(os.environ)
    env.pop(ENV_KEY, None)
    if value is not None:
        env[ENV_KEY] = value
    with mock.patch.dict(os.environ, env, clear=True):
        return worker_mod._parse_args(argv or [])


def _worker(**overrides) -> worker_mod.Worker:
    kwargs = dict(
        session_factory=None,
        queues=["agent"],
        worker_id="test:1",
        lease_seconds=60.0,
        heartbeat_seconds=30.0,
        heartbeat_source="env",
    )
    kwargs.update(overrides)
    return worker_mod.Worker(**kwargs)


class WorkerHeartbeatEnvTest(unittest.TestCase):
    def test_env_value_is_read(self) -> None:
        args = _parse_with_env("30")
        self.assertEqual(args.heartbeat_seconds, 30.0)
        self.assertEqual(args.heartbeat_source, "env")

    def test_cli_beats_env(self) -> None:
        args = _parse_with_env("30", ["--heartbeat-seconds", "5"])
        self.assertEqual(args.heartbeat_seconds, 5.0)
        self.assertEqual(args.heartbeat_source, "cli")

    def test_blank_env_falls_back_to_default(self) -> None:
        args = _parse_with_env("  ")
        self.assertEqual(args.heartbeat_seconds, worker_mod.DEFAULT_HEARTBEAT_SECONDS)
        self.assertEqual(args.heartbeat_source, "default")

    def test_unset_env_falls_back_to_default(self) -> None:
        args = _parse_with_env(None)
        self.assertEqual(args.heartbeat_seconds, worker_mod.DEFAULT_HEARTBEAT_SECONDS)
        self.assertEqual(args.heartbeat_source, "default")

    def test_non_numeric_env_fails_fast_with_key_name(self) -> None:
        with self.assertRaises(SystemExit) as ctx:
            _parse_with_env("thirty")
        self.assertIn(ENV_KEY, str(ctx.exception))

    def test_non_positive_env_fails_fast_with_key_name(self) -> None:
        with self.assertRaises(SystemExit) as ctx:
            _parse_with_env("0")
        self.assertIn(ENV_KEY, str(ctx.exception))

    def test_half_lease_is_allowed(self) -> None:
        """compose 的 30s / 60s 租约正好在边界上，必须能起。"""
        self.assertEqual(_worker().heartbeat_seconds, 30.0)

    def test_heartbeat_above_half_lease_is_rejected(self) -> None:
        """防止有人把 30 改成 300：超过 lease/2 直接起不来。"""
        with self.assertRaises(ValueError) as ctx:
            _worker(heartbeat_seconds=300.0)
        self.assertIn("lease", str(ctx.exception))
        self.assertIn("300", str(ctx.exception))

    def test_shrunk_lease_reports_limit_comes_from_lease(self) -> None:
        """租约默认值被调小时，失败必须说清是 lease/2 收紧了上限。

        场景：租约 60 → 40，配置里的 30 依然合法地存在，但 30 > 40/2 会让
        所有现有部署起不来。这条不要求放宽闸门，只要求失败原因可读。
        """
        with self.assertRaises(ValueError) as ctx:
            _worker(lease_seconds=40.0, heartbeat_seconds=30.0)
        message = str(ctx.exception)
        self.assertIn("lease_seconds(40.0)", message)
        self.assertIn("上限 20.0", message)
        self.assertIn("租约的一半", message)
        self.assertIn("调小租约默认值", message)
        self.assertIn(ENV_KEY, message)

    def test_startup_log_reports_heartbeat_source_lease_and_renew(self) -> None:
        worker = _worker()
        with self.assertLogs("erp.worker", level="INFO") as logs:
            worker._log_startup()
        joined = "\n".join(logs.output)
        self.assertIn("heartbeat_seconds=30.0 source=env", joined)
        self.assertIn("lease=60.0s renew=20.0s heartbeat_limit=30.0s (lease/2)", joined)


if __name__ == "__main__":
    unittest.main()
