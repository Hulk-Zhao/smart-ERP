"""安全用例基线：权限矩阵/IDOR 要验证「授权正常时能通过」，需要同步回滚入口是开着的。

生产默认关闭（``ERP_ROLLBACK_SYNC_ENABLED`` 未设置即 false）。关闭态下的
跨租户优先级（403 先于 503）由 ``tests/api/test_rollback_gate.py`` 覆盖；
这里只保证矩阵里的 200 分支测的是授权，而不是开关。
"""

from __future__ import annotations

import pytest

from apps.api.rollback_gate import ENV_ROLLBACK_SYNC_ENABLED


@pytest.fixture(autouse=True)
def _legacy_sync_rollback_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV_ROLLBACK_SYNC_ENABLED, "1")
