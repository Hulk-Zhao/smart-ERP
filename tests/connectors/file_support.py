"""文件通道测试的共用件：仓库内夹具目录 + 装配好的栈。

夹具全部在 ``fixtures/connectors/file`` 下，不依赖系统临时目录或绝对路径。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from apps.connectors.breaker import BreakerPolicy
from apps.connectors.file import FileStack, build_file_stack
from apps.connectors.ratelimit import ActionRateLimiter
from apps.connectors.retry import RetryPolicy
from apps.platform.policy.levels import RateLimit
from tests.connectors.support import run

ROOT = Path(__file__).resolve().parents[2]
FILE_FIXTURES = ROOT / "fixtures" / "connectors" / "file"


def make_file_stack(
    root: str | Path | None = None,
    *,
    sleep=None,
    read_limit: RateLimit | None = None,
    retry_policy: RetryPolicy | None = None,
    breaker_policy: BreakerPolicy | None = None,
    timeout_s: float = 5.0,
    limiter: ActionRateLimiter | None = None,
) -> FileStack:
    return build_file_stack(
        root=root or FILE_FIXTURES,
        sleep=sleep or asyncio.sleep,
        read_limit=read_limit,
        retry_policy=retry_policy,
        breaker_policy=breaker_policy,
        timeout_s=timeout_s,
        limiter=limiter,
    )


__all__ = ["FILE_FIXTURES", "ROOT", "make_file_stack", "run"]
