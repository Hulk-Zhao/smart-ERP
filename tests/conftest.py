"""测试基线：存量用例默认按「同步回滚已开放」的旧行为跑。

生产默认是关闭（``ERP_ROLLBACK_SYNC_ENABLED`` 未设置即 false）。默认值本身的
证据在 ``tests/api/test_rollback_gate.py``：那里显式 ``delenv`` 后断言 503 与
连接器零调用。这里只是让历史用例不必逐个加开关，避免把「补开关」变成大面积
改测试。
"""

from __future__ import annotations

import pytest

from apps.api.rollback_gate import ENV_ROLLBACK_SYNC_ENABLED


@pytest.fixture(autouse=True)
def _legacy_sync_rollback_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV_ROLLBACK_SYNC_ENABLED, "1")
